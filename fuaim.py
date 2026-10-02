#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Paul Taylor
"""fuaim — one entry point for the fuaim tools.

    fuaim scan <DIR>       index every recording under DIR, so the browser can read it
    fuaim label <DIR>      classify each recording against vocabulary.yaml
    fuaim transcribe <DIR> run speech-to-text over each recording, for search
    fuaim open <DIR>       serve the index in DIR and open it in a browser
    fuaim browse <DIR>     the same command, for when "browse" reads better

<DIR> is either the index directory itself (the one holding `manifest.json`) or
a library root with a `fuaim-index` subdirectory in it. Both are accepted, so
neither has to be remembered. With no DIR at all the committed fixture is used,
which is what makes this repository runnable from a fresh clone.

This is a **dispatcher, not a wrapper**: it replaces itself with the program for
the subcommand, so signals, exit codes and output behave exactly as if that
program had been run directly. That also keeps the two halves separate programs,
which AGENTS.md requires — the entry point must not grow into a third
implementation of either.

Install it with `./install.sh`; see that script for where it goes.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# resolve() follows a symlink, so an installed ~/bin/fuaim still finds the
# checkout it points at. Nothing else in here may depend on the current directory.
HERE = Path(__file__).resolve().parent

COMMANDS = {
    "scan": ("scan.py", None),
    "label": ("label.py", "fuaim-models"),
    "transcribe": ("transcribe.py", "fuaim-models"),
    "open": ("browse.py", None),
    "browse": ("browse.py", None),
}

# The model-using commands are the only ones with dependencies, and they cannot
# live in the interpreter the rest of this project runs on: PyTorch's last
# Intel-macOS wheel is 2.2.2, and it stops at Python 3.12. So `fuaim label` and
# `fuaim transcribe` exec the model environment's own interpreter.
# FUAIM_MODELS_PYTHON overrides where that is.
MODELS_PYTHON = Path(os.environ.get("FUAIM_MODELS_PYTHON") or
                     Path.home() / ".venvs" / "fuaim-models" / "bin" / "python")

MODELS_INSTALL = """fuaim label and fuaim transcribe need PyTorch and the model
libraries, which live in their own environment:

    python3.12 -m venv ~/.venvs/fuaim-models
    ~/.venvs/fuaim-models/bin/pip install "torch==2.2.2" "numpy<2" transformers faster-whisper pyyaml
"""

# The directory name `fuaim scan` writes by default, and the one `fuaim browse`
# looks for under a library root. The same constant appears in scan.py and
# browse.py: the halves are separate programs and either may be run on its own,
# so neither may import the other.
INDEX_DIR_NAME = "fuaim-index"

USAGE = """usage: fuaim <command> [args]

commands:
  scan <DIR>        index every recording under DIR, so the browser can read it
  label <DIR>       classify each recording against vocabulary.yaml
  transcribe <DIR>  run speech-to-text over each recording, for search
  open <DIR>        serve the index in DIR and open it in a browser
  browse <DIR>      the same command, for when "browse" reads better

<DIR> is either a library root or the index directory itself. A library root is
searched for a 'fuaim-index' subdirectory, which is where scan puts the index
unless told otherwise; point at the index directory itself, under any name, to
use one kept somewhere else. With no <DIR>, scan indexes the current directory
and the browser opens the committed fixture.

Anything after <DIR> is passed straight to the command:

  fuaim scan ~/Recordings --force --jobs 4
  fuaim label ~/Recordings --limit 20 --calibrate
"""


def main(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 2
    if argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    command, rest = argv[0], argv[1:]
    if command not in COMMANDS:
        sys.stderr.write(f"fuaim: unknown command {command!r}\n\n")
        sys.stderr.write(USAGE)
        return 2
    program, venv = COMMANDS[command]
    path = HERE / program
    python = sys.executable
    if venv is not None:
        if not MODELS_PYTHON.exists():
            sys.stderr.write(MODELS_INSTALL)
            return 1
        python = str(MODELS_PYTHON)
    # execv replaces this process with the subcommand's own, so there is no
    # shared entry point for either half to creep into the other through.
    os.execv(python, [python, str(path), *rest])
    sys.stderr.write(f"fuaim: could not exec {path}\n")
    return 127


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
