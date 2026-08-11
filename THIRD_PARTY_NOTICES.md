# Third-party notices

SmartTube Playlist's own source is MIT — see [LICENSE](LICENSE).

This file covers the third-party code **bundled into the published container image**. It matters because the two things are licensed differently: the Git repository contains none of this code, only a dependency list, whereas the image contains all of it.

## Summary

| | |
|---|---|
| **This repository's source** | MIT |
| **The published container image** | Combined work — see below. Distributed under **GPL-3.0-or-later** terms |

The image ships 36 Python packages. All but the two named below are permissive (MIT, BSD, Apache-2.0, PSF), which impose only attribution requirements satisfied by this file.

## pyytlounge — GPL-3.0

- **Version:** 3.2.0
- **Upstream:** https://github.com/FabioGNR/pyytlounge
- **License:** GNU General Public License v3.0

The authoritative licence is the [`LICENSE` file in the upstream repository](https://github.com/FabioGNR/pyytlounge/blob/master/LICENSE), which is the complete, unmodified text of the GNU GPL v3. The same file ships inside the distribution at `pyytlounge-3.2.0.dist-info/licenses/LICENSE`.

(For anyone who checks and finds an apparent contradiction: pyytlounge's PyPI metadata also carries `Classifier: License :: OSI Approved :: MIT License`. A licence file governs over a classifier that disagrees with it, so this project treats pyytlounge as GPL-3.0.)

**What follows from that.** Because the container image contains pyytlounge's code, the image is a combined work and is distributed under GPL-3.0-or-later terms. There is no licence conflict: MIT is compatible with the GPL in the direction that matters, so this project's own MIT-licensed source can be combined freely. Anyone obtaining the **source** from GitHub receives it under MIT; anyone obtaining the **image** receives the combination under the GPL.

**Corresponding source.** pyytlounge is used unmodified and pinned to an exact version, so its complete corresponding source is the upstream release. Retrieve it with:

```bash
pip download pyytlounge==3.2.0 --no-deps --no-binary :all:
```

or from the upstream repository at the `3.2.0` tag.

This project does **not** ship a patched copy. `lounge.py` wraps `YtLoungeApi._process_event` at runtime from its own module, to survive an upstream crash on malformed `onSubtitlesTrackChanged` events. No pyytlounge file is altered or redistributed in modified form.

## certifi — MPL-2.0

- **Version:** 2026.4.22 (or whatever the build resolves)
- **Upstream:** https://github.com/certifi/python-certifi
- **License:** Mozilla Public License 2.0

MPL-2.0 is a file-level copyleft: obligations attach only to modified MPL files. certifi is bundled unmodified, so no source-disclosure obligation arises beyond this notice. Its source is available from the upstream project and PyPI.

## Permissively licensed dependencies

Bundled under MIT, BSD-2/3-Clause, Apache-2.0 or the PSF license. Attribution is provided here; each package's own license text ships inside the image in its `*.dist-info/` directory.

`aiofiles`, `aiohappyeyeballs`, `aiohttp`, `aiosignal`, `androidtvremote2`, `annotated-types`, `anyio`, `attrs`, `cffi`, `click`, `colorama`, `cryptography`, `fastapi`, `frozenlist`, `h11`, `httpcore`, `httptools`, `httpx`, `idna`, `multidict`, `propcache`, `protobuf`, `pycparser`, `pydantic`, `pydantic-core`, `python-dotenv`, `PyYAML`, `sse-starlette`, `starlette`, `typing-extensions`, `uvicorn`, `watchfiles`, `websockets`, `yarl`

Two worth calling out by name:

- **androidtvremote2** (Apache-2.0) — https://github.com/tronikos/androidtvremote2 — the Android TV Remote v2 implementation this project is built on. Apache-2.0 requires that its license and attribution be preserved; the upstream distribution ships no `NOTICE` file, so no additional notice text is required.
- **cryptography** (Apache-2.0 OR BSD-3-Clause) — used by androidtvremote2 for the TLS pairing channel.

## Reproducing this list

The exact set depends on what pip resolves at build time. To regenerate from a built image:

```bash
docker run --rm --entrypoint python ghcr.io/alice-sabrina-ivy/smarttube-playlist:latest \
  -c "import importlib.metadata as m; [print(d.metadata['Name'], d.version) for d in sorted(m.distributions(), key=lambda x: x.metadata['Name'].lower())]"
```

Note that `zeroconf` (LGPL-2.1-or-later) appears only under androidtvremote2's optional `demo` extra, which this image does not install, and is therefore **not** bundled.
