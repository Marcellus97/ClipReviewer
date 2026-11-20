import tempfile
import unittest
from pathlib import Path

from clipper import group_candidates, parse_time, parse_youtube_vtt, preview_range


class ClipperTests(unittest.TestCase):
    def test_parse_time(self):
        self.assertEqual(parse_time("00:02:10.640"), 130.64)

    def test_recovers_only_new_words_from_rolling_captions(self):
        sample = """WEBVTT

00:00:01.000 --> 00:00:03.000
ciao<00:00:01.500><c> mondo</c>

00:00:03.000 --> 00:00:03.010
ciao mondo

00:00:03.010 --> 00:00:05.000
ciao mondo
Come<00:00:03.500><c> stai</c>
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.vtt"
            path.write_text(sample, encoding="utf-8")
            words = parse_youtube_vtt(path)
        self.assertEqual([word.text for word in words], ["ciao", "mondo", "Come", "stai"])
        self.assertEqual([word.start for word in words], [1.0, 1.5, 3.01, 3.5])

    def test_keeps_caption_after_youtube_blank_display_row(self):
        sample = """WEBVTT

00:00:01.000 --> 00:00:03.000
 
ciao<00:00:01.500><c> mondo</c>

00:00:03.000 --> 00:00:03.010
ciao mondo
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.vtt"
            path.write_text(sample, encoding="utf-8")
            words = parse_youtube_vtt(path)
        self.assertEqual([word.text for word in words], ["ciao", "mondo"])
        self.assertEqual([word.start for word in words], [1.0, 1.5])

    def test_groups_at_capitalized_boundary(self):
        sample = """WEBVTT

00:00:01.000 --> 00:00:03.000
ciao<00:00:01.300><c> a</c><00:00:01.500><c> tutti</c><00:00:02.000><c> Come</c><00:00:02.300><c> state</c>
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.vtt"
            path.write_text(sample, encoding="utf-8")
            words = parse_youtube_vtt(path)
        candidates = group_candidates(words)
        self.assertEqual([item["text"] for item in candidates], ["ciao a tutti", "Come state"])

    def test_preview_uses_first_second_and_a_half_of_long_clip(self):
        self.assertEqual(preview_range(10.0, 14.0), (10.0, 1.5))

    def test_preview_uses_whole_short_clip(self):
        start, duration = preview_range(10.0, 10.6)
        self.assertAlmostEqual(start, 10.0)
        self.assertAlmostEqual(duration, 0.6)

    def test_review_player_tracks_current_defined_end(self):
        source = Path(__file__).with_name("clipper.py").read_text(encoding="utf-8")
        self.assertIn("const definedEnd = activeClip().end", source)
        self.assertIn("stopRange(true)", source)
        self.assertNotIn("addEventListener('seeking'", source)

    def test_accepted_clips_download_is_always_enabled(self):
        source = Path(__file__).with_name("clipper.py").read_text(encoding="utf-8")
        self.assertIn("document.querySelector('#download').disabled = false", source)

    def test_merged_clip_can_be_split_again(self):
        source = Path(__file__).with_name("clipper.py").read_text(encoding="utf-8")
        self.assertIn("const canChooseSplit = wordsUnedited && clip.words.length > 1", source)
        self.assertIn("clip.mergeHistory = []", source)
        self.assertNotIn("index >= clip.words.length || clip.mergeHistory.length", source)

    def test_visual_split_picker_and_keyboard_shortcut(self):
        source = Path(__file__).with_name("clipper.py").read_text(encoding="utf-8")
        self.assertIn("className = `split-boundary", source)
        self.assertIn("clip.splitIndex = wordIndex + 1", source)
        self.assertIn("else if (key === 's') splitClip()", source)

    def test_reject_advances_and_plays_next_pending_clip(self):
        source = Path(__file__).with_name("clipper.py").read_text(encoding="utf-8")
        self.assertIn("const moved = move(1, true)", source)
        self.assertIn("if (status === 'rejected' && moved) playRange()", source)

    def test_end_adjustment_replays_configurable_clip_tail(self):
        source = Path(__file__).with_name("clipper.py").read_text(encoding="utf-8")
        self.assertIn('id="endReplayDuration"', source)
        self.assertIn("let endReplayDuration = 0.5", source)
        self.assertIn("playFrom(Math.max(clip.start, clip.end - endReplayDuration))", source)
        self.assertIn("if (field === 'end') playEndReplay()", source)
        self.assertIn("markChanged(); render(); playEndReplay()", source)

    def test_space_toggles_and_r_replays(self):
        source = Path(__file__).with_name("clipper.py").read_text(encoding="utf-8")
        self.assertIn("function togglePlayback()", source)
        self.assertIn("event.preventDefault(); togglePlayback()", source)
        self.assertIn("else if (key === 'r') playRange()", source)


if __name__ == "__main__":
    unittest.main()
