#!/usr/bin/env python3
"""Build an Anki .apkg deck from a ClipReviewer clips directory."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


MODEL_ID = 1_607_392_321
MODEL_NAME = "ClipReviewer Sentence"
FRONT_TEMPLATE = "{{Front}}"
BACK_TEMPLATE = """{{FrontSide}}

<hr id=answer>

{{Back}}

<div style='font-family: "Arial"; font-size: 14px; color:grey;'>{{Meaning}}</div>"""
CARD_CSS = """.card {
    font-family: arial;
    font-size: 20px;
    line-height: 1.5;
    text-align: center;
    color: black;
    background-color: white;
}"""


class AnkiExportError(Exception):
    """A user-facing export error."""


@dataclass(frozen=True)
class Clip:
    audio_path: Path
    preview_path: Path
    text: str


def safe_media_name(value: str, *, field: str, row_number: int) -> str:
    path = Path(value)
    if not value or path.name != value or value in {".", ".."}:
        raise AnkiExportError(
            f"manifest.tsv row {row_number} has an invalid {field}: {value!r}. "
            "Media entries must be plain filenames."
        )
    return value


def load_clips(directory: Path) -> list[Clip]:
    manifest_path = directory / "manifest.tsv"
    if not directory.is_dir():
        raise AnkiExportError(f"Clips directory not found: {directory}")
    if not manifest_path.is_file():
        raise AnkiExportError(f"Clip manifest not found: {manifest_path}")

    with manifest_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        required = {"file", "text"}
        missing = required.difference(reader.fieldnames or [])
        if missing:
            raise AnkiExportError(
                f"manifest.tsv is missing required columns: {', '.join(sorted(missing))}"
            )
        clips: list[Clip] = []
        seen_media: set[str] = set()
        for row_number, row in enumerate(reader, 2):
            audio_name = safe_media_name(row.get("file", "").strip(), field="file", row_number=row_number)
            preview_value = row.get("preview_file", "").strip()
            preview_name = safe_media_name(
                preview_value or str(Path(audio_name).with_suffix(".gif")),
                field="preview_file",
                row_number=row_number,
            )
            text = row.get("text", "").strip()
            if not text:
                raise AnkiExportError(f"manifest.tsv row {row_number} has no text.")
            if not audio_name.lower().endswith(".mp3"):
                raise AnkiExportError(f"Expected an MP3 in row {row_number}, got: {audio_name}")
            if not preview_name.lower().endswith(".gif"):
                raise AnkiExportError(f"Expected a GIF in row {row_number}, got: {preview_name}")
            for media_name in (audio_name, preview_name):
                if media_name in seen_media:
                    raise AnkiExportError(f"Duplicate media filename in manifest.tsv: {media_name}")
                seen_media.add(media_name)

            audio_path = directory / audio_name
            preview_path = directory / preview_name
            if not audio_path.is_file():
                raise AnkiExportError(f"MP3 file not found: {audio_path}")
            if not preview_path.is_file():
                raise AnkiExportError(f"GIF file not found: {preview_path}")
            clips.append(Clip(audio_path=audio_path, preview_path=preview_path, text=text))

    if not clips:
        raise AnkiExportError("manifest.tsv contains no clips.")
    return clips


def load_translation_cache(path: Path) -> dict[tuple[str, str], str]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        required = {"file", "text", "meaning"}
        missing = required.difference(reader.fieldnames or [])
        if missing:
            raise AnkiExportError(
                f"Translation cache is missing columns: {', '.join(sorted(missing))}"
            )
        return {
            (row["file"], row["text"]): row["meaning"].strip()
            for row in reader
            if row.get("file") and row.get("text") and row.get("meaning", "").strip()
        }


def write_translation_cache(
    path: Path, clips: list[Clip], meanings: dict[tuple[str, str], str]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["file", "text", "meaning"], delimiter="\t")
        writer.writeheader()
        for clip in clips:
            key = (clip.audio_path.name, clip.text)
            meaning = meanings.get(key)
            if meaning:
                writer.writerow({"file": key[0], "text": key[1], "meaning": meaning})


def argos_translator(
    source_language: str, target_language: str, *, install_model: bool
) -> Callable[[str], str]:
    try:
        from argostranslate import package, translate
    except ImportError as error:
        raise AnkiExportError(
            "Argos Translate is not installed. Install the Anki dependencies with:\n"
            "  python3.13 -m pip install -r requirements-anki.txt"
        ) from error

    installed = package.get_installed_packages()
    has_pair = any(
        item.from_code == source_language and item.to_code == target_language
        for item in installed
    )
    if not has_pair and install_model:
        print(f"Downloading the Argos {source_language}→{target_language} translation model...")
        package.update_package_index()
        candidate = next(
            (
                item
                for item in package.get_available_packages()
                if item.from_code == source_language and item.to_code == target_language
            ),
            None,
        )
        if candidate is None:
            raise AnkiExportError(
                f"No Argos model is available for {source_language}→{target_language}."
            )
        candidate.install()
        translate.get_installed_languages.cache_clear()
        has_pair = True

    if not has_pair:
        raise AnkiExportError(
            f"The Argos {source_language}→{target_language} model is not installed. "
            "Re-run with --install-translation-model for the one-time download."
        )

    translation = translate.get_translation_from_codes(source_language, target_language)
    if translation is None:
        raise AnkiExportError(
            f"Argos could not load the installed {source_language}→{target_language} model."
        )
    return translation.translate


def collect_meanings(
    clips: list[Clip],
    cache_path: Path,
    *,
    translator_name: str,
    source_language: str,
    target_language: str,
    install_model: bool,
    refresh: bool,
) -> dict[tuple[str, str], str]:
    meanings = load_translation_cache(cache_path)
    missing = [
        clip
        for clip in clips
        if refresh or not meanings.get((clip.audio_path.name, clip.text))
    ]
    if not missing:
        print(f"Using {len(clips)} cached translations from {cache_path}")
        return meanings
    if translator_name == "cache":
        raise AnkiExportError(
            f"{len(missing)} translation(s) are missing from {cache_path}. "
            "Use --translator argos or fill in the cache manually."
        )

    translate_text = argos_translator(
        source_language, target_language, install_model=install_model
    )
    for index, clip in enumerate(missing, 1):
        meaning = translate_text(clip.text).strip()
        if not meaning:
            raise AnkiExportError(f"Translation was empty for: {clip.text}")
        meanings[(clip.audio_path.name, clip.text)] = meaning
        write_translation_cache(cache_path, clips, meanings)
        print(f"Translated [{index}/{len(missing)}] {clip.text} → {meaning}")
    return meanings


def deck_id_for(name: str) -> int:
    digest = hashlib.sha256(f"ClipReviewer:{name}".encode()).digest()
    return 1_000_000_000 + int.from_bytes(digest[:4], "big") % 1_000_000_000


def front_field(clip: Clip) -> str:
    preview_name = html.escape(clip.preview_path.name, quote=True)
    audio_name = html.escape(clip.audio_path.name, quote=True)
    return f'<img src="{preview_name}"><br>[sound:{audio_name}]'


def write_package(
    clips: list[Clip], meanings: dict[tuple[str, str], str], deck_name: str, output_path: Path
) -> None:
    try:
        import genanki
    except ImportError as error:
        raise AnkiExportError(
            "genanki is not installed. Install the Anki dependencies with:\n"
            "  python3.13 -m pip install -r requirements-anki.txt"
        ) from error

    model = genanki.Model(
        MODEL_ID,
        MODEL_NAME,
        fields=[{"name": "Front"}, {"name": "Back"}, {"name": "Meaning"}],
        templates=[
            {"name": "Recognition", "qfmt": FRONT_TEMPLATE, "afmt": BACK_TEMPLATE}
        ],
        css=CARD_CSS,
    )
    deck = genanki.Deck(deck_id_for(deck_name), deck_name)
    media_files: list[str] = []
    for order, clip in enumerate(clips, 1):
        key = (clip.audio_path.name, clip.text)
        deck.add_note(
            genanki.Note(
                model=model,
                fields=[front_field(clip), html.escape(clip.text), html.escape(meanings[key])],
                tags=["clipreviewer", "italian"],
                guid=genanki.guid_for("clipreviewer", clip.audio_path.name),
                due=order,
            )
        )
        media_files.extend([str(clip.audio_path), str(clip.preview_path)])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    package = genanki.Package(deck)
    package.media_files = media_files
    package.write_to_file(str(output_path))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("directory", type=Path, help="Directory containing manifest.tsv and clip media")
    result.add_argument("-o", "--output", type=Path, help="Output .apkg path (default: beside the clips directory)")
    result.add_argument("--deck-name", default="Italian ClipReviewer", help="Name shown in Anki")
    result.add_argument("--source-language", default="it", help="Argos source language code")
    result.add_argument("--target-language", default="en", help="Argos target language code")
    result.add_argument(
        "--translator",
        choices=("argos", "cache"),
        default="argos",
        help="Translate missing meanings locally with Argos, or require cached meanings",
    )
    result.add_argument(
        "--translation-cache",
        type=Path,
        help="Editable translation TSV (default: DIRECTORY/translations.tsv)",
    )
    result.add_argument(
        "--install-translation-model",
        action="store_true",
        help="Download and install the Argos language model if it is missing",
    )
    result.add_argument(
        "--refresh-translations",
        action="store_true",
        help="Replace cached translations using the selected translator",
    )
    return result


def main() -> int:
    args = parser().parse_args()
    directory = args.directory.resolve()
    output_path = (
        args.output.resolve()
        if args.output
        else directory.parent / f"{directory.name}.apkg"
    )
    if output_path.suffix.lower() != ".apkg":
        raise AnkiExportError(f"Output path must end in .apkg: {output_path}")
    cache_path = (
        args.translation_cache.resolve()
        if args.translation_cache
        else directory / "translations.tsv"
    )

    clips = load_clips(directory)
    meanings = collect_meanings(
        clips,
        cache_path,
        translator_name=args.translator,
        source_language=args.source_language,
        target_language=args.target_language,
        install_model=args.install_translation_model,
        refresh=args.refresh_translations,
    )
    write_package(clips, meanings, args.deck_name, output_path)
    print(f"Created {output_path} with {len(clips)} cards.")
    print(f"Translations: {cache_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AnkiExportError as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)
