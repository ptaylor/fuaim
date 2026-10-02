#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Paul Taylor
"""fuaim transcribe — the other half of the fuaim indexer.

Runs speech-to-text (Whisper) over each recording and writes a full transcript
with per-segment timestamps, so spoken words are searchable and each segment can
be jumped to. Transcription writes transcripts and nothing else, so it never
costs a re-scan or a re-label. It runs in its own environment (see fuaim.py).

Not implemented yet. This stub stands in so the dispatcher and the installer
work end to end.
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="fuaim transcribe",
        description="run speech-to-text over each recording",
    )
    parser.add_argument("dir", nargs="?", default=".", help="library root")
    parser.add_argument("--index", help="index directory, under any name")
    parser.add_argument("--limit", type=int, help="only the first N recordings")
    parser.add_argument("--match", help="only recordings whose path contains this")
    parser.add_argument("--force", action="store_true", help="re-transcribe everything")
    parser.add_argument("--model", help="Whisper model size (tiny, base, small, …)")
    parser.parse_args(argv)
    parser.exit(1, "fuaim transcribe: not implemented yet\n")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
