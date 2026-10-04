#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Paul Taylor
"""fuaim scan — the indexer half of fuaim.

Walks a directory hierarchy of recordings, measures each one with `ffprobe` and
`ffmpeg`, generates a waveform (the cover) and a spectrogram (the detail view),
and writes an index that the browser half reads. Standard library only; ffmpeg
and ffprobe are the only external programs, and they are run as **CLI
processes**, never linked. Linking `libav*` would make this program
GPL-3.0-or-later as well; see the licence section of AGENTS.md.

    fuaim scan ~/Recordings                    # measure, label and transcribe
    fuaim scan ~/Recordings --no-label --no-transcribe   # measure only
    fuaim scan ~/Recordings --index /tmp/idx   # put the index elsewhere
    fuaim scan ~/Recordings --force            # re-scan files that have not changed
    fuaim scan ~/Recordings --proxy            # write playable copies where needed
    fuaim scan ~/Recordings --dry-run          # say what would happen, write nothing

One process per file, and nothing is decoded that is not needed:

    ffprobe     technical metadata, duration, capture time
    ffmpeg      one pass for levels and silence; one pass for the two images

After the scan, the label and transcribe halves run over the fresh index as
separate processes under the model environment's interpreter — unless
`--no-label` / `--no-transcribe` says otherwise — so one command measures,
labels and transcribes, while each half stays runnable and redoable on its own.

The index format is the contract in docs/index-format.md. Where this file makes a
choice the contract leaves open, the choice is named as a constant below with the
reason, and the raw measurement is stored alongside the derived one so a
threshold can be re-tuned later without re-scanning the library.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------- constants

# Bumped whenever a stored measurement changes meaning, so a record written under
# an older version is re-scanned rather than kept beside a newer one.
SCAN_VERSION = "1"
INDEX_VERSION = 1
ASSET_VERSION = 1
INDEX_DIR_NAME = "fuaim-index"

# Extensions the scan will index; anything else is silently skipped, per
# requirement 6 in AGENTS.md (a folder of recordings legitimately contains a
# .DS_Store, a receipt .pdf, a .txt note or a .png).
AUDIO_EXTENSIONS = ("wav", "amr", "m4a", "3ga", "mpeg", "mp3", "aac", "mp2", "ac3")

# What a browser plays, so the indexer knows what needs a copy. These mirror the
# expectation recorded in AGENTS.md, to be verified over the real library:
# browsers play wav, mp3 and m4a/aac, and refuse amr, 3ga, mp2, ac3, mpeg and
# raw aac. The browser prefers a copy whenever a record has one, so a wrong guess
# costs a wasted copy rather than a page that will not play.
PLAYABLE_EXTENSIONS = ("wav", "mp3", "m4a")

# The representation images. Fixed width, so cards align; the spectrogram uses a
# log frequency axis. These numbers are starting guesses to tune after one batch
# (AGENTS.md, open questions).
WAVE_SIZE = "1200x360"
SPEC_SIZE = "1200x360"
WAVE_COLOR = "4f8cff"

# Levels and silence. silencedetect's noise floor and minimum duration are
# stored in the fingerprint, so re-tuning them re-measures rather than silently
# reusing old numbers.
SILENCE_NOISE_DB = "-40dB"
SILENCE_DURATION_S = 0.5

# Playable copies. A browser cannot decode amr, 3ga, mp2, ac3, mpeg or raw aac,
# so those get an m4a/AAC copy. The copy is cut short by default — a minute
# shows what a recording is — and is never upscaled in sample rate or channels.
PROXY_SECONDS = 60.0
PROXY_AUDIO_KBPS = 96


def proxy_minutes(value: str) -> float:
    """Minutes of audio to copy, or the word 'max' for no cap.

    Returned as seconds, so 'max' becomes 0.0 — the no-cap sentinel that
    `make_proxy` already reads as "copy the whole recording". `--proxy-minutes 2`
    is the same as `--proxy-seconds 120`.
    """
    if value.strip().lower() == "max":
        return 0.0
    try:
        return float(value) * 60.0
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"not a number of minutes nor 'max': {value!r}")


# ---------------------------------------------------------------- output


class Palette:
    """ANSI colours, off unless the destination is a terminal that wants them."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def green(self, text: str) -> str:
        return self._wrap("32", text)

    def yellow(self, text: str) -> str:
        return self._wrap("33", text)

    def red(self, text: str) -> str:
        return self._wrap("31", text)

    def cyan(self, text: str) -> str:
        return self._wrap("36", text)


class Glyphs:
    """Progress markers, in ASCII if the terminal cannot be trusted with more."""

    def __init__(self, unicode_ok: bool) -> None:
        self.ok = "✓" if unicode_ok else "ok"
        self.skip = "·" if unicode_ok else "-"
        self.fail = "✗" if unicode_ok else "x"


class Reporter:
    """Progress in the form a human reads while waiting.

    One line per file as it finishes, with a transient line for the file being
    worked on so a slow recording does not look like a hang.
    """

    def __init__(self, colour: Palette, glyphs: Glyphs, verbose: bool, quiet: bool,
                 width: int) -> None:
        self.c = colour
        self.g = glyphs
        self.verbose = verbose
        self.quiet = quiet
        self.width = width
        self.live = colour.enabled and not quiet
        self.transient = False

    def _line(self, text: str) -> None:
        if self.transient:
            sys.stdout.write("\r\033[K")
            self.transient = False
        sys.stdout.write(text + "\n")
        sys.stdout.flush()

    def _pending(self, text: str) -> None:
        if not self.live:
            return
        sys.stdout.write("\r\033[K" + text)
        sys.stdout.flush()
        self.transient = True

    def header(self, rows: list[tuple[str, str]]) -> None:
        if self.quiet:
            return
        pad = max(len(label) for label, _ in rows)
        for label, value in rows:
            self._line(f"{self.c.dim(label.ljust(pad))}  {value}")
        self._line("")

    def working(self, index: int, total: int, path: str) -> None:
        self._pending(f"{self.c.dim(f'{index:>4}/{total}')}  - {path[:self.width]}")

    def done(self, index: int, total: int, path: str, detail: str) -> None:
        if self.quiet:
            return
        counter = self.c.dim(f"{index:>4}/{total}")
        self._line(f"{counter}  {self.c.green(self.g.ok)} "
                   f"{path[:self.width].ljust(self.width)} {detail}")

    def skipped(self, index: int, total: int, path: str, detail: str) -> None:
        if self.quiet:
            return
        counter = self.c.dim(f"{index:>4}/{total}")
        self._line(f"{counter}  {self.c.dim(self.g.skip)} "
                   f"{self.c.dim(path[:self.width].ljust(self.width))} {self.c.dim(detail)}")

    def failed(self, index: int, total: int, path: str, detail: str) -> None:
        # Never suppressed: a file that could not be indexed is the thing a
        # caller most needs to see.
        counter = self.c.dim(f"{index:>4}/{total}")
        self._pending("")
        self._line(f"{counter}  {self.c.red(self.g.fail)} "
                   f"{path[:self.width].ljust(self.width)} {self.c.red(detail)}")

    def note(self, text: str) -> None:
        if self.verbose and not self.quiet:
            self._line(self.c.dim("      " + text))

    def warn(self, text: str) -> None:
        if not self.quiet:
            self._line(self.c.yellow("  ! ") + text)

    def summary(self, parts: list[tuple[str, str]]) -> None:
        if self.transient:
            sys.stdout.write("\r\033[K")
            self.transient = False
        chunks = [getattr(self.c, colour)(value) for value, colour in parts]
        self._line("")
        self._line("  ".join(chunks))


def unicode_ok() -> bool:
    encoding = (getattr(sys.stdout, "encoding", "") or "").lower()
    return "utf" in encoding


def colour_wanted(stream) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return hasattr(stream, "isatty") and stream.isatty()


# ---------------------------------------------------------------- helpers


LIVE: set[subprocess.Popen] = set()
STOPPING = False


class Stopped(BaseException):
    """A worker's way out once a ^C has stopped the scan."""


def run(args: list[str], timeout: float | None = None) -> tuple[int, str, str]:
    """Run a program, capturing both streams. Never raises on a non-zero exit.

    stdin is closed so a stray keystroke cannot be eaten by ffmpeg while the user
    is typing in the terminal that started the scan. Popen rather than
    subprocess.run, so the child is visible while it runs and ^C can stop it.
    """
    if STOPPING:
        raise Stopped("interrupted")
    try:
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        return 1, "", str(exc)
    with process:
        LIVE.add(process)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            LIVE.discard(process)
            return 124, "", f"gave up after {timeout:.0f}s"
        else:
            LIVE.discard(process)
    return (process.returncode,
            stdout.decode("utf-8", "replace").strip(),
            stderr.decode("utf-8", "replace").strip())


def stop_children(grace: float = 2.0) -> None:
    """Stop the children, and stop the workers asking for more."""
    global STOPPING
    STOPPING = True
    deadline = time.monotonic() + grace
    while LIVE and time.monotonic() < deadline:
        for process in list(LIVE):
            try:
                process.kill()
            except OSError:
                pass
        time.sleep(0.05)


def first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


FFMPEG_CONTEXT = re.compile(r"^\[[^\]]*\]\s*")


def tidy_error(text: str, path: Path) -> str:
    """Make a message from ffprobe or ffmpeg fit to go in the index.

    Two things have to go: the input path (the index may contain no absolute path
    but `media_root`), and ffmpeg's bracketed log context with its per-run heap
    address, which would make two scans of an unchanged library differ.
    """
    cleaned = first_line(text)
    for prefix in (f"{path}: ", f"{path.resolve()}: "):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
    cleaned = FFMPEG_CONTEXT.sub("", cleaned, count=1).strip()
    return cleaned or "failed"


def as_float(value) -> float | None:
    """A number, or None when the value is not one. nan and inf are refused."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def iso_z(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> datetime | None:
    cleaned = text.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        for pattern in ("%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                parsed = datetime.strptime(text.strip()[:19], pattern)
                break
            except ValueError:
                continue
        else:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


DATE_IN_NAME = (
    re.compile(r"(?P<y>19\d{2}|20\d{2})[-_.]?(?P<m>0[1-9]|1[0-2])[-_.]?(?P<d>0[1-9]|[12]\d|3[01])"
               r"[T_\- ]?(?P<H>[01]\d|2[0-3])[-_.:]?(?P<M>[0-5]\d)[-_.:]?(?P<S>[0-5]\d)?"),
    re.compile(r"(?P<y>19\d{2}|20\d{2})[-_.](?P<m>0[1-9]|1[0-2])[-_.](?P<d>0[1-9]|[12]\d|3[01])"),
)

YEAR_IN_NAME = re.compile(r"(?<!\d)(?P<y>19\d{2}|20\d{2})(?!\d)")


def date_from_name(name: str) -> datetime | None:
    """A full date written into the file name — often the only place it survives."""
    for pattern in DATE_IN_NAME:
        match = pattern.search(name)
        if not match:
            continue
        parts = match.groupdict()
        try:
            return datetime(int(parts["y"]), int(parts["m"]), int(parts["d"]),
                            int(parts.get("H") or 0), int(parts.get("M") or 0),
                            int(parts.get("S") or 0), tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def year_from_name(name: str) -> str | None:
    """A bare year in the file name ("Inters2018.mp3"): coarse, but the library's
    only "when" for files with no metadata. Stored as the first of that year."""
    match = YEAR_IN_NAME.search(name)
    return match.group("y") if match else None


def parse_device(tags: dict) -> dict | None:
    make = tags.get("com.apple.quicktime.make") or tags.get("make")
    model = tags.get("com.apple.quicktime.model") or tags.get("model")
    if not make and not model:
        return None
    return {"make": make or None, "model": model or None}


def slugify(stem: str, limit: int = 32) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", stem).strip("-").lower()
    return (slug or "asset")[:limit]


def asset_id(relative_path: str, stem: str) -> str:
    """The contract's rule: the first 8 hex characters of the SHA-256 of the
    media-root-relative path, then a slug from the file name. Renaming a file
    therefore produces a new id — recorded in the contract as a known cost."""
    digest = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:8]
    return f"{digest}-{slugify(stem)}"


def human_bytes(count: int) -> str:
    size = float(count)
    for unit in ("B", "K", "M", "G", "T"):
        if size < 1024 or unit == "T":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}T"


def human_duration(seconds: float | None) -> str:
    if seconds is None:
        return "--"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(round(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m{rest:02d}s"


# ---------------------------------------------------------------- ffmpeg work


def probe(path: Path, timeout: float) -> tuple[dict, dict, str | None]:
    """Technical facts and capture metadata. Returns (technical, captured, error)."""
    code, out, err = run([
        "ffprobe", "-v", "error",
        "-show_format", "-show_streams", "-of", "json", str(path),
    ], timeout)
    if code != 0:
        return {}, {}, tidy_error(err, path) or f"ffprobe exited {code}"
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        return {}, {}, f"ffprobe output was not JSON: {exc}"

    streams = payload.get("streams") or []
    fmt = payload.get("format") or {}
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if audio is None:
        # A file that is not audio at all (a video-only .mpeg, say). The non-goals
        # say the picture is ignored and only the audio track is indexed, so a
        # file with no audio track is recorded as unreadable rather than dropped
        # silently with the other non-audio files.
        return {}, {}, "no audio stream"

    # The container name comes from the extension, not from format_name: mov, mp4,
    # 3gp and m4a all report the same demuxer, and the extension is what the
    # library is organised by.
    container = path.suffix.lstrip(".").lower()

    duration = as_float(fmt.get("duration"))
    if duration is None:
        duration = as_float(audio.get("duration"))

    bit_rate = as_int(fmt.get("bit_rate"))
    if bit_rate is None:
        bit_rate = as_int(audio.get("bit_rate"))

    technical = {
        "container": container,
        "codec": audio.get("codec_name"),
        "sample_rate": as_int(audio.get("sample_rate")),
        "channels": as_int(audio.get("channels")),
        "bit_rate": bit_rate,
        "duration_s": round(duration, 3) if duration is not None else None,
    }

    tags: dict = {}
    for source in (fmt.get("tags") or {}, audio.get("tags") or {}):
        for key, value in source.items():
            tags.setdefault(key, value)

    captured: dict = {}
    stamp = None
    for key in ("creation_time", "com.apple.quicktime.creationdate", "date"):
        if isinstance(tags.get(key), str):
            stamp = parse_iso(tags[key])
            if stamp:
                break
    if stamp:
        captured["at"] = stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        captured["at_source"] = "container_metadata"
    else:
        named = date_from_name(path.name)
        if named:
            captured["at"] = named.strftime("%Y-%m-%dT%H:%M:%SZ")
            captured["at_source"] = "filename"
        else:
            year = year_from_name(path.name)
            if year:
                captured["at"] = f"{year}-01-01T00:00:00Z"
                captured["at_source"] = "filename"
            else:
                captured["at"] = iso_z(path.stat().st_mtime)
                captured["at_source"] = "file_mtime"
    captured["device"] = parse_device(tags)
    return technical, captured, None


MEAN_VOLUME = re.compile(r"mean_volume:\s*(-?[\d.]+)\s*dB")
MAX_VOLUME = re.compile(r"max_volume:\s*(-?[\d.]+)\s*dB")
SILENCE_DURATION = re.compile(r"silence_duration:\s*([\d.]+)")


def measure(path: Path, timeout: float) -> tuple[dict, str | None]:
    """One pass for levels and silence.

    Returns the raw numbers and an error. `volumedetect`'s `max_volume` is the
    peak and its `mean_volume` an RMS-like mean magnitude, so they are stored as
    `peak_dbfs` and `rms_dbfs`. `silencedetect` reports each quiet run as a
    `silence_duration`, summed into a ratio against the duration by the caller.

    Unlike the image and metadata passes this runs at ffmpeg's default log level,
    because both filters report through the log rather than through the pipe, and
    `-v error` would suppress exactly what is being measured.
    """
    code, _, err = run([
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
        "-map", "0:a:0",
        "-af", f"silencedetect=noise={SILENCE_NOISE_DB}:d={SILENCE_DURATION_S},volumedetect",
        "-f", "null", "-",
    ], timeout)
    if code != 0:
        return {}, tidy_error(err, path) or f"ffmpeg exited {code}"

    peak = None
    rms = None
    for match in MAX_VOLUME.finditer(err):
        peak = as_float(match.group(1))
    for match in MEAN_VOLUME.finditer(err):
        rms = as_float(match.group(1))
    silence = 0.0
    for match in SILENCE_DURATION.finditer(err):
        value = as_float(match.group(1))
        if value is not None:
            silence += value

    return {
        "peak_dbfs": peak,
        "rms_dbfs": rms,
        "silence_duration_s": round(silence, 3),
    }, None


def make_images(path: Path, name: str, index_dir: Path,
                timeout: float) -> tuple[dict | None, str | None]:
    """The waveform (cover) and spectrogram (detail), from one decode pass.

    Both are "pic" filters, so each emits exactly one frame. Written to temporary
    files and renamed, like the records are, so an interrupted scan cannot leave a
    truncated PNG at a path a record from an earlier scan already points at.
    """
    wave_target = index_dir / "wave" / f"{name}.png"
    spec_target = index_dir / "spec" / f"{name}.png"
    wave_target.parent.mkdir(parents=True, exist_ok=True)
    spec_target.parent.mkdir(parents=True, exist_ok=True)
    wave_tmp = wave_target.with_name(wave_target.name + ".tmp.png")
    spec_tmp = spec_target.with_name(spec_target.name + ".tmp.png")

    graph = (f"[0:a:0]showwavespic=s={WAVE_SIZE}:colors=0x{WAVE_COLOR}:split_channels=0[w];"
             f"[0:a:0]showspectrumpic=s={SPEC_SIZE}:legend=0:fscale=log[s]")
    code, _, err = run([
        "ffmpeg", "-v", "error", "-i", str(path),
        "-filter_complex", graph,
        "-map", "[w]", "-frames:v", "1", "-y", str(wave_tmp),
        "-map", "[s]", "-frames:v", "1", "-y", str(spec_tmp),
    ], timeout)
    if code != 0:
        wave_tmp.unlink(missing_ok=True)
        spec_tmp.unlink(missing_ok=True)
        return None, tidy_error(err, path) or f"ffmpeg exited {code} making the images"
    if not (wave_tmp.is_file() and wave_tmp.stat().st_size > 0
            and spec_tmp.is_file() and spec_tmp.stat().st_size > 0):
        wave_tmp.unlink(missing_ok=True)
        spec_tmp.unlink(missing_ok=True)
        return None, "images not written"
    os.replace(wave_tmp, wave_target)
    os.replace(spec_tmp, spec_target)
    return {"wave": f"wave/{name}.png", "spec": f"spec/{name}.png"}, None


# ---------------------------------------------------------------- playable copies


def playable(technical: dict) -> bool:
    """Whether a browser plays this file as it stands, with no copy needed."""
    return str(technical.get("container") or "").lower() in PLAYABLE_EXTENSIONS


def make_proxy(source: Path, target: Path, seconds: float, timeout: float) -> str | None:
    """A playable m4a/AAC copy of one file. Returns an error, or None on success.

    AAC in an m4a container, because the native encoder is always present (no
    libmp3lame dependency) and every browser plays it. The length cap is where
    most of the space and time is saved. Written to a temporary and renamed, as
    records and images are.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp.m4a")
    command = ["ffmpeg", "-v", "error", "-i", str(source), "-map", "0:a:0"]
    if seconds and seconds > 0:
        command += ["-t", f"{seconds:.3f}"]
    command += ["-c:a", "aac", "-b:a", f"{PROXY_AUDIO_KBPS}k", "-y", str(temporary)]
    code, _, err = run(command, timeout)
    if code != 0 or not temporary.is_file() or temporary.stat().st_size == 0:
        temporary.unlink(missing_ok=True)
        return tidy_error(err, source) or f"ffmpeg exited {code} writing a copy"
    os.replace(temporary, target)
    return None


def ensure_proxy(path: Path, name: str, record: dict, index_dir: Path,
                 args) -> tuple[dict | None, str, str | None]:
    """Make the playable copy this file wants, if any.

    Returns the record to write, a note for the progress line, and an error. A
    copy is wanted only for a recording a browser cannot play. A copy that
    already exists and is current is left alone too, which is what makes this a
    cache: rebuilt when the source changed, when the settings changed, or under
    --force. It is deliberately absent from the settings fingerprint — putting it
    there would make switching --proxy on mark every record stale and re-measure
    the whole library before encoding anything.
    """
    if not args.proxy:
        return None, "", None
    technical = record.get("technical") or {}
    if playable(technical):
        return None, "", None

    target = index_dir / "proxies" / f"{name}.m4a"
    source = record.get("source") or {}
    said = record.get("playback") or {}
    same_source = (said.get("source_mtime_ns"), said.get("source_size_bytes")) == (
        source.get("mtime_ns"), source.get("size_bytes"))
    same_settings = said.get("seconds") == args.proxy_seconds
    if not args.force and same_source and same_settings and target.is_file():
        return None, "", None

    error = make_proxy(path, target, args.proxy_seconds, args.timeout)
    if error:
        return None, "", error
    written = target.stat().st_size
    updated = dict(record)
    updated["playback"] = {
        "path": f"proxies/{name}.m4a",
        "seconds": args.proxy_seconds,
        "codec": "aac",
        "bytes": written,
        "source_mtime_ns": source.get("mtime_ns"),
        "source_size_bytes": source.get("size_bytes"),
    }
    return updated, f"copy {human_bytes(written)}", None


# ---------------------------------------------------------------- one asset


def settings_fingerprint(args) -> dict:
    """What a record's measurements and images depend on. When any of it changes,
    a skipped file's record is stale even though the file itself has not been
    touched — so a re-tuned threshold re-measures without --force."""
    return {
        "scan_version": SCAN_VERSION,
        "silence_noise": SILENCE_NOISE_DB,
        "silence_min": SILENCE_DURATION_S,
        "wave_size": WAVE_SIZE,
        "spec_size": SPEC_SIZE,
    }


def settings_drift(records: dict, args) -> tuple[int, dict[str, tuple[object, object]]]:
    wanted = settings_fingerprint(args)
    drift: dict[str, tuple[object, object]] = {}
    stale = 0
    for record in records.values():
        written = record.get("scan") or {}
        differences = [(key, written.get(key), value) for key, value in wanted.items()
                       if written.get(key) != value]
        if not differences:
            continue
        stale += 1
        for key, old, new in differences:
            drift.setdefault(key, (old, new))
    return stale, drift


def scan_one(path: Path, root: Path, index_dir: Path, existing: dict | None,
             args) -> tuple[dict | None, tuple[str, str] | None, list[dict]]:
    """Index one file. Returns (record, (stage, message), warnings).

    A warning is a failure that did not stop the record being written — an image
    that could not be generated, a levels pass that gave up. It is carried out to
    the manifest rather than dropped.
    """
    relative = path.relative_to(root).as_posix()
    name = asset_id(relative, path.stem)
    stat = path.stat()
    previous_source = (existing or {}).get("source") or {}
    same_file = (previous_source.get("size_bytes") == stat.st_size
                 and previous_source.get("mtime_ns") == stat.st_mtime_ns)

    if existing is not None and not args.force:
        fingerprint = existing.get("scan") or {}
        wanted = settings_fingerprint(args)
        same_rules = all(fingerprint.get(key) == value for key, value in wanted.items())
        wave_path = ((existing.get("wave") or {}).get("path") or "")
        spec_path = ((existing.get("spec") or {}).get("path") or "")
        images_present = ((index_dir / wave_path).is_file()
                          and (index_dir / spec_path).is_file())
        if (same_file and same_rules and images_present
                and existing.get("asset_version") == ASSET_VERSION):
            return None, None, []

    started = time.monotonic()
    technical, captured, error = probe(path, args.timeout)
    if error:
        return None, ("probe", error), []

    warnings: list[dict] = []
    analysis: dict = {"peak_dbfs": None, "rms_dbfs": None, "silence_ratio": None}
    measured, measure_error = measure(path, args.timeout)
    if measure_error:
        warnings.append({"stage": "analysis", "message": measure_error})
    else:
        duration = technical.get("duration_s")
        silence_ratio = None
        if duration:
            silence_ratio = round((measured.get("silence_duration_s") or 0.0) / duration, 4)
        analysis = {
            "peak_dbfs": measured.get("peak_dbfs"),
            "rms_dbfs": measured.get("rms_dbfs"),
            "silence_duration_s": measured.get("silence_duration_s"),
            "silence_ratio": silence_ratio,
        }

    images, image_error = make_images(path, name, index_dir, args.timeout)
    if image_error:
        warnings.append({"stage": "images", "message": image_error})
    wave = {"path": images["wave"]} if images else None
    spec = {"path": images["spec"]} if images else None

    record = {
        "asset_version": ASSET_VERSION,
        "id": name,
        "source": {
            "path": relative,
            "size_bytes": stat.st_size,
            "mtime": iso_z(stat.st_mtime),
            "sha256": None,
            # Extra, beyond the contract: exact nanoseconds, so a file modified
            # twice within the same second is still noticed next run.
            "mtime_ns": stat.st_mtime_ns,
        },
        "technical": technical,
        "captured": captured,
        "analysis": analysis,
        "labels": [],
        "events": [],
        "wave": wave,
        "spec": spec,
        # Extra, beyond the contract: what these measurements depend on, so a
        # changed threshold invalidates a skip without touching the file.
        "scan": {**settings_fingerprint(args),
                 "seconds": round(time.monotonic() - started, 2)},
    }

    # A re-measure under --force or changed settings must not cost a re-label or
    # a re-transcribe, so the other halves' fields are carried over — but only
    # when the file itself is unchanged. A changed file's old labels and
    # transcript describe different audio, so they are dropped for the next
    # label/transcribe pass to replace.
    if existing is not None and same_file:
        for key in ("labels", "events", "label", "transcription", "playback"):
            if key in existing:
                record[key] = existing[key]

    return record, None, warnings


def write_json(path: Path, payload) -> None:
    """Write JSON so an interrupted write cannot leave half a file behind.

    Written under a temporary name and renamed into place, so a reader sees
    either the old file or the new one and never a fragment. allow_nan=False
    refuses a number that is not a number here instead of writing a bare NaN,
    which JSON.parse would reject for the whole record.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False,
                                    allow_nan=False) + "\n")
    os.replace(temporary, path)


def write_record(index_dir: Path, record: dict) -> str | None:
    """Write one record, returning a message if it could not be written."""
    try:
        write_json(index_dir / "audio" / f"{record['id']}.json", record)
    except ValueError as error:
        return f"record not written: {error}"
    return None


# ---------------------------------------------------------------- traversal


def find_media(root: Path, index_dir: Path) -> list[Path]:
    """Every supported file under root, in a stable order.

    The index directory is skipped — by default it sits *inside* the library, and
    a second run would otherwise find its own proxies — and so is any dot
    directory, because .git and friends are never media.
    """
    found: list[Path] = []
    index_resolved = index_dir.resolve()
    for current, directories, files in os.walk(root):
        here = Path(current)
        directories[:] = sorted(
            d for d in directories
            if not d.startswith(".") and (here / d).resolve() != index_resolved
        )
        if here.resolve() == index_resolved:
            directories[:] = []
            continue
        for name in sorted(files):
            if name.startswith("."):
                continue
            suffix = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if suffix in AUDIO_EXTENSIONS:
                found.append(here / name)
    return found


def existing_records(index_dir: Path) -> tuple[dict[str, dict], list[str]]:
    """The records already in the index, and the ids of any that would not parse.

    A record that cannot be read is reported, and because it is absent from the
    returned map the asset it belongs to counts as new and is indexed again —
    which is also what repairs it.
    """
    records: dict[str, dict] = {}
    unreadable: list[str] = []
    directory = index_dir / "audio"
    if not directory.is_dir():
        return records, unreadable
    for path in sorted(directory.glob("*.json")):
        try:
            records[path.stem] = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            unreadable.append(path.stem)
    return records, unreadable


def ffmpeg_facts() -> list[dict]:
    """The tooling and its licence, for the manifest. AGENTS.md requires committed
    generated output to record what produced it, and the licence of the decoder
    matters here: a GPL FFmpeg build is why this project is AGPL."""
    code, out, _ = run(["ffmpeg", "-version"])
    if code != 0:
        return []
    lines = out.splitlines()
    version = "unknown"
    licence = "unknown"
    if lines:
        parts = lines[0].split()
        if len(parts) > 2:
            version = parts[2]
    configuration = next((line for line in lines if line.startswith("configuration:")), "")
    if "--enable-gpl" in configuration:
        licence = "GPL-3.0-or-later" if "--enable-version3" in configuration else "GPL-2.0-or-later"
    elif configuration:
        licence = "LGPL-2.1-or-later"
    return [{"name": "ffmpeg", "version": version, "licence": licence}]


def vocabulary_hash() -> dict | None:
    source = Path(__file__).resolve().parent / "vocabulary.yaml"
    if not source.is_file():
        return None
    return {"name": "vocabulary.yaml",
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}


# ---------------------------------------------------------------- model halves

MODELS_INSTALL = """fuaim scan runs the label and transcribe halves afterwards, which live in
their own environment — this Mac's last PyTorch wheel is 2.2.2, and it stops at
Python 3.12, so they cannot share the interpreter scan runs on. Build it once:

    python3.12 -m venv ~/.venvs/fuaim-models
    ~/.venvs/fuaim-models/bin/pip install "torch==2.2.2" "numpy<2" "transformers==4.40.2" faster-whisper pyyaml

Without it, scan measures and then skips label and transcribe — or pass
--no-label --no-transcribe to scan only.
"""


def model_python() -> Path | None:
    candidate = Path(os.environ.get("FUAIM_MODELS_PYTHON") or
                     Path.home() / ".venvs" / "fuaim-models" / "bin" / "python")
    return candidate if candidate.is_file() else None


def run_models(args, index_dir: Path, reporter) -> None:
    """Run the label and transcribe halves over the fresh index.

    They are separate programs, run under the model environment's own
    interpreter as processes rather than imported, so scan keeps its
    standard-library interpreter and the halves stay replaceable. The point is
    one command that measures, labels and transcribes — while `fuaim label` and
    `fuaim transcribe` remain runnable and redoable on their own.
    """
    python = model_python()
    if python is None:
        reporter.warn("label and transcribe need the model environment — scanning only")
        sys.stdout.write(MODELS_INSTALL)
        sys.stdout.flush()
        return
    here = Path(__file__).resolve().parent
    for program, wanted, name in (("label.py", args.label, "label"),
                                  ("transcribe.py", args.transcribe, "transcribe")):
        if not wanted:
            continue
        command = [str(python), str(here / program), str(index_dir)]
        if args.limit is not None:
            command += ["--limit", str(args.limit)]
        if args.quiet:
            command.append("--quiet")
        if args.no_colour:
            command.append("--no-colour")
        code = subprocess.run(command).returncode
        if code != 0:
            reporter.warn(f"{name} stopped (exit {code}); what it wrote is kept")
            break


# ---------------------------------------------------------------- command


def resolve_index(root: Path, given: str | None) -> Path:
    if given:
        return Path(given).expanduser().resolve()
    return (root / INDEX_DIR_NAME).resolve()


def media_root_value(root: Path, index_dir: Path) -> str:
    """A relative media root wherever one is reasonable, so the index survives the
    library being moved, which is the point of rule 2. An index parked far away
    gets an absolute path instead."""
    relative = os.path.relpath(root, index_dir)
    if relative == ".":
        return "."
    if not relative.startswith(".."):
        return Path(relative).as_posix()
    if len(relative.split(os.sep)) <= 3:
        return Path(relative).as_posix()
    return str(root)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fuaim scan",
        description="Index a directory hierarchy of recordings so the browser can read it.",
        epilog="Exit status is 0 when the scan ran, 1 when it could not run at all. "
               "Use --strict to also fail when a file could not be indexed.",
    )
    parser.add_argument("dir", nargs="?", default=".",
                        help="root of the hierarchy to scan (default: the current directory)")
    parser.add_argument("--index", "--out", dest="index", default=None,
                        help=f"index directory, any name and place (default: {INDEX_DIR_NAME}/ "
                             "inside the directory being scanned)")
    parser.add_argument("--force", action="store_true",
                        help="re-scan files that have not changed")
    parser.add_argument("--prune", action="store_true",
                        help="delete records whose media file is gone")
    parser.add_argument("--dry-run", action="store_true",
                        help="list what would be done, write nothing")
    parser.add_argument("--proxy", action="store_true",
                        help="also write an m4a copy a browser can play, for recordings it cannot")
    parser.add_argument("--proxy-seconds", type=float, default=PROXY_SECONDS,
                        help="seconds of audio to copy (default: %(default)s; 0 for all of it)")
    parser.add_argument("--proxy-minutes", type=proxy_minutes, default=None, metavar="MINUTES",
                        help="minutes of audio to copy, or 'max' for no cap "
                             "(overrides --proxy-seconds)")
    parser.add_argument("--label", action=argparse.BooleanOptionalAction, default=True,
                        help="run the labeller over the index afterwards (default: yes)")
    parser.add_argument("--transcribe", action=argparse.BooleanOptionalAction, default=True,
                        help="run the transcriber over the index afterwards (default: yes)")
    parser.add_argument("--jobs", type=int, default=1,
                        help="files to work on at once (default: 1, which keeps output in order)")
    parser.add_argument("--limit", type=int, default=None, help="stop after this many files")
    parser.add_argument("--timeout", type=float, default=900.0,
                        help="seconds before one ffmpeg call is abandoned (default: 900)")
    parser.add_argument("--strict", action="store_true",
                        help="exit non-zero if any file failed to be indexed")
    parser.add_argument("--quiet", action="store_true", help="only failures and the summary")
    parser.add_argument("--verbose", action="store_true", help="also show per-file timing")
    parser.add_argument("--no-colour", "--no-color", dest="no_colour", action="store_true",
                        help="plain output, no ANSI colours")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.proxy_minutes is not None:
        args.proxy_seconds = args.proxy_minutes

    colour = Palette(colour_wanted(sys.stdout) and not args.no_colour)
    glyphs = Glyphs(unicode_ok())
    reporter = Reporter(colour, glyphs, args.verbose, args.quiet, width=38)

    for program in ("ffprobe", "ffmpeg"):
        if shutil.which(program) is None:
            sys.stderr.write(
                f"{program} is not on PATH, and the indexer cannot work without it\n")
            return 1

    root = Path(args.dir).expanduser().resolve()
    if not root.is_dir():
        sys.stderr.write(f"no such directory: {root}\n")
        return 1
    index_dir = resolve_index(root, args.index)
    if index_dir == root:
        sys.stderr.write("the index directory cannot be the directory being scanned\n")
        return 1

    if not args.dry_run:
        try:
            (index_dir / "audio").mkdir(parents=True, exist_ok=True)
            (index_dir / "wave").mkdir(parents=True, exist_ok=True)
            (index_dir / "spec").mkdir(parents=True, exist_ok=True)
            if args.proxy:
                (index_dir / "proxies").mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            sys.stderr.write(
                f"cannot create the index in {index_dir}: {exc}\n"
                "If the library is read-only, put the index elsewhere:\n"
                f"    fuaim scan {args.dir} --index ~/indexes/{root.name}\n")
            return 1

    files = find_media(root, index_dir)
    if args.limit is not None:
        files = files[:args.limit]
    if files:
        reporter.width = min(max(len(p.relative_to(root).as_posix()) for p in files), 46)
    if not files:
        reporter.header([("directory", str(root)), ("index", str(index_dir))])
        reporter.summary([("no supported files found", "yellow")])
        return 0

    records, unreadable = existing_records(index_dir)
    by_relative: dict[str, Path] = {}
    for path in files:
        by_relative[path.relative_to(root).as_posix()] = path
    current_ids = {asset_id(rel, p.stem) for rel, p in by_relative.items()}
    stale = sorted(set(records) - current_ids)

    counts: dict[str, int] = {}
    total_bytes = 0
    for path in files:
        suffix = path.suffix.lstrip(".").lower()
        counts[suffix] = counts.get(suffix, 0) + 1
        try:
            total_bytes += path.stat().st_size
        except OSError:
            pass

    if not args.quiet:
        reporter.header([
            ("directory", str(root)),
            ("index", str(index_dir)),
            ("media root", media_root_value(root, index_dir)),
            ("found", f"{len(files)} files, {human_bytes(total_bytes)}  " +
                      colour.dim(" ".join(f"{k} {v}" for k, v in sorted(counts.items())))),
            ("scan", f"version {SCAN_VERSION}, index format {INDEX_VERSION}"),
        ])

    if unreadable:
        shown = ", ".join(unreadable[:3]) + (" …" if len(unreadable) > 3 else "")
        reporter.warn(f"{len(unreadable)} record(s) in the index could not be read "
                      f"({shown}); indexing them again")

    drifted, drift = settings_drift(records, args)
    if drifted:
        changed = ", ".join(
            f"{key} {'none' if old is None else old} → {new}"
            for key, (old, new) in sorted(drift.items())[:3])
        if len(drift) > 3:
            changed += f", and {len(drift) - 3} more"
        reporter.warn(f"{drifted} of {len(records)} records were written under different "
                      f"settings ({changed}); they are measured again, not skipped")

    if args.dry_run:
        for index, path in enumerate(files, start=1):
            relative = path.relative_to(root).as_posix()
            name = asset_id(relative, path.stem)
            reporter.skipped(index, len(files), relative,
                             "would re-scan" if name in records else "would index")
        reporter.summary([(f"{len(files)} files", "bold"),
                          (f"{len(records)} already indexed", "dim"),
                          ("nothing written", "dim")])
        return 0

    started = time.monotonic()
    indexed = skipped = failed = copies = 0
    errors: list[dict] = []
    warnings: list[str] = []

    def handle(path: Path) -> tuple[str, dict | None, tuple[str, str] | None, list[dict], float, str]:
        relative = path.relative_to(root).as_posix()
        name = asset_id(relative, path.stem)
        record, error, problems = scan_one(path, root, index_dir, records.get(name), args)
        if record is None and error is None:
            # Unchanged — but its playable copy may still be missing or stale,
            # which the measurement being current says nothing about.
            existing = records.get(name)
            if existing is not None:
                updated, note, proxy_error = ensure_proxy(path, name, existing, index_dir, args)
                if proxy_error:
                    return "proxied", None, None, [{"stage": "proxy", "message": proxy_error}], 0.0, ""
                if updated is not None:
                    return "proxied", updated, None, [], 0.0, note
            return "skipped", None, None, [], 0.0, ""
        seconds = float((record or {}).get("scan", {}).get("seconds") or 0.0)
        if record is None:
            return "failed", None, error, problems, seconds, ""
        updated, note, proxy_error = ensure_proxy(path, name, record, index_dir, args)
        if proxy_error:
            problems = [*problems, {"stage": "proxy", "message": proxy_error}]
        return "indexed", (updated or record), error, problems, seconds, note

    def report(index: int, outcome: str, record: dict | None, error: tuple[str, str] | None,
               relative: str, problems: list[dict], elapsed: float, note: str = "") -> None:
        nonlocal indexed, skipped, failed, copies
        if outcome == "skipped":
            skipped += 1
            reporter.skipped(index, len(files), relative, "unchanged, not re-scanned")
            return
        if outcome == "proxied":
            copies += 1
            for problem in problems:
                errors.append({"path": relative, **problem})
            reporter.skipped(index, len(files), relative, note or "playable copy made")
            return
        if outcome == "failed":
            failed += 1
            stage, message = error or ("probe", "failed for no recorded reason")
            errors.append({"path": relative, "stage": stage, "message": message})
            reporter.failed(index, len(files), relative, message)
            return
        indexed += 1
        assert record is not None
        for problem in problems:
            errors.append({"path": relative, **problem})
        if note:
            copies += 1
        technical = record["technical"]
        detail = (f"{colour.dim((technical.get('codec') or '--').ljust(10))} "
                  f"{human_duration(technical.get('duration_s')):>7}  "
                  f"{colour.dim(f'{human_duration(elapsed):>7}')}")
        if note:
            detail += "  " + colour.dim(note)
        reporter.done(index, len(files), relative, detail)

    def write_record_or_report(relative: str, record: dict) -> None:
        problem = write_record(index_dir, record)
        if problem:
            errors.append({"path": relative, "stage": "write", "message": problem})
            reporter.warn(f"{relative}: {problem}")

    if args.jobs > 1:
        pool = ThreadPoolExecutor(max_workers=args.jobs)
        try:
            pending = {pool.submit(handle, path): path for path in files}
            for done, future in enumerate(as_completed(pending), start=1):
                path = pending.pop(future)
                relative = path.relative_to(root).as_posix()
                outcome, record, error, problems, seconds, note = future.result()
                report(done, outcome, record, error, relative, problems, seconds, note)
                if record is not None:
                    write_record_or_report(relative, record)
        except BaseException:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            pool.shutdown(wait=True)
    else:
        for index, path in enumerate(files, start=1):
            relative = path.relative_to(root).as_posix()
            reporter.working(index, len(files), relative)
            outcome, record, error, problems, seconds, note = handle(path)
            report(index, outcome, record, error, relative, problems, seconds, note)
            if record is not None:
                write_record_or_report(relative, record)

    pruned: list[str] = []
    for name in stale:
        record = records[name]
        path = index_dir / "audio" / f"{name}.json"
        if args.prune:
            path.unlink(missing_ok=True)
            for key in ("wave", "spec", "playback"):
                item = record.get(key) or {}
                item_path = index_dir / (item.get("path") or "")
                if item_path.is_file():
                    item_path.unlink()
            pruned.append(name)
        else:
            warnings.append(record.get("source", {}).get("path", name))

    manifest = {
        "index_version": INDEX_VERSION,
        "generated": iso_z(time.time()),
        "media_root": media_root_value(root, index_dir),
        "asset_count": len(list((index_dir / "audio").glob("*.json"))),
        "generator": {"name": "scan.py", "version": SCAN_VERSION},
        "models": ffmpeg_facts(),
        # `assets` is deliberately absent. The browser then lists audio/ for
        # itself, which is also what makes an interrupted scan harmless: the
        # records already written are still found, with no manifest to fall out
        # of step.
    }
    vocabulary = vocabulary_hash()
    if vocabulary:
        manifest["vocabulary"] = vocabulary
    if errors:
        manifest["errors"] = errors
    write_json(index_dir / "manifest.json", manifest)

    elapsed = time.monotonic() - started
    if warnings:
        shown = ", ".join(warnings[:4]) + (" …" if len(warnings) > 4 else "")
        reporter.warn(f"{len(warnings)} record(s) no longer match a file: {shown}")
        reporter.warn("re-run with --prune to delete them")
    reporter.summary([
        (f"scanned {indexed}", "green" if indexed else "dim"),
        (f"skipped {skipped}", "dim"),
        (f"failed {failed}", "red" if failed else "dim"),
        *([(f"{copies} copies", "cyan" if copies else "dim")] if args.proxy else []),
        (f"{human_duration(elapsed)}", "dim"),
    ])
    if not args.quiet:
        reporter.note(f"index {index_dir}")
    if not args.dry_run and (args.label or args.transcribe):
        run_models(args, index_dir, reporter)
    # A file that could not be indexed is reported and recorded, but it does not
    # make the run a failure: a library with one corrupt file would otherwise fail
    # every scan for ever. --strict is there for callers that want the opposite.
    return 1 if (args.strict and failed) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        stop_children()
        print("\nscan interrupted: the records already written are kept, so "
              "re-running the same command finishes the rest",
              file=sys.stderr, flush=True)
        sys.exit(130)
