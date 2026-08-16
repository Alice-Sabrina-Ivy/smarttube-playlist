#!/usr/bin/env python3
"""Cut a release: bump VERSION, sync it into the UI, tag, push.

    python scripts/release.py --minor          # 1.01 -> 1.02
    python scripts/release.py --major          # 1.13 -> 2.00
    python scripts/release.py --set 2.05       # explicit
    python scripts/release.py --check          # verify everything is in sync
    python scripts/release.py --dry-run --minor

Versions are MAJOR.MINOR with a ZERO-PADDED two-digit minor: 1.00, 1.01, …
1.99, 2.00. Deliberately not the YYYY.MM.DD scheme used by asi-letter — that
suits dated documents, not software where users need to reason about
compatibility.

Two consequences of the padding, both load-bearing here:

  * **Do not sort tags with git.** `--sort=v:refname` follows strverscmp,
    which reads a digit run with a leading zero as a FRACTION — so `v1.01`
    sorts BELOW `v1.0`, and `--check` would call a correctly-released repo
    drifted, permanently. Measured, not assumed. `newest_tag` compares
    (major, minor) as integers instead.

  * **One spelling per release.** `1.01` and `1.1` are the same number but
    different strings, and the GHCR image tag comes from the string — so
    allowing both would publish two pinnable tags for one build. Only the
    padded form is accepted, and tagging refuses a version that already
    exists under another spelling.

The pre-1.01 tag `v1.0` predates the scheme; it is still read correctly (as
1.0) so history keeps working, it is simply not a form this script will
produce.

What happens after the tag is pushed is CI's job, not this script's:
.github/workflows/publish.yml builds the multi-arch image, tags it
`1.01`, `1`, and `latest` in GHCR, and creates the GitHub Release with notes
generated from the commits since the previous tag. Old releases and old image
tags stay available forever — GitHub lists them newest-first automatically, so
there is no manifest to maintain.

`--check` is the CI-friendly mode: it verifies VERSION, the UI footer and the
latest git tag all agree, and exits non-zero if they have drifted.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "VERSION"
INDEX_HTML = ROOT / "index.html"

# Canonical: a two-digit (or wider, past 1.99) zero-padded minor. `1.1` is
# deliberately NOT accepted — see the module docstring.
VERSION_RX = re.compile(r"^(\d+)\.(\d{2,})$")
# Anything version-shaped, padded or not, with or without the leading `v`.
# Used only for READING tags, so the legacy `v1.0` still participates in
# ordering rather than being silently dropped from it.
ANY_VERSION_RX = re.compile(r"^v?(\d+)\.(\d+)$")
MINOR_WIDTH = 2
# The footer carries the version for people who never open a terminal. It's
# rendered from /api/status at runtime, but the static fallback in the markup
# should still be right for anyone reading the source. Anchored on the span id
# rather than the surrounding prose, so rewording the footer doesn't silently
# break the sync.
FOOTER_RX = re.compile(r'(id="app-version">v)(\d+\.\d+)')


class ReleaseError(RuntimeError):
    pass


def run(*args: str, capture: bool = True) -> str:
    result = subprocess.run(
        args, cwd=ROOT, capture_output=capture, text=True, check=True,
    )
    return (result.stdout or "").strip()


def parse_canonical(text: str) -> tuple[int, int] | None:
    """(major, minor) for a canonical version string, else None.

    Strict on purpose: `1.1` is refused even though it is a perfectly good
    number, because it is a different STRING from `1.01` and the image tag is
    built from the string.

    Canonical means "round-trips through format_version", which catches the
    OVER-padded spellings too. `1.001` also parses to (1, 1), so accepting it
    would publish a third tag for the same release — and worse, `--check`
    would then agree with itself forever, because the tag and VERSION match
    as strings while no v1.01 was ever cut. The regex alone only blocked the
    under-padded direction.
    """
    m = VERSION_RX.match(text or "")
    if not m:
        return None
    major, minor = int(m.group(1)), int(m.group(2))
    if format_version(major, minor) != text:
        return None
    return (major, minor)


def parse_any(text: str) -> tuple[int, int] | None:
    """(major, minor) for any version-shaped string, padded or not.

    Reading, not writing: this is how the legacy `v1.0` tag keeps its place
    in the ordering.
    """
    m = ANY_VERSION_RX.match((text or "").strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def format_version(major: int, minor: int) -> str:
    return f"{major}.{minor:0{MINOR_WIDTH}d}"


def read_version() -> str:
    if not VERSION_FILE.exists():
        raise ReleaseError(f"{VERSION_FILE.name} is missing")
    raw = VERSION_FILE.read_text(encoding="utf-8").strip()
    if parse_canonical(raw) is None:
        raise ReleaseError(
            f"VERSION should look like 1.01 (zero-padded minor), found {raw!r}"
        )
    return raw


def bump(current: str, part: str) -> str:
    parsed = parse_any(current)
    if parsed is None:
        raise ReleaseError(f"cannot bump {current!r}")
    major, minor = parsed
    if part == "major":
        return format_version(major + 1, 0)
    # Width is a floor, not a ceiling: 1.99 -> 1.100, which still orders
    # correctly because the comparison is on integers.
    return format_version(major, minor + 1)


def newest_tag(tags: Iterable[str]) -> str | None:
    """The highest version tag, compared NUMERICALLY.

    Not `git --sort=v:refname`: that follows strverscmp, which treats a
    leading zero as a fraction and therefore ranks v1.01 below v1.0 — the
    exact inversion this project's scheme would hit on its second release.
    Unparseable refs are skipped rather than sorting somewhere arbitrary.
    """
    best: tuple[int, int] | None = None
    best_tag: str | None = None
    for tag in tags:
        parsed = parse_any(tag)
        if parsed is None:
            continue
        if best is None or parsed > best:
            best, best_tag = parsed, tag.strip()
    return best_tag


def conflicting_tag(version: str, tags: Iterable[str]) -> str | None:
    """An existing tag that is the SAME VERSION spelled differently, if any.

    Tagging v1.10 while v1.1 exists would put a second GHCR tag on one
    release, each pinnable and neither obviously the wrong one to pin.
    """
    target = parse_any(version)
    if target is None:
        return None
    for tag in tags:
        if parse_any(tag) == target:
            return tag.strip()
    return None


def footer_version() -> str | None:
    m = FOOTER_RX.search(INDEX_HTML.read_text(encoding="utf-8"))
    return m.group(2) if m else None


def all_tags() -> list[str]:
    try:
        return run("git", "tag", "--list", "v*.*").splitlines()
    except subprocess.CalledProcessError:
        return []


def latest_tag() -> str | None:
    # No --sort: git's version ordering is wrong for this scheme. See
    # newest_tag.
    return newest_tag(all_tags())


def write_version(new: str) -> None:
    VERSION_FILE.write_text(new + "\n", encoding="utf-8")


def sync_footer(new: str) -> bool:
    """Point the static footer at `new`. Returns True if it changed."""
    text = INDEX_HTML.read_text(encoding="utf-8")
    updated, count = FOOTER_RX.subn(lambda m: m.group(1) + new, text)
    if count == 0:
        raise ReleaseError(
            "couldn't find the version in the index.html footer — the markup "
            "changed and FOOTER_RX needs updating"
        )
    if updated == text:
        return False
    INDEX_HTML.write_text(updated, encoding="utf-8", newline="\n")
    return True


def check() -> int:
    """Verify VERSION, the footer and the newest tag agree."""
    problems: list[str] = []
    version = read_version()
    foot = footer_version()
    tag = latest_tag()

    if foot != version:
        problems.append(f"index.html footer says v{foot}, VERSION says {version}")
    if tag is None:
        problems.append("no v*.* tag exists yet — nothing has been released")
    elif parse_any(tag) != parse_any(version):
        problems.append(f"newest tag is {tag}, VERSION says {version}")
    elif tag.lstrip("v") != version:
        # Same number, different spelling — e.g. tag v1.1 against VERSION
        # 1.01. The image tag comes from the string, so this is a real
        # mismatch even though the versions compare equal.
        problems.append(
            f"newest tag is {tag} but VERSION says {version} — same version, "
            f"two spellings; the image would be published under both"
        )

    print(f"VERSION      : {version}")
    print(f"index.html   : v{foot}")
    print(f"newest tag   : {tag or '(none)'}")
    for p in problems:
        print(f"  MISMATCH: {p}")
    if problems:
        return 1
    print("in sync")
    return 0


def ensure_clean_tree() -> None:
    if run("git", "status", "--porcelain"):
        raise ReleaseError(
            "working tree has uncommitted changes — commit or stash first, so "
            "the tag points at exactly what was tested"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--major", action="store_true", help="1.13 -> 2.00")
    group.add_argument("--minor", action="store_true", help="1.01 -> 1.02")
    group.add_argument("--set", metavar="X.YY", help="set an explicit version, e.g. 1.05")
    ap.add_argument("--check", action="store_true",
                    help="verify VERSION, footer and tag agree; changes nothing")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would happen without writing or pushing")
    args = ap.parse_args()

    try:
        if args.check:
            return check()

        current = read_version()
        if args.set:
            if parse_canonical(args.set) is None:
                hint = ""
                loose = parse_any(args.set)
                if loose is not None:
                    hint = f" — did you mean {format_version(*loose)}?"
                raise ReleaseError(
                    f"--set expects a zero-padded minor like 1.01, got "
                    f"{args.set!r}{hint}"
                )
            new = args.set
        elif args.major:
            new = bump(current, "major")
        elif args.minor:
            new = bump(current, "minor")
        else:
            raise ReleaseError("choose one of --major, --minor, --set or --check")

        tag = f"v{new}"
        print(f"{current} -> {new}   (tag {tag})")

        if args.dry_run:
            print("dry run — nothing written, nothing pushed")
            return 0

        ensure_clean_tree()
        existing = all_tags()
        if tag in existing:
            raise ReleaseError(f"{tag} already exists")
        clash = conflicting_tag(new, existing)
        if clash is not None:
            raise ReleaseError(
                f"{clash} is already {new} spelled differently — tagging {tag} "
                f"would publish a second GHCR tag for one release"
            )

        write_version(new)
        sync_footer(new)
        run("git", "add", "VERSION", "index.html")
        # Nothing staged is a legitimate case, not an error: the very first
        # release tags a VERSION that already exists, and re-tagging an
        # unchanged version would otherwise die on "nothing to commit".
        if run("git", "diff", "--cached", "--name-only"):
            run("git", "commit", "-m", f"Release {tag}")
        else:
            print(f"VERSION and footer already say {new} — tagging HEAD as-is")
        run("git", "tag", "-a", tag, "-m", f"Release {tag}")
        run("git", "push", "origin", "HEAD")
        run("git", "push", "origin", tag)

        print(f"pushed {tag}. CI will build the image and open the release:")
        print("  https://github.com/Alice-Sabrina-Ivy/smarttube-playlist/releases")
        return 0
    except ReleaseError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        print(f"git failed: {e}", file=sys.stderr)
        if e.stderr:
            print(e.stderr.strip(), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
