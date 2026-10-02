# AGENTS.md

Instructions for any human or AI agent working in this repository.

`fuaim` is a tool for analysing, classifying and transcribing audio recordings,
and for searching the spoken words and the sounds in them, so that a library of
recordings can be browsed and searched by what is in it instead of by filename.

The project is called **fuaim** (Irish for *sound*). The word carries no
síneadh fada, so there is nothing to strip: file names, commands and identifiers
are ASCII throughout — `fuaim.py`, the `fuaim` command, the `fuaim-index`
directory — because accents in executable names and paths are a portability tax
that buys nothing.

## Status

**The scaffold is up; the indexer and the browser are written.** `fuaim.py`
dispatches and `install.sh` installs. `scan.py` is implemented: it walks a
hierarchy, probes with `ffprobe`, measures levels and silence, generates the
waveform and spectrogram, writes the index, and with `--proxy` writes playable
copies. `browse.py` is implemented: it serves the index as a grid of waveform
cards, filterable by when, what and how long, searchable over words, labels,
filenames and metadata, with a detail view showing the spectrogram, sound-event
timeline and transcript. `label.py` and `transcribe.py` are still stubs that
parse their arguments and say so. This file is the specification, written before
any working code, and it is the contract the first implementation is held to. It
mirrors the
físeán project in `../finsean`, which is the video analogue of this tool:
`finsean/scan.py` measures videos and extracts stills, `finsean/label.py` asks a
vision-language model what is in them, and `finsean/browse.py` presents the
result as a browsable, searchable library. `fuaim` is the same shape, for sound
instead of picture.

The library to be indexed is listed in [`audio-files.txt`](audio-files.txt):
about 230 recordings under `/Users/paul/Dropbox/Recordings/` — one person's
library, so its paths are absolute and it is **not** committed media. The list
also contains a handful of non-audio files (`.DS_Store`, a `.pdf`, `w.txt`, a
`.png`), which the scan silently skips.

## What the tool is for

- **Analyse** each recording: measure its technical metadata (container, codec,
  sample rate, channels, bitrate), its duration, its capture date where the file
  carries one, and its audio levels.
- **Represent** it: generate a waveform image as the recording's cover and a
  spectrogram for its detail view, so the library is browsed by sight rather
  than by filename.
- **Classify** it: assign each recording to categories — singing, music, talk,
  noise, nature, birds, cars, and so on — enough to list the library by type.
- **Transcribe** it: run speech-to-text over the recording so that spoken words
  are searchable, not only the sounds.
- **Browse and search**: present the library through those waveforms, labels and
  transcripts in a web app, filterable by date, by type and by length, with free
  text search across transcribed words, labels, filenames and metadata.

## Architecture — two parts, one contract

The tool is two programs, not one, and the split is a requirement rather than a
layout preference: either half must be replaceable without rewriting the other.

1. **Indexer** — walks a directory hierarchy offline and writes an *index*:
   metadata, measurements, the waveform and spectrogram images, labels, a
   sound-event timeline and a transcript.
2. **Browser** — a web app that reads **only the index** and presents the
   library.

Rules that keep the seam honest:

- The browser never invokes `ffmpeg` and never decodes audio. It does not read
  the media directory either, with **one documented exception**: serving the
  original bytes for playback, read-only. If it needs a *fact*, that fact belongs
  in the index.
- The indexer serves no HTTP and owns no UI.
- **The entry point dispatches; it does not implement.** `fuaim.py` maps a
  subcommand to a program and `exec`s it, so signals, exit codes and output are
  the command's own, and no half can creep into the other by way of a shared
  entry point. A later tool is a new subcommand, not a new branch inside the
  browser.
- The index format is a **versioned, documented contract** —
  [docs/index-format.md](docs/index-format.md), which both halves are held to.
  Letting indexer internals leak into the browser is exactly what would cost us
  the freedom to swap the backend.
- Re-indexing one file must not require touching the others.
- **The index lives beside the library by default** — `<DIR>/fuaim-index` — so one
  path serves both commands and the index travels with the media it describes.
  `--index` puts it anywhere, under any name. The media is still never written
  to; a library on a read-only disk cannot hold its own index, and the scan says
  so and names the flag rather than failing obscurely.

## Requirements

Numbered, as agreed, so that a later change can be checked against them.

### Indexer — `fuaim scan`

1. Runs over a directory hierarchy of recordings, offline, with no network
   access at index time (model weights are fetched beforehand, once).
2. Measures each recording: container, codec, sample rate, channels, bitrate,
   duration, capture date where carried, and audio levels.
3. Generates the two representation images: a waveform as the cover and a
   spectrogram for the detail view.
4. Leaves the source files untouched, and writes its output where it is told:
   `<DIR>/fuaim-index` by default, or `--index` anywhere else.
5. Handles the target formats: `wav`, `amr`, `m4a`, `3ga`, `mpeg`, `mp3`,
   `aac`, `mp2`, `ac3`.
6. Scans a hierarchy recursively and shows progress as it goes, saying which
   files it indexed, which it left alone and which it could not read. Files
   whose extension is not in the target set are **silently skipped** — not
   recorded as errors, because a folder of recordings legitimately contains a
   `.DS_Store`, a receipt `.pdf`, a `.txt` note or a `.png`.
7. Skips files that have not changed since the last scan and re-indexes the ones
   that have. `--force` re-indexes everything regardless.
8. Records the duration of each recording, which the browser shows on every
   card.
9. Survives a file it cannot read: the failure is recorded in the manifest's
   `errors`, the rest of the scan carries on, and the library stays browsable.

### Indexer — `fuaim label`

10. Asks a zero-shot audio-language model (CLAP) how well each phrase in
    `vocabulary.yaml` fits the recording, sampling a small number of fixed-length
    windows, and writes the phrases that survive as **whole-file labels**, each
    carrying its model, its score and how many windows agreed. Where the
    per-window scores support it, it also writes a **sound-event timeline** —
    events with `at_s` and `at_e` — so a recording that is birds for half a
    minute and then talk is labelled at the right points, not only as a whole.

### Indexer — `fuaim transcribe`

11. Runs speech-to-text (Whisper) over each recording and writes a full
    transcript with per-segment timestamps, so spoken words are searchable and
    each segment can be jumped to. Transcription writes transcripts and nothing
    else, so it never costs a re-scan or a re-label.

### Browser

12. A browser-based web app over the index.
13. Filters and lists the library along three axes — **when** (date), **what**
    (sound category), and **how long** (a min–max duration slider) — plus
    **free-text search** over transcribed words, sound labels, filenames and
    metadata. A range over a measurement is not a fourth axis, which is why
    duration is listed with the axes but not as one of them.
14. Shows the waveforms as the primary browsing surface — a grid of waveform
    cards — and looks good doing it. The detail view shows the spectrogram, the
    sound-event timeline and the transcript, with the timeline and transcript
    clickable to seek playback.
15. Plays a recording where the browser can decode it, and otherwise offers a
    one-click **copy of the full path** so it can be opened in a player. For
    formats a browser cannot decode, the scan can write a small playback proxy
    (an `mp3` or `m4a` copy inside the index), exactly as físeán's `--proxy`
    does.
16. Reads only the index, save for the one documented playback exception.

### Non-goals

- **Video analysis.** The inverse of físeán's audio non-goal: if a file carries a
  video stream (an `.mpeg` or a `.3gp`), the picture is ignored and only the
  audio track is indexed. What a recording *sounds* like is the point; what it
  looks like is not.
- **Speaker diarisation.** Transcription says *what* was said, not *who* said it.
- **Music transcription.** Pitch, key, chords and melody are out of scope; a
  recording is "music" or "trumpet", not a score.
- **Embedded cover art.** The cover is a generated waveform, not the picture the
  file's tags may carry.
- Editing, transcoding or de-duplicating the source media — the only transcoding
  is the playback proxy, and that writes inside the index, never to the source.

## Open questions

The decisions above are settled; what remains open:

- **Whisper model size and language.** `base` multilingual is the natural
  default; `small` for better accuracy, `tiny` for speed, or an English-only
  model if the library proves to be English throughout. This is measured over the
  real library before it is pinned.
- **Whisper runtime.** `faster-whisper` (MIT, CTranslate2) is the likely choice
  for CPU transcription on this machine; `openai/whisper` and `whisper.cpp` are
  the alternatives. None is adopted until one is run over the real files.
- **CLAP checkpoint.** `laion/clap-htsat-fused` (Apache-2.0) is the recommended
  start; MS-CLAP 2023 (MS-PL) is the alternative. See the candidate stack.
- **How many windows per recording** for classification — físeán settled on a
  dozen frames per video; the audio analogue is a handful of fixed-length
  windows, and the number is a measurement decision, not a guess.
- **Waveform and spectrogram size and palette.** Fixed width, so cards align; a
  log-frequency spectrogram; a palette that reads at a glance.
- **Whether transcription runs over silence.** Voice-activity detection to skip
  the quiet stretches, or transcribe everything and let the browser show where
  the words are.
- **Label thresholds.** The values in `vocabulary.yaml` are starting guesses;
  running one batch over the real library and looking at where the scores
  actually fall is the obvious next step.
- **Which formats need a playback proxy.** Recorded as "measure, don't assume"
  in Target formats; the mechanism is the same as físeán's.
- **Place names.** Audio rarely carries GPS, so there is no "where" facet; if a
  file ever does carry coordinates, a name still needs a dataset or a network
  call, and the indexer is offline.

## The index contract — decisions recorded

- **The generated index is JSON, not YAML.** The browser reads it with
  `JSON.parse`, so the front end keeps zero dependencies; YAML would put a parser
  inside the browser half. JSON also has no indentation traps and no implicit
  typing surprises.
- **YAML for what a human edits**: `vocabulary.yaml` (labels — project source,
  in this repo) and `overrides.yaml` (corrections — kept beside the index,
  outside the repo, because it is one person's library data and may name people).
- **One JSON file per asset, plus a manifest**, so re-indexing one recording
  rewrites one file and the counters, never its neighbours.
- **The index stores measurements and labels; the browser derives facets.**
  "Silent", "quiet", "loud", "mostly speech" are thresholds over numbers, so
  re-tuning them must not require a re-index.
- **The `media_root` is the only path in the index that may be absolute**, and it
  may be relative, resolved against the index directory — which is what lets a
  committed fixture work after being cloned anywhere. So an index survives the
  library being moved.
- **`null` means "not known", never "probably not".** A field that could not be
  measured is `null`. No defaults that look like data.
- **Every derived fact carries provenance**: which model, which version, which
  source. Two classifiers may both have an opinion about one recording, and a
  later run may add a third.
- **The waveform and the spectrogram are generation, not interpretation**, and
  live under the index — `wave/<id>.png` and `spec/<id>.png` — served the way
  físeán serves stills. The cover is therefore always the waveform, stated
  rather than implied.
- **`events` is a timeline and whole-file labels are separate.** Events are in
  ascending `at_s` and carry `at_e`; a recording's category is its set of
  whole-file labels. A hand correction for either form comes from
  `overrides.yaml`.
- **Playback is expected to fail for part of the library** (browsers do not
  decode AMR, 3GA, MP2, AC-3 or MPEG audio), which is why copy-full-path is a
  primary action rather than a fallback, and why `scan --proxy` exists.
- **The browser resolves what the index cannot state.** The absolute media path
  is derived from `media_root` and served to the interface at `/config.json`;
  asset ids come from `manifest.assets` or from listing `audio/`. Both are
  browser-half behaviour, and `/config.json` is deliberately *not* part of the
  format: a derived value must not be stored twice.
- **Asset ids are stable and filesystem-safe**: the first 8 hex characters of the
  SHA-256 of the media-root-relative path, then a slug from the file name.
  **Known consequence:** renaming or moving a file produces a new id, and any
  `overrides.yaml` entry keyed to the old id is lost. Recorded rather than
  fixed, because content hashing would change the id every time a file is
  re-encoded, which is worse.
- **The browser half is to be built against a fixture index**, before the
  indexer exists, so that the contract is proved implementable first — the same
  way físeán's was. Everything it shows is invented audio until the indexer
  replaces the fixture.

## Target formats

All nine extensions are supported by FFmpeg. Verified against FFmpeg 8.1.2 on
this machine, 2026-10-02 — from its demuxer and decoder tables, not assumed:

| Extension | FFmpeg demuxer | Typical codec inside | Note |
| --- | --- | --- | --- |
| `wav` | `wav` | `pcm_*` | lossless PCM, so the largest files |
| `amr` | `amr` (3GPP AMR) | `amrnb` / `amrwb` | 8 kHz narrowband speech, typically |
| `m4a` | `mov,mp4,m4a,3gp,3g2,mj2` | `aac` (or `alac`) | MPEG-4 audio in an MP4 container |
| `3ga` | the same `mov` demuxer | `aac` or `amr` | 3GPP audio container — the audio-only form of `3gp` |
| `mpeg` | `mpeg` (MPEG-PS) | `mp2`/`mp3` — may also carry video | a program stream; the audio track is what is indexed |
| `mp3` | `mp3` | `mp3` | the `mp3` demuxer also reads `mp2` |
| `aac` | `aac` (raw ADTS) | `aac` | raw AAC, no container |
| `mp2` | `mp3` | `mp2` | MPEG audio layer 2 |
| `ac3` | `ac3` | `ac3` | raw AC-3 |

**What a browser plays** is to be measured over the real library, not assumed —
the físeán note applies here verbatim: `canPlayType()` is not a reliable guide,
and only loading real files settles it. The expectation, recorded to be
checked: browsers play `wav`, `mp3` and `m4a`/`aac`, and refuse `amr`, `3ga`,
`mp2`, `ac3`, `mpeg` and raw `aac` — so `scan --proxy` writes an `mp3`/`m4a`
copy for those, capped in length by default exactly as físeán's proxy is.

Two traps to know before writing any of this:

- **AMR is speech-grade, not music-grade.** 8 kHz narrowband AMR is a telephone
  codec; a waveform and spectrogram of it will not show the detail a `wav` shows.
  The representation images must be generated from the decoded audio, never
  assumed to be lossless.
- **A `3ga`/`mpeg`/`mp4` file can carry a video stream.** Anything downstream
  that assumes audio only must handle a file with both — by indexing the audio
  track and ignoring the picture, per the non-goals, not by failing.

## Prior art — surveyed

Nothing here is adopted yet beyond the plan. This records what was looked at and
why none of it is the whole answer; licences are from the projects' own pages.

| Tool | Licence | What it gives us | Why it is not the answer |
| --- | --- | --- | --- |
| FFmpeg / ffprobe | GPL-3.0-or-later (this build) | decoding, probing, `volumedetect`, `silencedetect`, `astats`, `showwaves`, `showspectrumpic` | a library, not a tool: the substrate everything else sits on |
| Whisper (openai) | MIT code, Apache-2.0 weights | speech transcription with timestamps | transcription only — no sound labels, no index, no UI |
| faster-whisper | MIT | the same transcription, CTranslate2-fast on CPU | as above |
| whisper.cpp | MIT | the same, in C++ with no Python ML stack | as above |
| LAION CLAP | Apache-2.0 | zero-shot audio classification against a text vocabulary — the CLIP analogue, the físeán approach made sound | a model, not a tool: everything else is built around it |
| MS-CLAP | MS-PL | the stronger 2023 CLAP weights, plus captioning | as above; permissive but a different licence text |
| YAMNet | Apache-2.0 | AudioSet's 521 fixed classes, tiny and fast | a fixed label set — cannot be edited to "sam's piano" or "tin whistle" |
| PANNs | MIT | AudioSet classifiers | as above; a fixed vocabulary is not our classification scheme |
| BirdNET | — | bird-call identification | one species, where "birds" in the vocabulary is enough; adds a specialist model for little gain |
| Immich / PhotoPrism | AGPL-3.0 | the closest thing to the browser half, but for photos and video | no audio search; brings a server, Postgres and their schema |

## Licence

**AGPL-3.0-or-later**, chosen 2026-10-02. See [LICENSE](LICENSE).

This repo shells out to `/usr/local/bin/ffmpeg`, a GPL-3.0-or-later build, as a
**separate process** — it does not link it. The sibling repos `videos` and
`finsean` chose AGPL-3.0-or-later because of that same GPL build, and `fuaim`
follows them: one licence across the three tools, and AGPL (rather than GPL)
keeps AGPL projects such as Immich and PhotoPrism reusable as prior art. The
choice is deliberate and documented so it is not silently revisited.

The models the indexer uses at run time are permissively licensed — Whisper
(Apache-2.0 weights, MIT code), LAION CLAP (Apache-2.0), faster-whisper and
whisper.cpp (MIT) — all compatible with AGPL, so none of them pulls the repo off
it.

## Candidate stack — partly chosen

What is decided and what is still being measured:

- **FFmpeg 8.1.2** at `/usr/local/bin/ffmpeg` — verified present. It covers all
  nine target extensions, and its filters already include the whole
  measurement-and-image toolkit: `volumedetect` and `astats` (levels),
  `silencedetect` (silence ratio), `showwaves` (the waveform cover) and
  `showspectrumpic` (the spectrogram). Generation is therefore a filter call,
  not a plotting library.
- **`ffprobe` is present; `mediainfo` and `exiftool` are not installed.** Any
  metadata plan built on the latter two adds a dependency to install and to
  document, which is worth knowing before it is designed in.
- **Zero-shot labels: adopted.** CLAP through `transformers`,
  `laion/clap-htsat-fused` (Apache-2.0), scoring a hand-editable
  `vocabulary.yaml` — the same loop físeán runs with CLIP. The vocabulary groups
  are the audio answer to "who & what": `sound type` (singing, music, talk,
  noise), `nature` (birds, bees, gulls, rain, wind), `place` (indoors, outdoors,
  traffic, crowd, kitchen), `instrument` (piano, trumpet, oboe, flute, tin
  whistle, guitar) and `other` (laughter, bells, door, fridge).
- **Transcription: adopted, runtime undecided.** Whisper is the model;
  `faster-whisper` is the likely CPU runtime. Model size is an open question
  above.
- **Model weights are a one-time download.** Requirement 1 says the indexer runs
  offline, so that means *no network at index time*: weights are fetched
  beforehand, once, and cached outside the repository (`~/.cache`), so no ignore
  pattern is needed for them.
- **The model-using commands keep their dependencies out of the interpreter the
  rest of the project uses.** This machine's PyTorch stops at torch 2.2.2 /
  Python 3.12 (the same constraint físeán documents), so `label` and
  `transcribe` run in their own environment, and `scan` and `browse` stay
  standard-library. Whether the two model commands share one environment or each
  keeps its own is an implementation detail to settle when the venv layout is
  written.

## Keeping this file current — required

Whenever a new language, library, framework, service or major dependency is
introduced to this repository, **update this AGENTS.md file in the same change**
with:

- **What was added and why** — including the alternatives that were rejected and
  the reason, so the decision is not silently revisited.
- A **Best Practices** sub-section for that technology: idiomatic usage,
  project-specific conventions, and links to authoritative documentation.
- Any new **install / build / run / test / lint** commands, under
  "Development Commands".
- Standard ignore patterns for it in **`.gitignore`** — see the next section,
  which applies to every change, not only to new technologies.

Do not let this file go stale — it is the source of truth for how to work in this
repo. If you are unsure of the current best practice for a technology, check the
official documentation before writing the section rather than relying on
possibly outdated knowledge.

If a note in this file later turns out to be **wrong**, correct it in place and
say so explicitly. A silently deleted wrong theory gets reintroduced; a
corrected one with an explanation does not.

### Technology Stack entry format

```markdown
### <Technology Name>

- **Role**: what it is used for in this project.
- **Version**: pinned or minimum version, and what it is pinned to.
- **Best Practices**:
  - ...
- **Docs**: link(s) to official documentation.
```

## Keeping `.gitignore` current — required

`.gitignore` is part of a change, never a follow-up to it. When a change creates
files that should not be committed, the patterns go in **the same commit** — a
generated waveform committed by accident is much harder to remove than a line is
to add. (No `.gitignore` exists yet because no code exists yet; the first change
that generates artefacts creates it.)

Keep current, at minimum:

- **Tool and dependency output**: build directories, package caches, virtual
  environments, downloaded models and sample media, generated index files and
  the waveform/spectrogram/proxy images inside them.
- **Local state**: config and env files (`*.env`, `.env.local`), log files, and
  anything naming this machine or a user's home directory.
- **Editor and OS metadata**: `.DS_Store`, `.vscode/`, `.idea/`, `*.swp`.

Rules:

- Prefer the official patterns published for the technology over invented ones;
  check the tool's own documentation rather than guessing.
- Group patterns by reason, each under a comment saying which tool or platform it
  belongs to, so the next reader can tell what a pattern is for.
- **Never** ignore editor tooling by reflex where the team may want it shared: an
  ignore rule is a commitment, and adding an un-ignore afterwards (`!.vscode/…`)
  is confusing. Ignore the specific local artefacts, not the whole directory, when
  that distinction matters.
- **Never commit secrets** — ignore the file that holds them. If one is committed
  by accident, say so in the commit that removes it rather than deleting it
  quietly, because the history still has it.
- The failure to watch for is the opposite of the usual one: ignoring a file the
  tool needs at run time. If it is needed to run or to build, it is source and it
  belongs in git.

## Layout

**Correction (2026-10-02):** the plan was the browser and its fixture first, so
that the contract is proved before the indexer exists. The order was reversed by
direction — `scan.py` was written first, and the browser was built against the
fixture afterwards. The layout, with `scan.py`, `browse.py` and the fixture now
implemented and `label.py` and `transcribe.py` still to come:

| Path | What it is |
| --- | --- |
| `fuaim.py` | the `fuaim` command: one entry point that dispatches to the halves, and implements neither |
| `browse.py` | the browser half: CLI and HTTP server, nothing else |
| `scan.py` | the indexer: walks a hierarchy, probes with `ffprobe`, measures with FFmpeg filters, generates the waveform and spectrogram, writes the index |
| `label.py` | one half of the indexer: asks CLAP what is in each recording and writes `labels` and `events`. Runs in its own environment |
| `transcribe.py` | the other half: runs Whisper and writes `transcription`. Runs in its own environment |
| `static/` | its interface — `index.html`, `app.css`, `app.js`, `icon.svg`, `favicon.ico`, served from disk |
| `vocabulary.yaml` | the label vocabulary — project source, human-edited |
| `docs/index-format.md` | the index contract between indexer and browser |
| `install.sh` | symlinks `fuaim` into `$BIN` (default `~/bin`) and checks that it runs |
| `fixtures/` | the synthetic library the browser is developed against, before a real scan exists |
| `audio-files.txt` | the list of real library paths the scan is built to handle |

## AI contributions

AI-assisted work in this repo follows the convention used across
`/Users/paul/github/pftylr/`: every AI-assisted commit ends with an
`Assisted-by:` trailer — e.g. `Assisted-by: GitHub Copilot (DeepSeek V4 Flash)`,
one trailer per assistant — and AI contributions to prose documents and release
notes carry the same attribution in the text.
