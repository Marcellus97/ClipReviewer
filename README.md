# ClipReviewer

This small local workflow cleans YouTube's rolling WebVTT captions, proposes
dialogue clips, lets you review their boundaries in a browser, and exports the
accepted ranges as MP3 files.

It uses only Python's standard library. FFmpeg/ffprobe are used for media
inspection and final export.

Run these commands from the directory containing the episode and captions. The
reusable application itself lives in `~/ClipReviewer`.

## 1. Generate the review page

```sh
python3 ~/ClipReviewer/clipper.py prepare \
  "Hunter x Condizione x Addio ep. 01   ITA [ozmH5IC2BkM].webm" \
  "Hunter x Condizione x Addio ep. 01   ITA [ozmH5IC2BkM].it.vtt"
```

This writes `review.html`. Start a local static server in the same directory:

```sh
python3 ~/ClipReviewer/clipper.py serve
```

Then open <http://localhost:8000/review.html> in Brave or Chrome.

The included server supports byte-range requests, which lets the browser seek
through the episode without downloading the whole file first. Stop it with
Ctrl+C when finished.

The page saves progress to browser local storage. Review each candidate,
adjust its start/end, edit its transcript if necessary, then accept or reject
it. To split a candidate, click a vertical line between words and press `S` or
the split button. You can download the currently accepted clips as JSON at any
time.

Adjusting a clip's end replays only its final 0.5 seconds by default. Change
the on-screen **End replay** value to use a different preview duration. The
space bar pauses or resumes playback, while `R` replays the whole clip.

## 2. Export accepted clips

Move the downloaded `reviewed-clips.json` here, or refer to it by its full path:

```sh
python3 ~/ClipReviewer/clipper.py export reviewed-clips.json \
  "Hunter x Condizione x Addio ep. 01   ITA [ozmH5IC2BkM].webm"
```

For every accepted range, this writes an MP3 plus a tiny animated GIF preview
to `clips/`. The GIF captures the first 1.5 seconds (or the entire range when
shorter), has no audio, runs at 10 fps, is at most 240 pixels high, and uses a
32-color palette. `manifest.tsv` lists both files.

## Candidate grouping controls

The defaults intentionally favor simple, editable proposals. If they split too
often or not often enough, regenerate the page with adjusted values:

```sh
python3 ~/ClipReviewer/clipper.py prepare VIDEO.webm CAPTIONS.vtt \
  --pause 1.1 --capital-gap 0.35 --max-duration 9
```

Changing these values generates a new candidate layout. Browser progress only
applies when the source filenames and candidate count match.

## 3. Build an Anki deck

Create a Python 3.13 virtual environment and install the deck dependencies:

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -r ~/ClipReviewer/requirements-anki.txt
```

Then build a deck from the exported clips. The first run can download the
Italian-to-English Argos Translate model; translation runs locally after that:

```sh
~/ClipReviewer/make-anki.sh clips --install-translation-model
```

This creates `clips.apkg` beside the `clips/` directory. Each card's front
shows the GIF and plays the MP3. The back shows the Italian transcript and a
rough English meaning. Translations are saved in the editable
`clips/translations.tsv`; subsequent runs reuse that file. To require the cache
without invoking a translator, pass `--translator cache`.

For normal subsequent exports, run this from `~/ClipReviewer`:

```sh
./make-anki.sh
```

With no arguments, the wrapper uses `~/ClipReviewer/clips`. You can pass a
different clips directory and any `anki_exporter.py` options after it.
