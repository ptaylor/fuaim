#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Paul Taylor
"""fuaim label — ask what is in each recording, and write it down.

`scan.py` stops at numbers: it knows a recording is forty seconds of speech at
-12 dB and nothing about what is in it. This is the other half of the indexer.
It samples a handful of fixed-length windows from each recording, asks a
zero-shot audio-language model (CLAP) how well each phrase in `vocabulary.yaml`
fits them, and writes the phrases that survive into the record's `labels` — and,
where the per-window scores support it, a sound-event timeline in `events` with
`at_s` and `at_e`, so a recording that is birds for half a minute and then talk
is labelled at the right points, not only as a whole.

    fuaim label ~/Recordings                label everything not already labelled
    fuaim label ~/Recordings --limit 20     twenty recordings, to look at results
    fuaim label ~/Recordings --calibrate    report where the scores actually fall
    fuaim label ~/Recordings --tune --force set thresholds from the scores and rewrite vocabulary.yaml
    fuaim label ~/Recordings --force        label again, even where it already has

**This is one of the two parts of the project with dependencies.** It needs
PyTorch and transformers, and PyTorch's last Intel-macOS wheel is 2.2.2 — which
stops at Python 3.12, so it cannot be installed into the interpreter the rest of
the project runs on. It therefore has its own virtual environment, and says so
rather than failing with an ImportError:

    python3.12 -m venv ~/.venvs/fuaim-models
    ~/.venvs/fuaim-models/bin/pip install "torch==2.2.2" "numpy<2" "transformers==4.40.2" pyyaml

**Labelling writes `labels` and `events` and nothing else.** No measurement is
taken and none is invalidated, so `SCAN_VERSION` is untouched and a run can be
repeated as often as the vocabulary is edited without re-measuring a single
file. What makes a record skippable is its own `label` block: the model and the
vocabulary hash that produced the labels it already has.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import wave
from pathlib import Path

# tokenizers uses a thread pool by default; label.py runs ffmpeg as a child
# process for every window, and the pool would print a fork warning per window.
# One thread is plenty for tokenising the vocabulary, so it is disabled rather
# than tolerated.
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

HERE = Path(__file__).resolve().parent
# The sibling indexer, imported rather than copied: these two are the same half
# of the tool, and the terminal output, the ffmpeg call and the atomic write are
# the same in both. scan.py has no import-time side effects.
sys.path.insert(0, str(HERE))
from scan import (  # noqa: E402
    INDEX_DIR_NAME, Glyphs, Palette, Reporter, colour_wanted, existing_records,
    human_duration, resolve_index, run, unicode_ok, write_json,
)

# The zero-shot audio-language model. laion/clap-htsat-fused is the recommended
# start (Apache-2.0); MS-CLAP 2023 (MS-PL) is the alternative (AGENTS.md).
MODEL = "laion/clap-htsat-fused"
# CLAP's native sample rate; the window is decoded to this before it is scored.
SAMPLE_RATE = 48000
# A handful of fixed-length windows per recording — the number is an open
# question in AGENTS.md, a measurement decision rather than a guess.
WINDOWS = 8
WINDOW_SECONDS = 10.0
TIMEOUT = 60.0
# Bumped when the scoring or what a record's `label` block means changes, the
# way scan.py's SCAN_VERSION guards measurements.
LABEL_VERSION = 1
# The threshold rule applied by --tune, documented in vocabulary.yaml: a label's
# p90 where that is already signal, otherwise its p99, and never below the floor.
# A label marked `locked: true` in the vocabulary keeps its hand-picked value.
TUNE_P90_LINE = 0.15
TUNE_FLOOR = 0.10

INSTALL = """fuaim label needs PyTorch and transformers, which are not installed here.
They cannot go into the interpreter this project otherwise runs on: PyTorch's
last Intel-macOS wheel is 2.2.2, and it stops at Python 3.12. Give them their
own environment:

    python3.12 -m venv ~/.venvs/fuaim-models
    ~/.venvs/fuaim-models/bin/pip install "torch==2.2.2" "numpy<2" "transformers==4.40.2" pyyaml

transformers is pinned to 4.40.2: this Intel Mac's last PyTorch wheel is 2.2.2,
and a newer transformers demands PyTorch >= 2.5.

Then run this command with that interpreter, or through the dispatcher:

    ~/.venvs/fuaim-models/bin/python label.py <DIR>
"""


def dependencies():
    """The model stack, or None with an explanation.

    Imported here rather than at the top so that `--help` works, and so that a
    missing dependency is a sentence about how to fix it rather than a traceback
    ending in ImportError.
    """
    try:
        import numpy  # noqa: F401
        import torch  # noqa: F401
        import transformers  # noqa: F401
        import yaml  # noqa: F401
    except ImportError:
        sys.stderr.write(INSTALL)
        return None
    return transformers, torch, numpy, yaml


# ------------------------------------------------------------------ vocabulary


def load_vocabulary(yaml, path: Path) -> tuple[list[dict], list[dict]]:
    """The labels to ask for, and the calibration negatives.

    The negatives are scored alongside the real labels and never written: a
    label that scores no better on "silence" than on the recording is measuring
    nothing. They are the "is anything here at all?" floor.
    """
    data = yaml.safe_load(path.read_text())
    labels = []
    for entry in data.get("zero_shot") or []:
        labels.append({
            "text": entry["text"],
            "group": entry.get("group"),
            "threshold": float(entry.get("threshold", 0.0)),
            "min_windows": int(entry.get("min_windows", 1)),
            "weak": bool(entry.get("weak", False)),
            "locked": bool(entry.get("locked", False)),
        })
    negatives = [{"text": text}
                 for text in (data.get("calibration") or {}).get("negatives", [])]
    return labels, negatives


def vocabulary_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
    against the index directory, which is what lets an index travel with its
    media (docs/index-format.md, rule 2)."""
    value = manifest.get("media_root") or "."
    root = Path(value)
    return root if root.is_absolute() else (index_dir / root).resolve()


# --------------------------------------------------------------------- windows


def window_times(duration: float | None, wanted: int, window: float) -> list[tuple[float, float | None]]:
    """Where to listen. A fixed-length window, spread evenly across the recording.

    A recording shorter than the window is one window over all of it. A recording
    with no measured duration is one window from the start, of unknown length —
    the extractor then takes the whole file.
    """
    if not duration or duration <= 0:
        return [(0.0, None)]
    length = min(window, duration)
    if length >= duration:
        return [(0.0, duration)]
    wanted = max(1, wanted)
    if wanted == 1:
        return [(0.0, length)]
    span = duration - length
    return [(span * i / (wanted - 1), span * i / (wanted - 1) + length)
            for i in range(wanted)]


def extract_window(source: Path, start: float, end: float | None, target: Path) -> bool:
    """One window, decoded to mono 48 kHz PCM — the form CLAP scores.

    Seeking before the input is a fast seek, and one ffmpeg per window keeps a
    long recording from being decoded end to end just to listen to a few moments.
    """
    command = ["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{start:.3f}",
               "-i", str(source)]
    if end is not None:
        command += ["-t", f"{end - start:.3f}"]
    command += ["-map", "0:a:0", "-ac", "1", "-ar", str(SAMPLE_RATE),
                "-c:a", "pcm_s16le", "-y", str(target)]
    code, _, _ = run(command, timeout=TIMEOUT)
    return code == 0 and target.is_file() and target.stat().st_size > 0


def load_wav(path: Path, numpy) -> "numpy.ndarray":
    """The window's samples as float32 in [-1, 1], straight from the stdlib."""
    with wave.open(str(path), "rb") as handle:
        raw = handle.readframes(handle.getnframes())
    data = numpy.frombuffer(raw, dtype=numpy.int16).astype(numpy.float32) / 32768.0
    return data


# ------------------------------------------------------------------- labelling


def label_record(transformers, torch, numpy, model, processor, text_features,
                 by_group: dict[str, list[int]], record: dict, root: Path,
                 vocabulary: list[dict], negatives: list[dict],
                 wanted: int, window: float) -> tuple[list[dict], list[dict], list[dict], str | None]:
    """Score one recording, and return the labels and events it earned.

    Scored window by window, and the strongest window is what the record's
    `score` reports. Each label is softmaxed against its own group plus the
    calibration negatives — never the whole vocabulary — the same per-group rule
    the video sibling measures: one softmax over the whole vocabulary hands the
    single best label almost the whole probability budget, while per-group alone
    forces a winner out of every group. The negatives are the floor.

    Whole-file labels are the vocabulary's own `min_windows`: a count of windows
    that cleared the label's threshold. The sound-event timeline merges the
    passing windows that are adjacent in time, so a label that holds for the
    middle half of a recording is an event there rather than one everywhere.
    """
    technical = record.get("technical") or {}
    duration = technical.get("duration_s")
    source = root / record["source"]["path"] if record.get("source") else None
    if source is None or not source.is_file():
        return [], [], [], f"media file is not reachable: {source}"

    times = window_times(duration, wanted, window)
    arrays: list = []
    spans: list[tuple[float, float | None]] = []
    with tempfile.TemporaryDirectory(prefix="fuaim-label-") as work:
        for index, (start, end) in enumerate(times):
            target = Path(work) / f"{index + 1:02d}.wav"
            if not extract_window(source, start, end, target):
                continue
            data = load_wav(target, numpy)
            if data.size == 0:
                continue
            arrays.append(data)
            spans.append((start, end))
        if not arrays:
            return [], [], [], "no audio could be decoded"

        inputs = processor(audios=arrays, sampling_rate=SAMPLE_RATE,
                           return_tensors="pt", padding=True)
        with torch.no_grad():
            audio_features = model.get_audio_features(**inputs)
        audio_features = audio_features / audio_features.norm(dim=-1, keepdim=True)
        scale = model.logit_scale_a.exp()
        logits = scale * audio_features @ text_features.T      # (windows, labels + negatives)

        negative_indices = list(range(len(vocabulary), len(vocabulary) + len(negatives)))
        per_label: dict[str, list[float]] = collections.defaultdict(list)
        per_window: list[dict[str, float]] = []
        for w in range(logits.shape[0]):
            full = logits[w].softmax(dim=-1)                    # the negatives keep this scale
            row: dict[str, float] = {}
            for indices in by_group.values():
                within = logits[w, indices + negative_indices].softmax(dim=-1)
                for offset, index in enumerate(indices):
                    score = within[offset].item()
                    per_label[vocabulary[index]["text"]].append(score)
                    row[vocabulary[index]["text"]] = score
            for entry, score in zip(negatives, full[len(vocabulary):].tolist()):
                per_label[entry["text"]].append(score)
            per_window.append(row)

    kept = []
    for entry in vocabulary:
        scores = per_label[entry["text"]]
        agreed = sum(1 for score in scores if score >= entry["threshold"])
        if agreed >= entry["min_windows"]:
            kept.append({
                "text": entry["text"],
                "group": entry["group"],
                "source": "zero_shot",
                "model": MODEL_LABEL,
                "score": round(max(scores), 4),
                "windows": agreed,
                "weak": entry["weak"],
            })
    kept.sort(key=lambda label: label["score"], reverse=True)
    kept_texts = {label["text"] for label in kept}

    events = []
    for entry in vocabulary:
        if entry["text"] not in kept_texts:
            continue
        passing = [i for i in range(len(per_window))
                   if per_window[i].get(entry["text"], 0.0) >= entry["threshold"]]
        if not passing:
            continue
        runs = []
        run_start = run_end = passing[0]
        for i in passing[1:]:
            if i == run_end + 1:
                run_end = i
            else:
                runs.append((run_start, run_end))
                run_start = run_end = i
        runs.append((run_start, run_end))
        for a, b in runs:
            events.append({
                "text": entry["text"],
                "group": entry["group"],
                "source": "zero_shot",
                "model": MODEL_LABEL,
                "at_s": round(spans[a][0], 3),
                "at_e": round(spans[b][1], 3) if spans[b][1] is not None else None,
                "score": round(max(per_window[i][entry["text"]] for i in range(a, b + 1)), 4),
            })
    events.sort(key=lambda event: event["at_s"])

    observed = [{"text": entry["text"], "group": entry["group"],
                 "threshold": entry["threshold"], "scores": per_label[entry["text"]],
                 "weak": entry["weak"]}
                for entry in vocabulary]
    observed += [{"text": entry["text"], "group": "calibration",
                  "threshold": 0.0, "scores": per_label[entry["text"]], "weak": False}
                 for entry in negatives]
    return kept, events, observed, None


# ---------------------------------------------------------------------- report


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def print_calibration(observed: dict[str, list[float]], thresholds: dict[str, float],
                      groups: dict[str, str], negatives: set[str]) -> None:
    """Where the window scores actually sit, which is the only way to set a
    threshold. Per window, not per recording, because a threshold is compared
    with a window's score: `min_windows` counts the windows that cleared it."""
    sys.stdout.write("\n  window scores over the library, per label\n")
    sys.stdout.write("  (a label is kept when `min_windows` windows score at or above "
                     "its threshold)\n\n")
    sys.stdout.write(f"  {'label':36} {'group':10} {'windows':>8} {'p50':>7} {'p90':>7} "
                     f"{'p99':>7} {'max':>7}  threshold\n")
    for text, scores in sorted(observed.items(), key=lambda kv: -percentile(kv[1], 0.90)):
        if text in negatives:
            continue
        sys.stdout.write(
            f"  {text[:35]:36} {(groups.get(text) or '')[:9]:10} {len(scores):>8} "
            f"{percentile(scores, 0.50):>7.3f} {percentile(scores, 0.90):>7.3f} "
            f"{percentile(scores, 0.99):>7.3f} {max(scores):>7.3f}  "
            f"{thresholds.get(text, 0.0):>6.2f}\n")
    if negatives:
        sys.stdout.write("\n  calibration negatives — nothing should sit high here\n")
        for text in sorted(negatives):
            scores = observed.get(text) or []
            if scores:
                sys.stdout.write(f"    {text[:44]:46} p90 {percentile(scores, 0.90):>7.3f}  "
                                 f"max {max(scores):>7.3f}\n")
    sys.stdout.write(
        "\n  Set a threshold near a label's p90 to keep the recordings where it is\n"
        "  unmistakable, or near the p99 to keep only the ones it shouts about.\n")


def tune_thresholds(observed: dict[str, list[float]], thresholds: dict[str, float],
                    locked: set[str], path: Path) -> list[tuple[str, float, float]]:
    """Apply the threshold rule to a score distribution and write it back.

    Only the `threshold:` value on each zero-shot line is rewritten, so the
    comments, order, `min_windows`, `weak` and `locked` all survive untouched.
    Returns the labels that changed as (text, old, new). If a planned label
    cannot be matched back to a line, nothing is written rather than half the
    file.
    """
    planned: dict[str, float] = {}
    for text, old in thresholds.items():
        if text in locked:
            continue
        scores = observed.get(text) or []
        if len(scores) < 2:
            continue
        p90 = percentile(scores, 0.90)
        p99 = percentile(scores, 0.99)
        new = round(p90, 2) if p90 >= TUNE_P90_LINE else round(p99, 2)
        new = max(TUNE_FLOOR, new)
        if abs(new - old) > 0.005:
            planned[text] = new
    if not planned:
        return []

    lines = path.read_text().splitlines()
    changed = 0
    for index, line in enumerate(lines):
        match = re.match(r"^\s*-\s*\{\s*text:\s*([^,]+),", line)
        if not match:
            continue
        text = match.group(1).strip()
        if text not in planned:
            continue
        new_line, count = re.subn(r"threshold:\s*[0-9.]+",
                                  f"threshold: {planned[text]:.2f}", line)
        if count == 1:
            lines[index] = new_line
            changed += 1
    if changed != len(planned):
        raise SystemExit(
            f"tune matched {changed} of {len(planned)} labels in {path.name}; "
            f"writing nothing")
    path.write_text("\n".join(lines) + "\n")
    return [(text, thresholds[text], planned[text]) for text in sorted(planned)]


# --------------------------------------------------------------------- command


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fuaim label",
        description="Ask a zero-shot audio-language model what is in each recording and "
                    "record it as labels and sound events the browser can filter by.",
        epilog="Labelling writes `labels` and `events` only: no measurement is taken and "
               "none is invalidated, so this may be run again whenever vocabulary.yaml changes.",
    )
    parser.add_argument("dir", nargs="?", default=".",
                        help="the library, or the index directory itself (default: .)")
    parser.add_argument("--force", action="store_true",
                        help="label again even where the labels are already current")
    parser.add_argument("--limit", type=int, default=None, help="stop after this many recordings")
    parser.add_argument("--match", default=None, metavar="TEXT",
                        help="only recordings whose path contains TEXT — a year, a folder, a name")
    parser.add_argument("--windows", type=int, default=WINDOWS,
                        help=f"windows to sample per recording (default: {WINDOWS})")
    parser.add_argument("--window-seconds", type=float, default=WINDOW_SECONDS,
                        help=f"length of each window in seconds (default: {WINDOW_SECONDS})")
    parser.add_argument("--model", default=MODEL,
                        help=f"CLAP checkpoint on the Hugging Face hub (default: {MODEL})")
    parser.add_argument("--vocabulary", default=str(HERE / "vocabulary.yaml"),
                        help="the labels to ask for (default: vocabulary.yaml beside this program)")
    parser.add_argument("--calibrate", action="store_true",
                        help="report the score distribution at the end")
    parser.add_argument("--tune", action="store_true",
                        help="set thresholds from this run's score distribution and rewrite the "
                             "vocabulary (combine with --force to re-measure everything)")
    parser.add_argument("--dry-run", action="store_true",
                        help="say what would be labelled, write nothing")
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
    transformers, torch, numpy, yaml = stack
    from transformers import ClapModel, ClapProcessor

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

    vocabulary_path = Path(args.vocabulary).expanduser()
    if not vocabulary_path.is_file():
        sys.stderr.write(f"no vocabulary at {vocabulary_path}\n")
        return 1
    vocabulary, negatives = load_vocabulary(yaml, vocabulary_path)
    if not vocabulary:
        sys.stderr.write(f"{vocabulary_path} asks for no labels\n")
        return 1
    digest = vocabulary_hash(vocabulary_path)

    records, unreadable = existing_records(index_dir)
    if not records:
        sys.stderr.write(f"no records in {index_dir}/audio\n")
        return 1

    todo = []
    for asset_id, record in sorted(records.items()):
        previous = record.get("label") or {}
        if not args.force and previous.get("model") == MODEL_LABEL \
                and previous.get("vocabulary") == digest \
                and previous.get("version") == LABEL_VERSION:
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
        ("model", f"{args.model}  " + colour.dim("CLAP, Apache-2.0")),
        ("labels", f"{len(vocabulary)} phrases, {len(negatives)} calibration negatives, "
                   f"{args.windows} windows per recording"),
        ("to do", f"{len(todo)} recordings  "
                  + colour.dim(f"{len(records) - len(todo)} already current")),
    ])
    if unreadable:
        reporter.warn(f"{len(unreadable)} record(s) could not be read and are left alone")

    if not todo:
        reporter.summary([("nothing to label", "bold"),
                          ("use --force to label again", "dim")])
        if args.calibrate:
            print_calibration({}, {}, {}, set())
        if args.tune:
            reporter.note("tune needs window scores, but there was nothing to label; "
                          "run with --force")
        return 0
    if args.dry_run:
        for index, asset_id in enumerate(todo, start=1):
            reporter.skipped(index, len(todo), records[asset_id]["source"]["path"], "would label")
        reporter.summary([(f"{len(todo)} recordings", "bold"), ("nothing written", "dim")])
        return 0

    started = time.perf_counter()
    reporter.working(0, len(todo), "loading the model")
    model = ClapModel.from_pretrained(args.model)
    processor = ClapProcessor.from_pretrained(args.model)
    model.eval()
    texts = [entry["text"] for entry in vocabulary]
    texts += [entry["text"] for entry in negatives]
    text_inputs = processor(text=texts, return_tensors="pt", padding=True)
    with torch.no_grad():
        text_features = model.get_text_features(**text_inputs)
        text_features = text_features / text_features.norm(dim=-1, keepdim=True)
    by_group: dict[str, list[int]] = collections.defaultdict(list)
    for index, entry in enumerate(vocabulary):
        by_group[entry["group"]].append(index)
    reporter.working(0, len(todo), "")
    reporter.note(f"model ready in {time.perf_counter() - started:.1f}s")

    labelled = failed = 0
    written_labels = written_events = 0
    errors: list[dict] = []
    observed: dict[str, list[float]] = collections.defaultdict(list)
    thresholds = {entry["text"]: entry["threshold"] for entry in vocabulary}
    groups = {entry["text"]: entry["group"] for entry in vocabulary}
    locked = {entry["text"] for entry in vocabulary if entry["locked"]}

    for index, asset_id in enumerate(todo, start=1):
        record = records[asset_id]
        relative = record["source"]["path"]
        reporter.working(index, len(todo), relative)
        began = time.perf_counter()
        keep, events, scores, error = label_record(
            transformers, torch, numpy, model, processor, text_features,
            by_group, record, root, vocabulary, negatives, args.windows,
            args.window_seconds)
        if error:
            failed += 1
            errors.append({"path": relative, "stage": "label", "message": error})
            reporter.failed(index, len(todo), relative, error)
            continue

        for row in scores:
            observed[row["text"]].extend(row["scores"])
        record["labels"] = keep
        record["events"] = events
        record["label"] = {
            "version": LABEL_VERSION,
            "model": MODEL_LABEL,
            "vocabulary": digest,
            "windows": args.windows,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        try:
            write_json(index_dir / "audio" / f"{asset_id}.json", record)
        except ValueError as exc:
            failed += 1
            errors.append({"path": relative, "stage": "label", "message": str(exc)})
            reporter.failed(index, len(todo), relative, str(exc))
            continue
        labelled += 1
        written_labels += len(keep)
        written_events += len(events)
        names = ", ".join(label["text"] for label in keep[:4]) or "nothing"
        reporter.done(index, len(todo), relative,
                      f"{len(keep):>2} labels, {len(events):>2} events  "
                      f"{colour.dim(names[:52])}  "
                      + colour.dim(f"{time.perf_counter() - began:.1f}s"))

    if errors:
        known = {(e.get("path"), e.get("stage")) for e in manifest.get("errors") or []}
        merged = list(manifest.get("errors") or [])
        for error in errors:
            if (error["path"], error["stage"]) not in known:
                merged.append(error)
        manifest["errors"] = merged
    # The manifest records what produced the index, and now part of that is a
    # model rather than a decoder (docs/index-format.md, rule 8).
    models = [m for m in manifest.get("models") or []
              if m.get("name") not in ("transformers", "clap")]
    models.append({"name": "transformers", "version": transformers.__version__,
                   "licence": "Apache-2.0"})
    models.append({"name": "clap", "version": args.model, "licence": "Apache-2.0"})
    manifest["models"] = models
    write_json(index_dir / "manifest.json", manifest)

    elapsed = time.perf_counter() - started
    reporter.summary([
        (f"{labelled} labelled", "green"),
        (f"{written_labels} labels", "bold"),
        (f"{written_events} events", "bold" if written_events else "dim"),
        (f"{elapsed / 60:.1f} min" if elapsed > 90 else f"{elapsed:.0f}s", "dim"),
    ])
    if failed:
        reporter.warn(f"{failed} recording(s) could not be labelled; recorded in the manifest")
    if args.calibrate:
        print_calibration(observed, thresholds, groups,
                          {entry["text"] for entry in negatives})
    if args.tune:
        changed = tune_thresholds(observed, thresholds, locked, vocabulary_path)
        if changed:
            reporter.note(f"tune: {len(changed)} threshold(s) rewritten in "
                          f"{vocabulary_path.name}; the next 'fuaim label' "
                          f"relabels with them")
            for text, old, new in changed:
                sys.stdout.write(f"    {text[:38]:40} {groups.get(text, '')[:8]:9} "
                                 f"{old:.2f} -> {new:.2f}\n")
        else:
            reporter.note("tune: every label already sits at its rule value")
    return 0


MODEL_LABEL = MODEL


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.stderr.write("\nstopped\n")
        raise SystemExit(130)
