import tempfile
import unittest
from pathlib import Path

from anki_exporter import (
    BACK_TEMPLATE,
    CARD_CSS,
    FRONT_TEMPLATE,
    Clip,
    AnkiExportError,
    deck_id_for,
    front_field,
    load_clips,
    load_translation_cache,
    write_translation_cache,
)


class AnkiExporterTests(unittest.TestCase):
    def make_clips_directory(self, directory: Path) -> None:
        (directory / "one.mp3").write_bytes(b"mp3")
        (directory / "one.gif").write_bytes(b"gif")
        (directory / "manifest.tsv").write_text(
            "file\tpreview_file\ttext\tstart\tend\tduration_seconds\n"
            "one.mp3\tone.gif\tCiao & arrivederci\t00:00:01.000\t00:00:02.000\t1.000\n",
            encoding="utf-8",
        )

    def test_loads_media_pairs_and_text_from_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.make_clips_directory(directory)
            clips = load_clips(directory)
        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0].audio_path.name, "one.mp3")
        self.assertEqual(clips[0].preview_path.name, "one.gif")
        self.assertEqual(clips[0].text, "Ciao & arrivederci")

    def test_rejects_missing_media(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.make_clips_directory(directory)
            (directory / "one.gif").unlink()
            with self.assertRaisesRegex(AnkiExportError, "GIF file not found"):
                load_clips(directory)

    def test_front_field_embeds_gif_and_audio(self):
        clip = Clip(Path("one.mp3"), Path("one.gif"), "Ciao")
        self.assertEqual(front_field(clip), '<img src="one.gif"><br>[sound:one.mp3]')

    def test_translation_cache_round_trip(self):
        clip = Clip(Path("one.mp3"), Path("one.gif"), "Ciao")
        meanings = {("one.mp3", "Ciao"): "Hello"}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "translations.tsv"
            write_translation_cache(path, [clip], meanings)
            loaded = load_translation_cache(path)
        self.assertEqual(loaded, meanings)

    def test_card_layout_matches_requested_template(self):
        self.assertEqual(FRONT_TEMPLATE, "{{Front}}")
        self.assertIn("{{FrontSide}}\n\n<hr id=answer>\n\n{{Back}}", BACK_TEMPLATE)
        self.assertIn("{{Meaning}}", BACK_TEMPLATE)
        self.assertIn("font-size: 20px", CARD_CSS)

    def test_deck_id_is_stable_and_name_specific(self):
        self.assertEqual(deck_id_for("Italian"), deck_id_for("Italian"))
        self.assertNotEqual(deck_id_for("Italian"), deck_id_for("French"))


if __name__ == "__main__":
    unittest.main()
