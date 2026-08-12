#!/usr/bin/env python3
"""Cut a release: bump VERSION, sync it into the UI, tag, push.

    python scripts/release.py --minor          # 1.0 -> 1.1
    python scripts/release.py --major          # 1.3 -> 2.0
    python scripts/release.py --set 2.5        # explicit
    python scripts/release.py --check          # verify everything is in sync
    python scripts/release.py --dry-run --minor

Versions are MAJOR.MINOR (v1.0, v1.1, v2.0) to match this project's existing
history. Deliberately not the YYYY.MM.DD scheme used by asi-letter — that suits
dated documents, not software where users need to reason about compatibility.

What happens after the tag is pushed is CI's job, not this script's:
.github/workflows/publish.yml builds the multi-arch image, tags it
`1.1`, `1`, and `latest` in GHCR, and creates the GitHub Release with notes
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

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "VERSION"
INDEX_HTML = ROOT / "index.html"

VERSION_RX = re.compile(r"^(\d+)\.(\d+)$")
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


def read_version() -> str:
    if not VERSION_FILE.exists():
        raise ReleaseError(f"{VERSION_FILE.name} is missing")
    raw = VERSION_FILE.read_text(encoding="utf-8").strip()
    if not VERSION_RX.match(raw):
        raise ReleaseError(f"VERSION should look like 1.0, found {raw!r}")
    return raw


def bump(current: str, part: str) -> str:
    major, minor = (int(x) for x in current.split("."))
    if part == "major":
        return f"{major + 1}.0"
    return f"{major}.{minor + 1}"


def footer_version() -> str | None:
    m = FOOTER_RX.search(INDEX_HTML.read_text(encoding="utf-8"))
    return m.group(2) if m else None


def latest_tag() -> str | None:
    try:
        tags = run("git", "tag", "--list", "v*.*", "--sort=-v:refname")
    except subprocess.CalledProcessError:
        return None
    return tags.splitlines()[0] if tags else None


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
    elif tag.lstrip("v") != version:
        problems.append(f"newest tag is {tag}, VERSION says {version}")

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
    group.add_argument("--major", action="store_true", help="1.3 -> 2.0")
    group.add_argument("--minor", action="store_true", help="1.0 -> 1.1")
    group.add_argument("--set", metavar="X.Y", help="set an explicit version")
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
            if not VERSION_RX.match(args.set):
                raise ReleaseError(f"--set expects X.Y, got {args.set!r}")
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
        if tag in run("git", "tag", "--list", tag).splitlines():
            raise ReleaseError(f"{tag} already exists")

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
