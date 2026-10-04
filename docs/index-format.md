# Index format — version 1

The contract between the **indexer** and the **browser**. Either half may be
replaced by a different implementation as long as this document is honoured; the
browser must never need to know how the index was produced, and must never need
to know what an audio file is.

**Status: draft.** This document and `manifest.json`'s `index_version` move
together. Version 1 is not frozen until the indexer exists and has been run over
a real library; until then, treat field names as provisional and say so in the
commit that changes them.

## 1. Rules the format exists to enforce

1. **The browser reads the index, and nothing else.** The single exception is
   playback — see section 6 — and it is narrowly drawn: bytes are served, never
   decoded. Star ratings and the viewer's own titles and descriptions are not
   part of the contract: the browser keeps its own `ratings.json` and
   `notes.json` beside the index, and the record files never carry them.
2. **`media_root` is the only path in the index that may be absolute**, and it may
   also be relative — resolved against the index directory — so an index can
   travel with its media. Everything else is relative to it, with POSIX
   separators, so an index survives the library being moved or copied.
3. **`null` means "not known", never "probably not".** A field that could not be
   measured is `null`. No defaults that look like data.
4. **Every derived fact carries provenance**: which model, which version, which
   source. Two classifiers may both have an opinion about one recording, and a
   later run may add a third.
5. **The index stores measurements and labels; the browser derives facets.**
   "Silent", "loud" and "mostly speech" are thresholds over numbers, and a
   threshold is a presentation decision — so it belongs in the browser, where it
   can be changed without a re-index.
6. **One file per asset**, plus a manifest. Re-indexing a single recording
   rewrites that one file and the manifest's counters. It never rewrites its
   neighbours.
7. **Additions are safe, changes are not.** The browser ignores fields it does
   not recognise, so the indexer may write extra fields within version 1. The
   browser refuses any `index_version` whose major number it does not know.
8. **Generated artefacts record their generator.** `manifest.generator` and
   `manifest.models` name the tooling and model versions that produced the index,
   which is both the reproducibility record and the licence attribution AGENTS.md
   requires of committed generated output.

## 2. Where the index lives

Beside the library it describes, in a directory named `fuaim-index`:

```
<library>/
├── Trumpet_1.wav
└── fuaim-index/
    ├── manifest.json
    ├── overrides.yaml      # hand-edited; see below
    ├── ratings.json        # written by the browser; see below
    ├── notes.json          # written by the browser; see below
    ├── wave/<id>.png       # the waveform cover
    ├── spec/<id>.png       # the spectrogram
    ├── proxies/<id>.mp3    # optional playback copies
    └── audio/<id>.json
```

The media itself is never written to; the index directory is a sibling of it.
`fuaim scan <DIR> --index <SOMEWHERE>` puts the index anywhere else, under any
name — which is what a library on a read-only disk needs, and the scan says so
rather than failing obscurely when it cannot write. Both commands accept either
the library root or the index directory, so the same path works for both.

`overrides.yaml` lives **here, not in the repo**: it is a correction list for one
person's library, it may name people, and it is not project source. The label
vocabulary is project source and does live in the repo (`vocabulary.yaml`).
Nothing reads `overrides.yaml` yet.

`ratings.json` is one of two files the browser writes, and neither is part of
the index contract: a map from asset id to a star rating of 1–5 (0 is unrated
and stored as an absent key). Ratings are the viewer's, not the library's, so
they stay out of the record files — which `fuaim scan` rewrites without knowing
or caring about them — and out of `overrides.yaml`, which is hand-edited. The
same consequence as with `overrides.yaml` applies: renaming or moving a file
produces a new id, and its rating is orphaned.

`notes.json` is the other browser write, under the same rule: a map from asset
id to `{"title": …, "description": …}` — the viewer's own name and notes for a
recording, shown in place of the file name and searchable. They live beside the
index for the same reason ratings do: a rescan rewrites record files, and the
viewer's words must survive it.

## 3. `manifest.json`

| Field | Type | Notes |
| --- | --- | --- |
| `index_version` | integer, required | major number only; `1` |
| `generated` | string, required | ISO 8601 UTC |
| `media_root` | string, required | absolute path as the indexer saw it; the one exception to rule 2 |
| `asset_count` | integer, required | number of `audio/*.json` files |
| `generator` | object, required | `{ "name", "version" }` — no host names, no user names |
| `models` | array, required | `{ "name", "version", "licence" }` per model used |
| `vocabulary` | object, optional | `{ "name": "vocabulary.yaml", "sha256": "…" }` |
| `errors` | array, optional | per-asset failures: `{ "path", "stage", "message" }`. An unreadable file must be recorded, not silently dropped |
| `assets` | array of strings, optional | asset ids. When absent, the browser lists `audio/` for `*.json` instead |

Two things the browser needs that are deliberately **not** in the index. Asset
ids come from `assets` above or from listing `audio/`. And the absolute media
path — needed to build a path for the clipboard, since `media_root` may be
relative — is resolved by the browser half and served to its own interface at
`/config.json`. That endpoint is part of the browser, not the format.

## 4. `audio/<id>.json`

`id` is stable and filesystem-safe: the first 8 hex characters of the SHA-256 of
the media-root-relative path, then a slug from the file name — for example
`3f9a1c07-trumpet_1`. **Known consequence:** renaming or moving a file produces
a new id, and any `overrides.yaml` entry keyed to the old id is lost. Recorded
rather than fixed, because content hashing would change the id every time a file
is re-encoded, which is worse.

```jsonc
{
  "asset_version": 1,
  "id": "3f9a1c07-trumpet_1",

  "source": {
    "path": "Trumpet_1.wav",          // relative to media_root, POSIX separators
    "size_bytes": 12345678,
    "mtime": "2026-09-28T14:03:11Z",
    "sha256": null                    // optional; null when not computed
  },

  "technical": {
    "container": "wav",
    "codec": "pcm_s16le",
    "sample_rate": 44100,
    "channels": 2,
    "bit_rate": 1411200,
    "duration_s": 42.3
  },

  "captured": {
    "at": "2026-09-28T14:03:11Z",
    "at_source": "file_mtime",        // see below — never assume metadata exists
    "device": null
  },

  "analysis": {
    "peak_dbfs": -1.2,
    "rms_dbfs": -14.6,
    "silence_ratio": 0.04,
    "speech_ratio": 0.62              // from transcription segments, when present
  },

  "labels": [
    { "text": "trumpet", "group": "instrument", "source": "zero_shot",
      "model": "laion/clap-htsat-fused", "score": 0.91, "windows": 4, "weak": false }
  ],

  "events": [
    { "text": "birds", "group": "nature", "source": "zero_shot",
      "model": "laion/clap-htsat-fused", "at_s": 12.3, "at_e": 40.1, "score": 0.88 }
  ],

  "label": { "version": 1, "model": "laion/clap-htsat-fused",
             "vocabulary": "5bb2eeb0…", "windows": 8, "at": "2026-10-02T19:05:07Z" },

  "transcription": {
    "model": "whisper-base", "language": "en", "at": "2026-10-02T19:06:11Z",
    "text": "…",
    "segments": [ { "at_s": 0.0, "at_e": 4.2, "text": "…" } ]
  },

  "wave": { "path": "wave/3f9a1c07-trumpet_1.png" },
  "spec": { "path": "spec/3f9a1c07-trumpet_1.png" },

  "playback": { }                    // optional, see section 6
}
```

### `captured.at_source`

| Value | Meaning |
| --- | --- |
| `container_metadata` | from the file's own tags; trustworthy |
| `filename` | parsed from a date in the file name (phones and voice memos do this) |
| `file_mtime` | last resort. **Wrong for files that were copied or restored from backup** — the browser should show this date as approximate |
| `user_override` | from `overrides.yaml` |
| `unknown` | `at` is then `null` |

Legacy `amr`, `3ga` and `mpeg` files usually carry no capture date at all, which
is why this field exists rather than a bare timestamp.

### `labels[].source`

`zero_shot` (a text label asked of CLAP), `metadata` or `filename` (derived from
the container or the name), `user` (from `overrides.yaml`). `user` always wins
over a machine label with the same `text`.

`weak: true` marks a label that must never be used as a hard filter. Weak labels
are shown in the interface with that caveat and are opt-in when filtering.

### `label` — how a record's labels were produced

Written by `fuaim label`, and the only field that command touches. **Labelling
takes no measurement and invalidates none**: it changes no `analysis`, no images
and no `transcription`, so re-running it after editing `vocabulary.yaml` costs a
labelling pass and never a re-scan. `model` and `vocabulary` together are the
skip test: a record whose `label` block already names this model and this
vocabulary hash is left alone, and `--force` re-runs it.

### `transcription` — how a record's transcript was produced

Written by `fuaim transcribe`, and the only field that command touches. The
`segments` array is ascending by `at_s`; `text` is the joined transcript. A
record with no `transcription` block has simply never been transcribed, which is
not the same as being silent.

### `events` vs `labels`

`events` is a timeline and whole-file labels are separate: events are ascending
by `at_s` and carry `at_e`; a recording's category is its set of whole-file
labels. A hand correction for either form comes from `overrides.yaml`.

## 5. The representation images

The waveform and the spectrogram are generation, not interpretation, and are
produced by `fuaim scan` from the decoded audio — never assumed to be lossless,
because AMR and the compressed formats are not. The waveform is the cover, which
is why it is stated (`wave`) rather than implied by position. Their size and
palette are presentation decisions left open in AGENTS.md.

## 6. Playback

A browser can decode `wav`, `mp3` and `m4a`/`aac`, and is expected to refuse
`amr`, `3ga`, `mp2`, `ac3`, `mpeg` and raw `aac` — to be verified over the real
library, not assumed. For the refused formats, `fuaim scan --proxy` writes an
`mp3`/`m4a` copy inside the index:

```jsonc
"playback": {
  "path": "proxies/3f9a1c07-trumpet_1.mp3",   // relative to the index directory
  "seconds": 60.0,                            // length of the copy; 0 was all of it
  "codec": "mp3",
  "bytes": 12984320,
  "source_mtime_ns": 1472303766000000000,     // what this copy was made from
  "source_size_bytes": 8849624
}
```

`path` is served from the index directory, at `/index/proxies/<id>.mp3`, so the
browser needs no new route and the media directory stays read-only. The two
source fields make the copy a cache: it is rebuilt when the source changed, when
the copy's own settings changed, or under `--force`, and otherwise costs nothing.
