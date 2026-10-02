# fixtures

The synthetic library the browser is developed against, before a real scan exists
— the same way físeán's was. Everything the browser shows here is invented audio
until the indexer replaces the fixture.

- `media/` — a few seconds of synthesised audio across four formats, enough to
  exercise playback and the waveform/spectrogram images.
- `fuaim-index/` — a committed index of `manifest.json`, `audio/<id>.json`,
  `wave/<id>.png` and `spec/<id>.png`, so `fuaim browse` works from a fresh clone
  with no scan and no model. The labels, sound events and transcript are patched
  in by `make_fixtures.py` as invented data.

Regenerate everything with:

```sh
python3 fixtures/make_fixtures.py
```

It is idempotent: it rebuilds the media with ffmpeg, re-runs `fuaim scan` over
it, and re-applies the fixture labels, events and transcript. See
[GENERATED.txt](GENERATED.txt) for the convention around committed generated
files.
