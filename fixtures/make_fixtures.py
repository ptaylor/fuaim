#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Paul Taylor
"""Generate the committed fixture: synthetic media plus a scanned index.

Run from anywhere; idempotent. The media is a few seconds of synthesised audio —
no recordings are committed — and the index is what `fuaim scan` writes over it,
with invented labels, events and a transcript patched in so the browser has
something to show before a real scan exists. See fixtures/README.md.

    python3 fixtures/make_fixtures.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
MEDIA = HERE / "media"
INDEX = HERE / "fuaim-index"

# (filename, aevalsrc expression, duration seconds, sample rate)
PIECES = [
    ("whistle.wav",
     "0.3*sin(2*PI*(440+330*t)*t)", 4, 44100),
    ("two-note.mp3",
     "0.3*sin(2*PI*440*t)*between(mod(t,1),0,0.5)"
     "+0.3*sin(2*PI*660*t)*between(mod(t,1),0.5,1)", 3, 44100),
    ("pulses.m4a",
     "0.3*sin(2*PI*300*t)*(0.5+0.5*sin(2*PI*4*t))", 3, 44100),
    ("quiet-gap.aac",
     "0.3*sin(2*PI*500*t)*between(t,0,1)"
     "+0.3*sin(2*PI*500*t)*between(t,2,3)", 3, 44100),
]

# Invented content, keyed by the media file name, patched over what the scan
# writes. `source` is "fixture" so it can never be mistaken for a model's work.
FIXTURE = {
    "whistle.wav": {
        "labels": [
            {"text": "whistle", "group": "instrument", "source": "fixture",
             "model": "fixture", "score": 0.9, "windows": 4, "weak": False},
        ],
        "events": [
            {"text": "whistle", "group": "instrument", "source": "fixture",
             "model": "fixture", "at_s": 0.0, "at_e": 4.0, "score": 0.9},
        ],
    },
    "two-note.mp3": {
        "labels": [
            {"text": "music", "group": "sound_type", "source": "fixture",
             "model": "fixture", "score": 0.88, "windows": 4, "weak": False},
            {"text": "piano", "group": "instrument", "source": "fixture",
             "model": "fixture", "score": 0.7, "windows": 3, "weak": True},
        ],
        "events": [
            {"text": "piano", "group": "instrument", "source": "fixture",
             "model": "fixture", "at_s": 0.0, "at_e": 1.5, "score": 0.7},
            {"text": "piano", "group": "instrument", "source": "fixture",
             "model": "fixture", "at_s": 1.5, "at_e": 3.0, "score": 0.72},
        ],
    },
    "pulses.m4a": {
        "labels": [
            {"text": "music", "group": "sound_type", "source": "fixture",
             "model": "fixture", "score": 0.85, "windows": 4, "weak": False},
        ],
        "transcription": {
            "model": "fixture", "language": "en", "at": "2026-10-02T00:00:00Z",
            "text": "This is a synthesised recording, not real speech.",
            "segments": [
                {"at_s": 0.0, "at_e": 1.4, "text": "This is a synthesised recording,"},
                {"at_s": 1.4, "at_e": 3.0, "text": "not real speech."},
            ],
        },
    },
    "quiet-gap.aac": {
        "labels": [
            {"text": "noise", "group": "sound_type", "source": "fixture",
             "model": "fixture", "score": 0.82, "windows": 3, "weak": False},
        ],
        "events": [
            {"text": "noise", "group": "sound_type", "source": "fixture",
             "model": "fixture", "at_s": 0.0, "at_e": 1.0, "score": 0.82},
            {"text": "noise", "group": "sound_type", "source": "fixture",
             "model": "fixture", "at_s": 2.0, "at_e": 3.0, "score": 0.8},
        ],
    },
}


def generate_media() -> None:
    MEDIA.mkdir(parents=True, exist_ok=True)
    for name, expr, duration, rate in PIECES:
        target = MEDIA / name
        subprocess.run([
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"aevalsrc='{expr}':s={rate}:d={duration}",
            str(target),
        ], check=True)


def scan() -> None:
    subprocess.run([
        sys.executable, str(ROOT / "scan.py"), str(MEDIA), "--index", str(INDEX),
    ], check=True)


def patch() -> None:
    directory = INDEX / "audio"
    for path in sorted(directory.glob("*.json")):
        record = json.loads(path.read_text())
        name = record["source"]["path"].split("/")[-1]
        extra = FIXTURE.get(name)
        if not extra:
            continue
        for key, value in extra.items():
            record[key] = value
        if "labels" in extra:
            record["label"] = {
                "version": 1, "model": "fixture", "vocabulary": None,
                "windows": 4, "at": "2026-10-02T00:00:00Z",
            }
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")


def main() -> None:
    generate_media()
    scan()
    patch()
    print(f"fixture ready: {INDEX}")


if __name__ == "__main__":
    main()
