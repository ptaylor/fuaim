<img src="static/icon.svg" width="76" height="76" alt="">

# fuaim

Analyse, classify and transcribe audio recordings, and search the words and
sounds in them, so that a library of recordings can be browsed by what is in it
instead of by filename. *Fuaim* is Irish for *sound*.

## Status

**The indexer and the browser work; the models do not.** `fuaim scan DIR`
indexes the recordings under `DIR` — technical metadata, levels, silence, and a
waveform (cover) and spectrogram (detail) per recording — and `fuaim browse DIR`
serves that index as a grid of waveform cards, filterable by when, what and how
long and searchable over words, labels, filenames and metadata. `fuaim label`
and `fuaim transcribe` are not written yet, so a recording is described by its
measurements rather than by what is in it. Read [AGENTS.md](AGENTS.md) for the
agreed requirements and the index contract.

```sh
./install.sh                 # puts the fuaim command in ~/bin

fuaim scan DIR               # index the recordings under DIR
fuaim browse DIR             # serve that index and open it
fuaim browse                 # the committed fixture, for a look around
```

## Two parts

The tool is deliberately two programs, so that either half can be replaced
without rewriting the other:

| Part | What it does |
| --- | --- |
| **Indexer** | `scan.py` walks a directory hierarchy offline and writes an index — metadata, measurements, and a waveform (cover) and spectrogram (detail) per recording. `label.py` asks a zero-shot audio-language model (CLAP) what is in each recording; `transcribe.py` runs Whisper so spoken words are searchable. |
| **Browser** | `browse.py` is a web app that reads only the index, and lists the library by when, what and how long, with free-text search over transcribed words, labels, filenames and metadata. |

The index format is the contract between them, specified in
[docs/index-format.md](docs/index-format.md) so that either half can be
replaced. The browser never decodes media — the single exception, serving the
original bytes for playback, is documented there.

## Formats

`wav` `amr` `m4a` `3ga` `mpeg` `mp3` `aac` `mp2` `ac3`

## Not a goal

Video analysis — if a file carries a video stream, the picture is ignored and
only the audio track is indexed. Also out of scope: speaker diarisation, music
transcription, and editing the source media.

## Working on this repo

Read [AGENTS.md](AGENTS.md) first. It carries the agreed requirements, the
architecture, the open questions, the conventions, the requirement to update
itself in the same change that introduces a new technology, and the required
form of AI attribution.

## AI contributions

This README, [AGENTS.md](AGENTS.md) and the icon (`static/icon.svg`, and the
`static/favicon.ico` generated from it) were written with GitHub Copilot
(DeepSeek V4 Pro).

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).
