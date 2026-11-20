#!/usr/bin/env python3
"""Prepare and export manually reviewed dialogue clips from YouTube WebVTT.

Phase 1 deliberately has no Python package dependencies. FFmpeg is only needed
when exporting the final, accepted clips.
"""

from __future__ import annotations

import argparse
import csv
import functools
import html
import http.server
import json
import mimetypes
import os
import re
import shlex
import shutil
import subprocess
import sys
import unicodedata
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


TIME_RE = re.compile(r"(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2})[.,](?P<ms>\d{3})")
CUE_RE = re.compile(
    r"(?P<start>\d{2}:\d{2}:\d{2}[.,]\d{3})\s+-->\s+"
    r"(?P<end>\d{2}:\d{2}:\d{2}[.,]\d{3})"
)
TAG_RE = re.compile(r"<(?P<timestamp>\d{2}:\d{2}:\d{2}[.,]\d{3})>|</?c(?:\.[^>]*)?>")
ANNOTATION_RE = re.compile(r"\[[^\]]*\]")
EDGE_PUNCTUATION_RE = re.compile(r"(^[^\wÀ-ÖØ-öø-ÿ']+|[^\wÀ-ÖØ-öø-ÿ']+$)")


@dataclass(frozen=True)
class TimedWord:
    text: str
    start: float
    cue_end: float


def parse_time(value: str) -> float:
    match = TIME_RE.fullmatch(value.strip())
    if not match:
        raise ValueError(f"Invalid timestamp: {value!r}")
    return (
        int(match["h"]) * 3600
        + int(match["m"]) * 60
        + int(match["s"])
        + int(match["ms"]) / 1000
    )


def format_time(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    secs, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{milliseconds:03d}"


def normalize_word(word: str) -> str:
    return EDGE_PUNCTUATION_RE.sub("", word).casefold()


def words_in_piece(piece: str, timestamp: float, cue_end: float) -> list[TimedWord]:
    piece = ANNOTATION_RE.sub(" ", html.unescape(piece))
    return [
        TimedWord(token, timestamp, cue_end)
        for token in piece.split()
        if normalize_word(token)
    ]


def timed_words_for_body(body: Iterable[str], cue_start: float, cue_end: float) -> list[TimedWord]:
    words: list[TimedWord] = []
    for line in body:
        current_time = cue_start
        cursor = 0
        for match in TAG_RE.finditer(line):
            words.extend(words_in_piece(line[cursor : match.start()], current_time, cue_end))
            if match["timestamp"]:
                current_time = parse_time(match["timestamp"])
            cursor = match.end()
        words.extend(words_in_piece(line[cursor:], current_time, cue_end))
    return words


def rolling_overlap(previous: list[TimedWord], current: list[TimedWord]) -> int:
    previous_words = [normalize_word(word.text) for word in previous]
    current_words = [normalize_word(word.text) for word in current]
    for size in range(min(len(previous_words), len(current_words)), 0, -1):
        if previous_words[-size:] == current_words[:size]:
            return size
    return 0


def parse_youtube_vtt(path: Path) -> list[TimedWord]:
    text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    tokens: list[TimedWord] = []
    previous_display: list[TimedWord] = []

    # YouTube sometimes puts a whitespace-only display row immediately after a
    # timing line. Splitting cues on blank lines would therefore discard the
    # real caption row that follows. A timing line is the reliable delimiter.
    cues: list[tuple[re.Match[str], list[str]]] = []
    timing: re.Match[str] | None = None
    body: list[str] = []
    for line in text.splitlines():
        new_timing = CUE_RE.search(line)
        if new_timing:
            if timing is not None:
                cues.append((timing, body))
            timing = new_timing
            body = []
        elif timing is not None:
            body.append(line)
    if timing is not None:
        cues.append((timing, body))

    for timing, body in cues:
        cue_start = parse_time(timing["start"])
        cue_end = parse_time(timing["end"])
        display = timed_words_for_body(body, cue_start, cue_end)

        if not display:
            previous_display = []
            continue

        overlap = rolling_overlap(previous_display, display)
        tokens.extend(display[overlap:])
        previous_display = display

    return tokens


def begins_with_uppercase(text: str) -> bool:
    for character in text:
        if character.isalpha():
            return character.isupper()
    return False


def ends_sentence(text: str) -> bool:
    return text.rstrip().endswith((".", "!", "?", "…"))


def group_candidates(
    words: list[TimedWord],
    *,
    pause: float = 0.9,
    capital_gap: float = 0.25,
    max_duration: float = 8.0,
    start_padding: float = 0.18,
    end_padding: float = 0.08,
) -> list[dict]:
    if not words:
        return []

    groups: list[list[TimedWord]] = []
    current: list[TimedWord] = []
    for word in words:
        if current:
            gap = word.start - current[-1].start
            duration = word.start - current[0].start
            break_before = (
                gap >= pause
                or duration >= max_duration
                or (
                    len(current) >= 3
                    and duration >= 0.8
                    and gap >= capital_gap
                    and begins_with_uppercase(word.text)
                )
                or ends_sentence(current[-1].text)
            )
            if break_before:
                groups.append(current)
                current = []
        current.append(word)
    if current:
        groups.append(current)

    candidates: list[dict] = []
    word_offset = 0
    for index, group in enumerate(groups):
        next_word_index = word_offset + len(group)
        next_start = words[next_word_index].start if next_word_index < len(words) else None
        last = group[-1]

        if next_start is None:
            estimated_end = min(last.cue_end, last.start + 1.2)
        else:
            estimated_end = min(next_start - 0.05, last.start + 1.2)
        estimated_end = max(estimated_end, last.start + 0.25)

        clip_start = max(0.0, group[0].start - start_padding)
        clip_end = estimated_end + end_padding
        text = " ".join(word.text for word in group)
        candidates.append(
            {
                "id": index + 1,
                "text": text,
                "start": round(clip_start, 3),
                "end": round(clip_end, 3),
                "anchorStart": round(group[0].start, 3),
                "anchorEnd": round(estimated_end, 3),
                "words": [
                    {"text": word.text, "start": round(word.start, 3)} for word in group
                ],
                "splitIndex": None,
                "status": "pending",
                "mergedInto": None,
                "mergeHistory": [],
                "original": {
                    "text": text,
                    "start": round(clip_start, 3),
                    "end": round(clip_end, 3),
                },
            }
        )
        word_offset += len(group)
    return candidates


def render_review_html(
    *,
    media_path: Path,
    vtt_path: Path,
    output_path: Path,
    candidates: list[dict],
    duration: float | None,
) -> None:
    relative_media = Path(os.path.relpath(media_path.resolve(), output_path.parent.resolve())).as_posix()
    media_url = urllib.parse.quote(relative_media, safe="/.:_-~")
    storage_key = f"dialogue-clip-review:v1:{media_path.name}:{vtt_path.name}:{len(candidates)}"
    payload = json.dumps(candidates, ensure_ascii=False).replace("</", "<\\/")
    metadata = json.dumps(
        {
            "schemaVersion": 1,
            "media": media_path.name,
            "captions": vtt_path.name,
            "duration": duration,
        },
        ensure_ascii=False,
    ).replace("</", "<\\/")

    page = REVIEW_PAGE.replace("__MEDIA_URL__", html.escape(media_url, quote=True))
    page = page.replace("__STORAGE_KEY__", json.dumps(storage_key))
    page = page.replace("__METADATA__", metadata)
    page = page.replace("__CANDIDATES__", payload)
    output_path.write_text(page, encoding="utf-8")


def probe_duration(media_path: Path) -> float | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(media_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def prepare(args: argparse.Namespace) -> int:
    media_path = args.media.resolve()
    vtt_path = args.vtt.resolve()
    output_path = args.output.resolve()
    if not media_path.is_file():
        raise SystemExit(f"Media file not found: {media_path}")
    if not vtt_path.is_file():
        raise SystemExit(f"VTT file not found: {vtt_path}")

    words = parse_youtube_vtt(vtt_path)
    candidates = group_candidates(
        words,
        pause=args.pause,
        capital_gap=args.capital_gap,
        max_duration=args.max_duration,
    )
    if not candidates:
        raise SystemExit("No spoken-word candidates were found in the VTT file.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    render_review_html(
        media_path=media_path,
        vtt_path=vtt_path,
        output_path=output_path,
        candidates=candidates,
        duration=probe_duration(media_path),
    )
    print(f"Recovered {len(words)} timed words in {len(candidates)} candidate clips.")
    print(f"Review page: {output_path}")
    print("Serve this directory, then open the page in a browser:")
    print(
        "  python3 "
        f"{shlex.quote(str(Path(__file__).resolve()))} serve --directory "
        f"{shlex.quote(str(output_path.parent))}"
    )
    print(f"  http://localhost:8000/{urllib.parse.quote(output_path.name)}")
    return 0


class RangeRequestHandler(http.server.SimpleHTTPRequestHandler):
    """Simple static handler with byte ranges needed for responsive video seeking."""

    byte_range: tuple[int, int] | None = None

    def send_head(self):  # noqa: ANN201 - signature is defined by the standard library
        path = self.translate_path(self.path)
        if os.path.isdir(path) or "Range" not in self.headers:
            return super().send_head()

        try:
            file = open(path, "rb")
        except OSError:
            self.send_error(404, "File not found")
            return None
        try:
            size = os.fstat(file.fileno()).st_size
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers["Range"].strip())
            if not match:
                raise ValueError
            first, last = match.groups()
            if first:
                start = int(first)
                end = int(last) if last else size - 1
            elif last:
                length = int(last)
                start = max(0, size - length)
                end = size - 1
            else:
                raise ValueError
            if start >= size or start < 0 or end < start:
                self.send_error(416, "Requested range not satisfiable")
                file.close()
                return None
            end = min(end, size - 1)
        except ValueError:
            self.send_error(400, "Invalid Range header")
            file.close()
            return None

        self.byte_range = (start, end)
        self.send_response(206)
        self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Last-Modified", self.date_time_string(os.fstat(file.fileno()).st_mtime))
        self.end_headers()
        file.seek(start)
        return file

    def copyfile(self, source, outputfile):  # noqa: ANN001, ANN201
        if self.byte_range is None:
            return super().copyfile(source, outputfile)
        remaining = self.byte_range[1] - self.byte_range[0] + 1
        while remaining:
            chunk = source.read(min(64 * 1024, remaining))
            if not chunk:
                break
            outputfile.write(chunk)
            remaining -= len(chunk)
        self.byte_range = None


def serve(args: argparse.Namespace) -> int:
    directory = args.directory.resolve()
    if not directory.is_dir():
        raise SystemExit(f"Directory not found: {directory}")
    handler = functools.partial(RangeRequestHandler, directory=str(directory))
    server = http.server.ThreadingHTTPServer((args.bind, args.port), handler)
    print(f"Serving {directory} at http://{args.bind}:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        server.server_close()
    return 0


def safe_slug(text: str, limit: int = 48) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text).strip("-").lower()
    return slug[:limit].rstrip("-") or "clip"


def preview_range(start: float, end: float, limit: float = 1.5) -> tuple[float, float]:
    """Return the opening preview range, using the whole clip when it is short."""
    return start, min(limit, end - start)


def export_clips(args: argparse.Namespace) -> int:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit("FFmpeg is required for export but was not found on PATH.")
    review_path = args.review.resolve()
    media_path = args.media.resolve()
    output_dir = args.output.resolve()
    if not review_path.is_file():
        raise SystemExit(f"Review JSON not found: {review_path}")
    if not media_path.is_file():
        raise SystemExit(f"Media file not found: {media_path}")

    review = json.loads(review_path.read_text(encoding="utf-8"))
    if review.get("schemaVersion") != 1 or not isinstance(review.get("clips"), list):
        raise SystemExit("Unsupported or invalid review JSON.")
    clips = review["clips"]
    if not clips:
        raise SystemExit("The review JSON contains no accepted clips.")

    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.tsv"
    rows: list[dict] = []
    for number, clip in enumerate(clips, 1):
        start = float(clip["start"])
        end = float(clip["end"])
        text = str(clip["text"]).strip()
        if start < 0 or end <= start:
            raise SystemExit(f"Invalid range for clip {clip.get('id', number)}: {start}–{end}")
        basename = f"{number:03d}-{safe_slug(text)}"
        audio_filename = f"{basename}.mp3"
        preview_filename = f"{basename}.gif"
        audio_destination = output_dir / audio_filename
        preview_destination = output_dir / preview_filename
        audio_command = [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(media_path),
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{end - start:.3f}",
            "-vn",
            "-map_metadata",
            "-1",
            "-codec:a",
            "libmp3lame",
            "-q:a",
            "4",
            str(audio_destination),
        ]
        subprocess.run(audio_command, check=True)

        preview_start, preview_duration = preview_range(start, end)
        preview_filter = (
            "[0:v]fps=10,scale=-1:min(240\\,ih):flags=area,split[frames][palette_source];"
            "[palette_source]palettegen=max_colors=32:stats_mode=diff[palette];"
            "[frames][palette]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle"
        )
        preview_command = [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{preview_start:.3f}",
            "-t",
            f"{preview_duration:.3f}",
            "-i",
            str(media_path),
            "-an",
            "-map_metadata",
            "-1",
            "-filter_complex",
            preview_filter,
            "-loop",
            "0",
            str(preview_destination),
        ]
        subprocess.run(preview_command, check=True)
        rows.append(
            {
                "file": audio_filename,
                "preview_file": preview_filename,
                "text": text,
                "start": format_time(start),
                "end": format_time(end),
                "duration_seconds": f"{end - start:.3f}",
            }
        )
        print(f"[{number}/{len(clips)}] {audio_filename} + {preview_filename}")

    with manifest_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["file", "preview_file", "text", "start", "end", "duration_seconds"],
            delimiter="\t",
        )
        writer.writeheader()
        writer.writerows(rows)
    print(f"Exported {len(clips)} clips to {output_dir}")
    print(f"Manifest: {manifest_path}")
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    subcommands = root.add_subparsers(dest="command", required=True)

    prepare_parser = subcommands.add_parser("prepare", help="Create a static review page")
    prepare_parser.add_argument("media", type=Path, help="Source audio or video file")
    prepare_parser.add_argument("vtt", type=Path, help="YouTube WebVTT captions")
    prepare_parser.add_argument("-o", "--output", type=Path, default=Path("review.html"))
    prepare_parser.add_argument("--pause", type=float, default=0.9, help="Split on word-start gaps of this many seconds")
    prepare_parser.add_argument("--capital-gap", type=float, default=0.25, help="Minimum gap before a capitalized sentence candidate")
    prepare_parser.add_argument("--max-duration", type=float, default=8.0, help="Maximum candidate duration before splitting")
    prepare_parser.set_defaults(func=prepare)

    export_parser = subcommands.add_parser(
        "export", help="Create MP3 files and tiny GIF previews from accepted ranges"
    )
    export_parser.add_argument("review", type=Path, help="JSON downloaded from the review page")
    export_parser.add_argument("media", type=Path, help="Original media file")
    export_parser.add_argument("-o", "--output", type=Path, default=Path("clips"))
    export_parser.set_defaults(func=export_clips)

    serve_parser = subcommands.add_parser("serve", help="Serve the review page with media seeking support")
    serve_parser.add_argument("--directory", type=Path, default=Path.cwd())
    serve_parser.add_argument("--bind", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)
    serve_parser.set_defaults(func=serve)
    return root


def main() -> int:
    args = parser().parse_args()
    return args.func(args)


REVIEW_PAGE = r'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Dialogue clip review</title>
  <style>
    :root { color-scheme: dark; font-family: system-ui, sans-serif; background: #111318; color: #edf0f7; }
    * { box-sizing: border-box; }
    body { margin: 0; }
    header { position: sticky; top: 0; z-index: 2; display: flex; gap: 1rem; align-items: center; padding: .8rem 1rem; background: #1a1e27; border-bottom: 1px solid #343b49; }
    header strong { margin-right: auto; }
    main { width: min(960px, 100%); margin: auto; padding: 1rem; }
    video { display: block; width: 100%; max-height: 52vh; background: black; border-radius: 8px; }
    .card { margin-top: 1rem; padding: 1rem; background: #1a1e27; border: 1px solid #343b49; border-radius: 8px; }
    .row { display: flex; flex-wrap: wrap; gap: .55rem; align-items: center; margin-top: .75rem; }
    .row > label { min-width: 5rem; color: #aeb7c9; }
    textarea { width: 100%; min-height: 5rem; resize: vertical; padding: .7rem; font: inherit; font-size: 1.15rem; color: inherit; background: #101217; border: 1px solid #465064; border-radius: 6px; }
    input[type=number] { width: 7rem; padding: .45rem; color: inherit; background: #101217; border: 1px solid #465064; border-radius: 5px; }
    button { padding: .52rem .75rem; color: inherit; background: #2b3240; border: 1px solid #4b566b; border-radius: 5px; cursor: pointer; }
    button:hover { background: #384257; }
    button:disabled { opacity: .45; cursor: default; }
    .primary { background: #205ca0; }
    .accept { background: #17653c; }
    .reject { background: #783036; }
    .pending { color: #ffd166; }
    .accepted { color: #7ee2a8; }
    .rejected { color: #ff9a9f; }
    .muted { color: #9da7ba; font-size: .9rem; }
    .spacer { flex: 1; }
    .split-picker { display: flex; flex-wrap: wrap; align-items: center; gap: 0; width: 100%; min-height: 4rem; margin-top: .8rem; padding: .85rem; background: #101217; border: 1px solid #465064; border-radius: 6px; }
    .split-word { padding: .3rem .16rem; font-size: 1.08rem; line-height: 1.6; }
    .split-boundary { position: relative; width: 1rem; min-width: 1rem; height: 2.2rem; padding: 0; background: transparent; border: 0; }
    .split-boundary::before { content: ''; position: absolute; top: .15rem; bottom: .15rem; left: calc(50% - 1px); width: 2px; border-radius: 2px; background: #465064; }
    .split-boundary:hover { background: transparent; }
    .split-boundary:hover::before { background: #8cbcff; }
    .split-boundary.selected::before { left: calc(50% - 2px); width: 4px; background: #4da3ff; box-shadow: 0 0 8px #4da3ff; }
    .split-picker.disabled { opacity: .55; }
    kbd { padding: .1rem .35rem; background: #101217; border: 1px solid #465064; border-radius: 4px; }
    @media (max-width: 600px) { header { gap: .5rem; font-size: .85rem; } main { padding: .5rem; } }
  </style>
</head>
<body>
  <header>
    <strong>Dialogue clip review</strong>
    <span id="position"></span>
    <span class="accepted" id="acceptedCount"></span>
    <span class="rejected" id="rejectedCount"></span>
    <span class="pending" id="pendingCount"></span>
  </header>
  <main>
    <video id="media" controls preload="metadata" src="__MEDIA_URL__"></video>
    <section class="card">
      <div class="row">
        <button id="previous">← Previous</button>
        <button id="play" class="primary">▶ Play clip</button>
        <button id="next">Next →</button>
        <span class="spacer"></span>
        <strong id="status"></strong>
      </div>

      <div class="row">
        <label for="start">Start</label>
        <button data-boundary="start" data-delta="-0.5">−500 ms</button>
        <button data-boundary="start" data-delta="-0.1">−100 ms</button>
        <input id="start" type="number" min="0" step="0.05">
        <button data-boundary="start" data-delta="0.1">+100 ms</button>
        <button data-boundary="start" data-delta="0.5">+500 ms</button>
      </div>
      <div class="row">
        <label for="end">End</label>
        <button data-boundary="end" data-delta="-0.5">−500 ms</button>
        <button data-boundary="end" data-delta="-0.1">−100 ms</button>
        <input id="end" type="number" min="0" step="0.05">
        <button data-boundary="end" data-delta="0.1">+100 ms</button>
        <button data-boundary="end" data-delta="0.5">+500 ms</button>
      </div>
      <div class="row">
        <label for="endReplayDuration">End replay</label>
        <input id="endReplayDuration" type="number" min="0.05" step="0.05" value="0.5">
        <span class="muted">seconds from the end of the clip</span>
      </div>
      <p class="muted" id="timing"></p>
      <textarea id="text" aria-label="Transcript"></textarea>

      <div id="splitPicker" class="split-picker" aria-label="Choose a split point in the transcript"></div>
      <div class="row">
        <button id="split">Split at highlighted line <kbd>S</kbd></button>
        <span id="splitHelp" class="muted"></span>
      </div>

      <div class="row">
        <button id="accept" class="accept">Accept + next <kbd>A</kbd></button>
        <button id="reject" class="reject">Reject + next <kbd>X</kbd></button>
        <button id="merge">Merge next <kbd>M</kbd> </button>
        <button id="undoMerge">Undo merge <kbd>U</kbd> </button>
        <button id="reset">Reset clip</button>
      </div>
      <p class="muted">
        <kbd>Space</kbd> pause/play · <kbd>R</kbd> replay · <kbd>J</kbd>/<kbd>K</kbd> start earlier/later ·
        <kbd>L</kbd>/<kbd>;</kbd> end earlier/later · <kbd>S</kbd> split at highlighted line · arrows navigate. Progress is saved in this browser.
      </p>
    </section>
    <section class="card">
      <div class="row">
        <button id="download" class="primary">Download accepted clips JSON</button>
        <span id="downloadHelp" class="muted"></span>
      </div>
    </section>
  </main>
  <script>
    const initialClips = __CANDIDATES__;
    const metadata = __METADATA__;
    const storageKey = __STORAGE_KEY__;
    let clips = initialClips;
    let currentIndex = 0;
    let rangePlaying = false;
    let playbackGeneration = 0;
    let endReplayDuration = 0.5;

    try {
      const saved = JSON.parse(localStorage.getItem(storageKey));
      if (saved && Array.isArray(saved.clips) && saved.baseCount === initialClips.length) {
        clips = saved.clips;
        currentIndex = Math.min(saved.currentIndex || 0, clips.length - 1);
        if (Number.isFinite(saved.endReplayDuration) && saved.endReplayDuration > 0) {
          endReplayDuration = saved.endReplayDuration;
        }
      }
    } catch (_) {}

    const media = document.querySelector('#media');
    const textInput = document.querySelector('#text');
    const startInput = document.querySelector('#start');
    const endInput = document.querySelector('#end');
    const endReplayInput = document.querySelector('#endReplayDuration');
    const splitPicker = document.querySelector('#splitPicker');

    function activeClip() { return clips[currentIndex]; }
    function visible(clip) { return clip.status !== 'merged'; }
    function activeIndices() { return clips.map((clip, index) => visible(clip) ? index : -1).filter(index => index >= 0); }
    function save() {
      localStorage.setItem(storageKey, JSON.stringify({
        clips, currentIndex, endReplayDuration, baseCount: initialClips.length
      }));
    }
    function roundTime(value) { return Math.round(value * 1000) / 1000; }
    function clock(value) {
      const hours = Math.floor(value / 3600);
      const minutes = Math.floor((value % 3600) / 60);
      const seconds = (value % 60).toFixed(3).padStart(6, '0');
      return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${seconds}`;
    }

    function render() {
      const clip = activeClip();
      const indices = activeIndices();
      const displayPosition = indices.indexOf(currentIndex) + 1;
      const counts = clips.reduce((result, item) => {
        result[item.status] = (result[item.status] || 0) + 1;
        return result;
      }, {});
      document.querySelector('#position').textContent = `${displayPosition}/${indices.length}`;
      document.querySelector('#acceptedCount').textContent = `✓ ${counts.accepted || 0}`;
      document.querySelector('#rejectedCount').textContent = `✕ ${counts.rejected || 0}`;
      document.querySelector('#pendingCount').textContent = `? ${counts.pending || 0}`;
      document.querySelector('#status').textContent = clip.status.toUpperCase();
      document.querySelector('#status').className = clip.status;
      startInput.value = clip.start.toFixed(3);
      endInput.value = clip.end.toFixed(3);
      endReplayInput.value = String(endReplayDuration);
      textInput.value = clip.text;
      const wordsUnedited = clip.words && clip.text === clip.words.map(word => word.text).join(' ');
      const hasSplitPoint = Number.isInteger(clip.splitIndex) && clip.splitIndex > 0 && clip.splitIndex < (clip.words || []).length;
      const canChooseSplit = wordsUnedited && clip.words.length > 1;
      const canSplit = canChooseSplit && hasSplitPoint;
      splitPicker.replaceChildren();
      splitPicker.classList.toggle('disabled', !canChooseSplit);
      const displayWords = clip.words || clip.text.split(/\s+/).filter(Boolean).map(text => ({text}));
      displayWords.forEach((word, wordIndex) => {
        const wordElement = document.createElement('span');
        wordElement.className = 'split-word';
        wordElement.textContent = word.text;
        splitPicker.append(wordElement);
        if (wordIndex >= displayWords.length - 1) return;
        const boundary = document.createElement('button');
        boundary.type = 'button';
        boundary.className = `split-boundary${clip.splitIndex === wordIndex + 1 ? ' selected' : ''}`;
        boundary.title = `Split between “${word.text}” and “${displayWords[wordIndex + 1].text}”`;
        boundary.setAttribute('aria-label', boundary.title);
        boundary.disabled = !canChooseSplit;
        boundary.addEventListener('click', () => {
          clip.splitIndex = wordIndex + 1;
          render();
        });
        splitPicker.append(boundary);
      });
      document.querySelector('#split').disabled = !canSplit;
      document.querySelector('#splitHelp').textContent = !wordsUnedited
        ? 'Splitting is unavailable after manually editing the transcript.'
        : !hasSplitPoint
          ? 'Click a vertical line between two words to choose the split.'
          : `Selected: before “${clip.words[clip.splitIndex].text}”.`;
      document.querySelector('#timing').textContent =
        `${clock(clip.start)} → ${clock(clip.end)} · ${(clip.end - clip.start).toFixed(3)} seconds · ` +
        `caption anchors ${clock(clip.anchorStart)} → ${clock(clip.anchorEnd)}`;
      document.querySelector('#undoMerge').disabled = !clip.mergeHistory.length;
      document.querySelector('#download').disabled = false;
      document.querySelector('#downloadHelp').textContent = `${counts.accepted || 0} accepted clips will be downloaded.`;
      save();
    }

    function markChanged() {
      if (activeClip().status === 'accepted' || activeClip().status === 'rejected') activeClip().status = 'pending';
    }

    function stopRange(snapToEnd = false) {
      const clip = activeClip();
      playbackGeneration += 1;
      rangePlaying = false;
      media.pause();
      if (snapToEnd) media.currentTime = clip.end;
    }

    function enforceEnd(generation) {
      if (!rangePlaying || generation !== playbackGeneration) return;
      const definedEnd = activeClip().end;
      if (media.currentTime >= definedEnd - .01) {
        stopRange(true);
        return;
      }
      requestAnimationFrame(() => enforceEnd(generation));
    }

    function playFrom(startTime) {
      const clip = activeClip();
      const generation = ++playbackGeneration;
      rangePlaying = true;
      media.currentTime = Math.max(clip.start, Math.min(startTime, clip.end - .01));
      requestAnimationFrame(() => enforceEnd(generation));
      const playback = media.play();
      if (playback) playback.catch(() => {
        if (generation === playbackGeneration) {
          rangePlaying = false;
          playbackGeneration += 1;
        }
      });
    }

    function playRange() {
      playFrom(activeClip().start);
    }

    function togglePlayback() {
      if (!media.paused) {
        media.pause();
        return;
      }
      const clip = activeClip();
      if (media.currentTime < clip.start || media.currentTime >= clip.end - .01) playRange();
      else playFrom(media.currentTime);
    }

    function playEndReplay() {
      const clip = activeClip();
      playFrom(Math.max(clip.start, clip.end - endReplayDuration));
    }
    media.addEventListener('play', () => {
      if (rangePlaying) return;
      const clip = activeClip();
      if (media.currentTime < clip.start || media.currentTime >= clip.end) media.currentTime = clip.start;
      const generation = ++playbackGeneration;
      rangePlaying = true;
      requestAnimationFrame(() => enforceEnd(generation));
    });
    media.addEventListener('pause', () => {
      if (!rangePlaying) return;
      rangePlaying = false;
      playbackGeneration += 1;
    });

    function move(direction, pendingOnly = false) {
      stopRange();
      const indices = activeIndices();
      const position = indices.indexOf(currentIndex);
      for (let offset = 1; offset <= indices.length; offset++) {
        const candidate = indices[(position + direction * offset + indices.length) % indices.length];
        if (!pendingOnly || clips[candidate].status === 'pending') {
          currentIndex = candidate;
          render();
          return true;
        }
      }
      render();
      return false;
    }

    function setStatus(status) {
      activeClip().status = status;
      save();
      const moved = move(1, true);
      if (status === 'rejected' && moved) playRange();
    }

    function adjustBoundary(field, delta) {
      const clip = activeClip();
      if (field === 'start') clip.start = roundTime(Math.max(0, Math.min(clip.end - 0.1, clip.start + delta)));
      else clip.end = roundTime(Math.max(clip.start + 0.1, clip.end + delta));
      markChanged();
      render();
      if (field === 'end') playEndReplay();
      else playRange();
    }

    function nextMergeIndex() {
      for (let index = currentIndex + 1; index < clips.length; index++) if (visible(clips[index])) return index;
      return -1;
    }

    function mergeNext() {
      const index = nextMergeIndex();
      if (index < 0) return;
      const clip = activeClip();
      const next = clips[index];
      clip.mergeHistory.push({text: clip.text, words: clip.words, end: clip.end, nextId: next.id, nextStatus: next.status});
      clip.text = `${clip.text} ${next.text}`.trim();
      clip.words = [...(clip.words || []), ...(next.words || [])];
      clip.splitIndex = null;
      clip.end = next.end;
      next.status = 'merged';
      next.mergedInto = clip.id;
      clip.status = 'pending';
      render();
      playRange();
    }

    function undoMerge() {
      const clip = activeClip();
      const state = clip.mergeHistory.pop();
      if (!state) return;
      const next = clips.find(item => item.id === state.nextId);
      clip.text = state.text;
      clip.words = state.words;
      clip.splitIndex = null;
      clip.end = state.end;
      clip.status = 'pending';
      if (next) { next.status = state.nextStatus; next.mergedInto = null; }
      render();
    }

    function splitClip() {
      const clip = activeClip();
      const index = clip.splitIndex;
      if (!clip.words || index <= 0 || index >= clip.words.length) return;
      const leftWords = clip.words.slice(0, index);
      const rightWords = clip.words.slice(index);
      const splitTime = rightWords[0].start;
      const oldEnd = clip.end;
      const oldAnchorEnd = clip.anchorEnd;
      const newId = Math.max(...clips.map(item => Number(item.id)).filter(Number.isFinite), 0) + 1;
      const rightStart = roundTime(Math.max(0, splitTime - .18));
      const leftEnd = roundTime(Math.max(clip.start + .1, splitTime + .03));

      clip.words = leftWords;
      clip.text = leftWords.map(word => word.text).join(' ');
      clip.end = leftEnd;
      clip.anchorEnd = splitTime;
      clip.status = 'pending';
      // A new split supersedes any earlier merge boundary. Hidden source
      // candidates stay hidden while the combined words are redistributed
      // between these two visible clips.
      clip.mergeHistory = [];
      clip.splitIndex = null;
      clip.original = {text: clip.text, start: clip.start, end: clip.end};

      const right = {
        id: newId,
        text: rightWords.map(word => word.text).join(' '),
        start: rightStart,
        end: oldEnd,
        anchorStart: splitTime,
        anchorEnd: oldAnchorEnd,
        words: rightWords,
        splitIndex: null,
        status: 'pending',
        mergedInto: null,
        mergeHistory: [],
        original: {text: rightWords.map(word => word.text).join(' '), start: rightStart, end: oldEnd}
      };
      clips.splice(currentIndex + 1, 0, right);
      render();
      playRange();
    }

    document.querySelector('#play').addEventListener('click', playRange);
    document.querySelector('#previous').addEventListener('click', () => move(-1));
    document.querySelector('#next').addEventListener('click', () => move(1));
    document.querySelector('#accept').addEventListener('click', () => setStatus('accepted'));
    document.querySelector('#reject').addEventListener('click', () => setStatus('rejected'));
    document.querySelector('#merge').addEventListener('click', mergeNext);
    document.querySelector('#undoMerge').addEventListener('click', undoMerge);
    document.querySelector('#split').addEventListener('click', splitClip);
    document.querySelector('#reset').addEventListener('click', () => {
      while (activeClip().mergeHistory.length) undoMerge();
      const clip = activeClip();
      clip.text = clip.original.text;
      clip.start = clip.original.start;
      clip.end = clip.original.end;
      clip.splitIndex = null;
      clip.status = 'pending';
      render();
      playRange();
    });
    document.querySelectorAll('[data-boundary]').forEach(button => button.addEventListener('click', () => {
      adjustBoundary(button.dataset.boundary, Number(button.dataset.delta));
    }));
    startInput.addEventListener('change', () => {
      activeClip().start = roundTime(Math.max(0, Math.min(activeClip().end - .1, Number(startInput.value))));
      markChanged(); render(); playRange();
    });
    endInput.addEventListener('change', () => {
      activeClip().end = roundTime(Math.max(activeClip().start + .1, Number(endInput.value)));
      markChanged(); render(); playEndReplay();
    });
    endReplayInput.addEventListener('change', () => {
      const value = Number(endReplayInput.value);
      if (Number.isFinite(value) && value > 0) endReplayDuration = roundTime(value);
      endReplayInput.value = String(endReplayDuration);
      save();
    });
    textInput.addEventListener('change', () => {
      activeClip().text = textInput.value.trim(); markChanged(); render();
    });
    document.querySelector('#download').addEventListener('click', () => {
      const accepted = clips.filter(clip => clip.status === 'accepted').map(clip => ({
        id: clip.id, text: clip.text, start: clip.start, end: clip.end
      }));
      const result = {...metadata, exportedAt: new Date().toISOString(), clips: accepted};
      const blob = new Blob([JSON.stringify(result, null, 2)], {type: 'application/json'});
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = 'reviewed-clips.json';
      link.click();
      setTimeout(() => URL.revokeObjectURL(link.href), 1000);
    });
    document.addEventListener('keydown', event => {
      if (event.target.matches('textarea,input,select')) return;
      const key = event.key.toLowerCase();
      if (key === ' ') { event.preventDefault(); togglePlayback(); }
      else if (key === 'r') playRange();
      else if (key === 'a') setStatus('accepted');
      else if (key === 'x') setStatus('rejected');
      else if (key === 'j') adjustBoundary('start', -.1);
      else if (key === 'k') adjustBoundary('start', .1);
      else if (key === 'l') adjustBoundary('end', -.1);
      else if (key === ';') adjustBoundary('end', .1);
      else if (key === 'u') undoMerge();
      else if (key === 's') splitClip();
      else if (event.key === 'ArrowLeft') move(-1);
      else if (event.key === 'ArrowRight') move(1);
    });
    render();
  </script>
</body>
</html>
'''


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as error:
        print(f"FFmpeg failed with exit code {error.returncode}.", file=sys.stderr)
        raise SystemExit(error.returncode)
