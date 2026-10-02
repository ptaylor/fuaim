#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Paul Taylor
"""fuaim browse — the browser half of fuaim.

Reads an index produced by the indexer and presents the library as a grid of
waveform cards. Standard library only, and no build step: this file is the CLI
and the HTTP server, and the interface it serves is ordinary files in `static/`.

Not implemented yet. The browser half is built against the fixture index in
`fixtures/` before the indexer exists, so the index contract is proved
implementable first (AGENTS.md). This stub stands in so the dispatcher and the
installer work end to end.
"""

from __future__ import annotations

import argparse
import sys

INDEX_DIR_NAME = "fuaim-index"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="fuaim browse",
        description="serve a fuaim index as a browsable library",
    )
    parser.add_argument(
        "dir", nargs="?",
        help="library root or index directory (default: the committed fixture)",
    )
    parser.add_argument("--index", help="index directory, under any name")
    parser.add_argument("--port", type=int, default=8765, help="port to serve on")
    parser.add_argument("--no-open", action="store_true", help="do not open a browser")
    parser.parse_args(argv)
    parser.exit(1, "fuaim browse: not implemented yet — built against fixtures/ first\n")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
