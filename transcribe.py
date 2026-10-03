#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Paul Taylor
"""fuaim transcribe — run speech-to-text over each recording, and write it down.

`label.py` says what a recording is; this says what is said in it. It runs
Whisper (through faster-whisper, the CTranslate2 runtime) over each recording and
writes a full transcript with per-segment timestamps into the record's
`transcription`, so spoken words are searchable and each segment can be jumped to
in the browser.

    fuaim transcribe ~/Recordings                  transcribe everything not done
    fuaim transcribe ~/Recordings --limit 20       twenty recordings, to look
    fuaim transcribe ~/Recordings --model small    a bigger model, more CPU
    fuaim transcribe ~/Recordings --force          transcribe again

**One of the two parts of the project with dependencies.** It needs
faster-whisper (and through it CTranslate2), which live in the model environment:

    python3.12 -m venv ~/.venvs/fuaim-models
    ~/.venvs/fuaim-models/bin/pip install "torch==2.2.2" "numpy<2" "transformers==4.40.2" faster-whisper pyyaml

**Transcription writes `transcription` and nothing else.** No measurement is
taken and none is invalidated, so `SCAN_VERSION` is untouched and a run never
costs a re-scan or a re-label. What makes a record skippable is its own
`transcription` block: the model and version that produced the transcript it
already has.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The sibling indexer, imported rather than copied: the terminal output, the
# ffmpeg call and the atomic write are the same in every half of this tool.
# scan.py has no import-time side effects.
sys.path.insert(0, str(HERE))
from scan import (  # noqa: E402
    INDEX_DIR_NAME, Glyphs, Palette, Reporter, colour_wanted, existing_records,
    human_duration, run, unicode_ok, write_json,
)

# `base` multilingual is the natural default; `small` for accuracy, `tiny` for
# speed (AGENTS.md, open questions). The size is recorded, not the CTranslate2
# checkpoint id, so the index names the model rather than the runtime.
MODEL = "base"
SAMPLE_RATE = 16000      # Whisper's native rate
COMPUTE_TYPE = "int8"    # faster-whisper on CPU
TRANSCRIBE_VERSION = 1
TIMEOUT = 120.0

INSTALL = """fuaim transcribe needs faster-whisper, which is not installed here.
Give it its own environment (the same one label uses):

    python3.12 -m venv ~/.venvs/fuaim-models
    ~/.venvs/fuaim-models/bin/pip install "torch==2.2.2" "numpy<2" "transformers==4.40.2" faster-whisper pyyaml

Then run this command with that interpreter, or through the dispatcher:

    ~/.venvs/fuaim-models/bin/python transcribe.py <DIR>
"""


def dependencies():
    """The model stack, or None with an explanation. Imported here rather than at
    the top so that `--help` works and a missing dependency is a sentence about
    how to fix it rather than a traceback ending in ImportError."""
    try:
        import numpy  # noqa: F401
        import faster_whisper  # noqa: F401
    except ImportError:
        sys.stderr.write(INSTALL)
        return None
    return faster_whisper, numpy


# ---------------------------------------------------------------------- index


def load_index(directory: Path) -> tuple[Path, dict] | tuple[None, None]:
    """The index directory and its manifest, from either a library root or the
    index directory itself — the same either-way rule the other commands follow."""
    if (directory / "manifest.json").is_file():
        index_dir = directory
    else:
        index_dir = directory / INDEX_DIR_NAME
    manifest_path = index_dir / "manifest.json"
    if not manifest_path.is_file():
        return None, None
    try:
        return index_dir, json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None, None


def media_root(index_dir: Path, manifest: dict) -> Path:
    """The library the index describes. `media_root` may be relative, resolved
    against the index directory (docs/index-format.md, rule 2)."""
    value = manifest.get("media_root") or "."
    root = Path(value)
    return root if root.is_absolute() else (index_dir / root).resolve()


# ------------------------------------------------------------------ audio


def load_wav(path: Path, numpy) -> "numpy.ndarray":
    """Samples as float32 in [-1, 1], straight from the stdlib."""
    with wave.open(str(path), "rb") as handle:
        raw = handle.readframes(handle.getnframes())
    return numpy.frombuffer(raw, dtype=numpy.int16).astype(numpy.float32) / 32768.0


# ------------------------------------------------------------------ one asset


def transcribe_record(model, numpy, record: dict, root: Path,
                      language: str | None, vad: bool) -> tuple[dict | None, str | None]:
    """Run Whisper over one recording, and return the transcription block.

    The whole recording is decoded to mono 16 kHz PCM and handed to the model as
    an array — ffmpeg is the only decoder, the same as everywhere else in the
    tool, so no extra audio dependency is needed. `condition_on_previous_text` is
    off: each recording stands alone, and letting the model lean on its previous
    output is exactly how Whisper invents speech over music or silence.
    """
    source = root / record["source"]["path"] if record.get("source") else None
    if source is None or not source.is_file():
        return None, f"media file is not reachable: {source}"

    with tempfile.TemporaryDirectory(prefix="fuaim-transcribe-") as work:
        target = Path(work) / "audio.wav"
        code, _, err = run([
            "ffmpeg", "-v", "error", "-nostdin", "-i", str(source),
            "-map", "0:a:0", "-ac", "1", "-ar", str(SAMPLE_RATE),
            "-c:a", "pcm_s16le", "-y", str(target),
        ], timeout=TIMEOUT)
        if code != 0 or not target.is_file() or target.stat().st_size == 0:
            return None, "no audio could be decoded"
        data = load_wav(target, numpy)
    if data.size == 0:
        return None, "no audio could be decoded"

    segments, info = model.transcribe(
        data, language=language, vad_filter=vad, beam_size=5,
        condition_on_previous_text=False)
    kept = []
    parts = []
    for segment in segments:
        text = segment.text.strip()
        kept.append({"at_s": round(segment.start, 3),
                     "at_e": round(segment.end, 3), "text": text})
        parts.append(text)
    transcription = {
        "model": f"whisper-{MODEL_LABEL}",
        "version": TRANSCRIBE_VERSION,
        "language": info.language,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "text": " ".join(part for part in parts if part).strip(),
        "segments": kept,
    }
    return transcription, None


# --------------------------------------------------------------------- command


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fuaim transcribe",
        description="Run speech-to-text over each recording and write a transcript "
                    "the browser can search and jump to.",
        epilog="Transcription writes `transcription` only: no measurement is taken and "
               "none is invalidated, so this may be run again whenever the model changes.",
    )
    parser.add_argument("dir", nargs="?", default=".",
                        help="the library, or the index directory itself (default: .)")
    parser.add_argument("--force", action="store_true",
                        help="transcribe again even where the transcript is already current")
    parser.add_argument("--limit", type=int, default=None, help="stop after this many recordings")
    parser.add_argument("--match", default=None, metavar="TEXT",
                        help="only recordings whose path contains TEXT — a year, a folder, a name")
    parser.add_argument("--model", default=MODEL,
                        help=f"Whisper model size: tiny, base, small, medium, large-v3 "
                             f"(default: {MODEL})")
    parser.add_argument("--language", default=None,
                        help="language code to force (default: auto-detect)")
    parser.add_argument("--vad", action="store_true",
                        help="skip silence with voice-activity detection")
    parser.add_argument("--dry-run", action="store_true",
                        help="say what would be transcribed, write nothing")
    parser.add_argument("--quiet", action="store_true", help="only failures and the summary")
    parser.add_argument("--verbose", action="store_true", help="also report per-recording timing")
    parser.add_argument("--no-colour", "--no-color", dest="no_colour", action="store_true",
                        help="plain output, no ANSI colours")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    colour = Palette(colour_wanted(sys.stdout) and not args.no_colour)
    reporter = Reporter(colour, Glyphs(unicode_ok()), args.verbose, args.quiet, width=38)

    stack = dependencies()
    if stack is None:
        return 1
    faster_whisper, numpy = stack
    from faster_whisper import WhisperModel

    global MODEL_LABEL
    MODEL_LABEL = args.model

    directory = Path(args.dir).expanduser()
    if not directory.is_dir():
        sys.stderr.write(f"no such directory: {directory}\n")
        return 1
    index_dir, manifest = load_index(directory)
    if index_dir is None:
        sys.stderr.write(f"no index under {directory} — run 'fuaim scan {directory}' first\n")
        return 1
    root = media_root(index_dir, manifest)

    records, unreadable = existing_records(index_dir)
    if not records:
        sys.stderr.write(f"no records in {index_dir}/audio\n")
        return 1

    model_label = f"whisper-{MODEL_LABEL}"
    todo = []
    for asset_id, record in sorted(records.items()):
        previous = record.get("transcription") or {}
        if not args.force and previous.get("model") == model_label \
                and previous.get("version") == TRANSCRIBE_VERSION:
            continue
        todo.append(asset_id)
    if args.match:
        needle = args.match.lower()
        todo = [i for i in todo if needle in records[i]["source"]["path"].lower()]
    if args.limit is not None:
        todo = todo[:args.limit]

    reporter.header([
        ("index", str(index_dir)),
        ("media", str(root)),
        ("model", f"whisper-{MODEL_LABEL}  " + colour.dim("faster-whisper, MIT")),
        ("to do", f"{len(todo)} recordings  "
                  + colour.dim(f"{len(records) - len(todo)} already current")),
    ])
    if unreadable:
        reporter.warn(f"{len(unreadable)} record(s) could not be read and are left alone")

    if not todo:
        reporter.summary([("nothing to transcribe", "bold"),
                          ("use --force to transcribe again", "dim")])
        return 0
    if args.dry_run:
        for index, asset_id in enumerate(todo, start=1):
            reporter.skipped(index, len(todo), records[asset_id]["source"]["path"],
                             "would transcribe")
        reporter.summary([(f"{len(todo)} recordings", "bold"), ("nothing written", "dim")])
        return 0

    started = time.perf_counter()
    reporter.working(0, len(todo), "loading the model")
    model = WhisperModel(MODEL_LABEL, device="cpu", compute_type=COMPUTE_TYPE)
    reporter.working(0, len(todo), "")
    reporter.note(f"model ready in {time.perf_counter() - started:.1f}s")

    transcribed = failed = 0
    written_segments = 0
    errors: list[dict] = []

    for index, asset_id in enumerate(todo, start=1):
        record = records[asset_id]
        relative = record["source"]["path"]
        reporter.working(index, len(todo), relative)
        began = time.perf_counter()
        transcription, error = transcribe_record(
            model, numpy, record, root, args.language, args.vad)
        if error:
            failed += 1
            errors.append({"path": relative, "stage": "transcribe", "message": error})
            reporter.failed(index, len(todo), relative, error)
            continue
        record["transcription"] = transcription
        try:
            write_json(index_dir / "audio" / f"{asset_id}.json", record)
        except ValueError as exc:
            failed += 1
            errors.append({"path": relative, "stage": "transcribe", "message": str(exc)})
            reporter.failed(index, len(todo), relative, str(exc))
            continue
        transcribed += 1
        written_segments += len(transcription["segments"])
        language = transcription["language"] or "?"
        snippet = (transcription["text"] or "")[:48] or "no speech"
        reporter.done(index, len(todo), relative,
                      f"{len(transcription['segments']):>2} segs  {language}  "
                      f"{colour.dim(snippet)}  "
                      + colour.dim(f"{time.perf_counter() - began:.1f}s"))

    if errors:
        known = {(e.get("path"), e.get("stage")) for e in manifest.get("errors") or []}
        merged = list(manifest.get("errors") or [])
        for error in errors:
            if (error["path"], error["stage"]) not in known:
                merged.append(error)
        manifest["errors"] = merged
    # The manifest records what produced the index (docs/index-format.md, rule 8).
    models = [m for m in manifest.get("models") or []
              if m.get("name") not in ("faster-whisper", "whisper")]
    models.append({"name": "faster-whisper", "version": faster_whisper.__version__,
                   "licence": "MIT"})
    models.append({"name": "whisper", "version": model_label, "licence": "Apache-2.0"})
    manifest["models"] = models
    write_json(index_dir / "manifest.json", manifest)

    elapsed = time.perf_counter() - started
    reporter.summary([
        (f"{transcribed} transcribed", "green"),
        (f"{written_segments} segments", "bold"),
        (f"{elapsed / 60:.1f} min" if elapsed > 90 else f"{elapsed:.0f}s", "dim"),
    ])
    if failed:
        reporter.warn(f"{failed} recording(s) could not be transcribed; recorded in the manifest")
    return 0


MODEL_LABEL = MODEL


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.stderr.write("\nstopped\n")
        raise SystemExit(130)
