#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Paul Taylor
"""fuaim scan — the indexer half of fuaim.

Walks a directory hierarchy of recordings, measures each one with `ffprobe` and
`ffmpeg`, generates a waveform (the cover) and a spectrogram (the detail view),
and writes an index the browser half reads. Standard library only; ffmpeg and
ffprobe are the only external programs, run as **CLI processes**, never linked.
Linking `libav*` would make this program GPL-3.0-or-later as well; see the
licence section of AGENTS.md.

Not implemented yet. This stub stands in so the dispatcher and the installer
work end to end.
"""

from __future__ import annotations

import argparse
import sys

INDEX_DIR_NAME = "fuaim-index"

# Extensions the scan will index; anything else is silently skipped, per
# requirement 6 in AGENTS.md (a folder of recordings legitimately contains a
# .DS_Store, a receipt .pdf, a .txt note or a .png).
AUDIO_EXTENSIONS = ("wav", "amr", "m4a", "3ga", "mpeg", "mp3", "aac", "mp2", "ac3")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="fuaim scan",
        description="index a library of recordings",
    )
    parser.add_argument("dir", nargs="?", default=".", help="library root")
    parser.add_argument("--index", help="index directory, under any name")
    parser.add_argument("--force", action="store_true", help="re-index everything")
    parser.add_argument("--dry-run", action="store_true", help="say what would happen, write nothing")
    parser.add_argument("--jobs", type=int, default=1, help="parallel processes")
    parser.add_argument("--proxy", action="store_true", help="write playback proxies for formats a browser cannot decode")
    parser.add_argument("--proxy-minutes", help="cap proxy length; 'max' for the whole recording")
    parser.parse_args(argv)
    parser.exit(1, "fuaim scan: not implemented yet\n")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
