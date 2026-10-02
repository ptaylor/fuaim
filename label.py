#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Paul Taylor
"""fuaim label — one half of the fuaim indexer.

Asks a zero-shot audio-language model (CLAP) how well each phrase in
`vocabulary.yaml` fits a handful of sampled windows of each recording, and
writes the phrases that survive as whole-file labels, plus a sound-event
timeline where the per-window scores support one. This is the only command with
dependencies, and it runs in its own environment (see fuaim.py).

Not implemented yet. This stub stands in so the dispatcher and the installer
work end to end.
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="fuaim label",
        description="classify each recording against vocabulary.yaml",
    )
    parser.add_argument("dir", nargs="?", default=".", help="library root")
    parser.add_argument("--index", help="index directory, under any name")
    parser.add_argument("--limit", type=int, help="only the first N recordings")
    parser.add_argument("--match", help="only recordings whose path contains this")
    parser.add_argument("--force", action="store_true", help="re-label everything")
    parser.add_argument("--calibrate", action="store_true", help="report score spread to tune thresholds")
    parser.parse_args(argv)
    parser.exit(1, "fuaim label: not implemented yet\n")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
