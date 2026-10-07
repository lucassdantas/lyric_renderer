"""Pure logic: time parsing/formatting, colors, paste-block parser, project files."""
import json
import os
import tempfile
import unittest

import lyric_renderer as lr


class ParseTimeTest(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(lr.parse_time("00:05"), 5)
        self.assertEqual(lr.parse_time("1:30"), 90)
        self.assertAlmostEqual(lr.parse_time("00:05.50"), 5.5)
        self.assertAlmostEqual(lr.parse_time("00:05,50"), 5.5)
        self.assertEqual(lr.parse_time("1:02:03"), 3723)
        self.assertEqual(lr.parse_time(" 12 "), 12)

    def test_invalid(self):
        for bad in ("abc", "1:xx", ""):
            with self.assertRaises(ValueError):
                lr.parse_time(bad)


class FormatTimeTest(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(lr.format_time(0), "00:00.00")
        self.assertEqual(lr.format_time(90.5), "01:30.50")

    def test_float_rounding(self):
        # 2.3 % 1 == 0.2999…, which used to be shown as .29
        self.assertEqual(lr.format_time(2.3), "00:02.30")
        # 59.999 used to become "00:60.00" in the editor
        self.assertEqual(lr.format_time(59.999), "01:00.00")

    def test_round_trip(self):
        for t in (0, 1.29, 2.3, 65.07, 599.99):
            self.assertAlmostEqual(lr.parse_time(lr.format_time(t)), t, places=2)

    def test_strophe_helpers(self):
        s = lr.Strophe(1, 2.3, 10, "x")
        self.assertEqual(s.start_str(), "00:02.30")
        self.assertEqual(s.end_str(), "00:10.00")
        self.assertAlmostEqual(s.duration(), 7.7)


class TimeMaskTest(unittest.TestCase):
    def test_mask(self):
        self.assertEqual(lr.mask_time_digits(""), "00:00")
        self.assertEqual(lr.mask_time_digits("1"), "00:01")
        self.assertEqual(lr.mask_time_digits("10"), "00:10")
        self.assertEqual(lr.mask_time_digits("100"), "01:00")
        self.assertEqual(lr.mask_time_digits("1000"), "10:00")

    def test_digits_to_seconds(self):
        self.assertEqual(lr.time_digits_to_seconds(""), 0)
        self.assertEqual(lr.time_digits_to_seconds("130"), 90)
        self.assertEqual(lr.time_digits_to_seconds("190"), 150)  # 01:90 → 2:30

    def test_seconds_to_digits(self):
        self.assertEqual(lr.seconds_to_time_digits(0), "")
        self.assertEqual(lr.seconds_to_time_digits(90), "130")
        self.assertEqual(lr.seconds_to_time_digits(21.9), "21")  # whole seconds
        self.assertEqual(lr.seconds_to_time_digits(-5), "")
        self.assertEqual(lr.seconds_to_time_digits(10 ** 6), "9959")


class HelpersTest(unittest.TestCase):
    def test_hex_to_rgb(self):
        self.assertEqual(lr.hex_to_rgb("#FF8000"), (255, 128, 0))
        self.assertEqual(lr.hex_to_rgb("00ff00"), (0, 255, 0))
        self.assertEqual(lr.hex_to_rgb("#fff"), (255, 255, 255))

    def test_safe_filename(self):
        self.assertEqual(lr.safe_filename('AC/DC: "Live"?'), "ACDC Live")
        self.assertEqual(lr.safe_filename("Música boa"), "Música boa")
        self.assertEqual(lr.safe_filename("  "), "video")
        self.assertEqual(lr.safe_filename("..."), "video")


class ParseLyricsBlockTest(unittest.TestCase):
    def test_basic_block(self):
        raw = "00:05 - 00:15\nLinha 1\n  Linha 2  \n\n00:20 – 00:30\nOutra"
        strophes, errors = lr.parse_lyrics_block(raw, next_id=7)
        self.assertEqual(errors, [])
        self.assertEqual(len(strophes), 2)
        self.assertEqual((strophes[0].id, strophes[0].start_time, strophes[0].end_time),
                         (7, 5, 15))
        self.assertEqual(strophes[0].text, "Linha 1\nLinha 2")
        self.assertEqual(strophes[1].id, 8)

    def test_decimals_and_hours(self):
        raw = "00:05.50 - 00:07,25\na\n\n1:00:00 - 1:00:10\nb"
        strophes, errors = lr.parse_lyrics_block(raw)
        self.assertEqual(errors, [])
        self.assertAlmostEqual(strophes[0].start_time, 5.5)
        self.assertAlmostEqual(strophes[0].end_time, 7.25)
        self.assertEqual(strophes[1].start_time, 3600)

    def test_text_on_header_line_is_kept(self):
        strophes, _ = lr.parse_lyrics_block("00:01 - 00:02 Oi\ntudo bem")
        self.assertEqual(strophes[0].text, "Oi\ntudo bem")

    def test_blank_lines_with_spaces_separate_blocks(self):
        strophes, errors = lr.parse_lyrics_block("00:01 - 00:02\na\n   \n00:03 - 00:04\nb")
        self.assertEqual((len(strophes), errors), (2, []))

    def test_errors(self):
        raw = ("sem tempo\nnada\n\n"
               "00:10 - 00:05\nfim antes do início\n\n"
               "00:01 - 00:02\n\n"
               "00:03 - 00:04\nok")
        strophes, errors = lr.parse_lyrics_block(raw)
        self.assertEqual([s.text for s in strophes], ["ok"])
        self.assertEqual(len(errors), 3)
        self.assertIn("sem tempo", errors[0])
        self.assertIn("fim deve ser após", errors[1])
        self.assertIn("sem letra", errors[2])


class NextStropheTimesTest(unittest.TestCase):
    def test_first_strophe(self):
        self.assertEqual(lr.next_strophe_times([]), (0, 10))

    def test_follows_last_existing_strophe(self):
        s = [lr.Strophe(1, 0, 10, "a"), lr.Strophe(2, 11, 20, "b")]
        self.assertEqual(lr.next_strophe_times(s), (21, 30))

    def test_after_deleting_last_goes_back(self):
        s = [lr.Strophe(1, 0, 10, "a"), lr.Strophe(2, 11, 20, "b"), lr.Strophe(3, 21, 30, "c")]
        self.assertEqual(lr.next_strophe_times(s), (31, 40))
        self.assertEqual(lr.next_strophe_times(s[:2]), (21, 30))

    def test_uses_latest_end_not_insertion_order(self):
        # edited/inserted out of order: the one ending last wins
        s = [lr.Strophe(1, 30, 40.5, "late"), lr.Strophe(2, 0, 10, "early")]
        self.assertEqual(lr.next_strophe_times(s), (41.5, 50.5))


class MergeWithNextTest(unittest.TestCase):
    def setUp(self):
        # inserted out of order on purpose: merge follows time order
        self.s = [lr.Strophe(3, 20, 25, "e\nf"), lr.Strophe(1, 0, 5, "a\nb"),
                  lr.Strophe(2, 10, 15, "c\nd")]

    def test_merge_two_line_pairs_into_four(self):
        res = lr.merge_with_next(self.s, 1)
        self.assertEqual([(x.id, x.start_time, x.end_time, x.text) for x in res],
                         [(1, 0, 15, "a\nb\nc\nd"), (3, 20, 25, "e\nf")])

    def test_merge_again_keeps_growing(self):
        res = lr.merge_with_next(lr.merge_with_next(self.s, 1), 1)
        self.assertEqual([(x.start_time, x.end_time, x.text) for x in res],
                         [(0, 25, "a\nb\nc\nd\ne\nf")])

    def test_last_or_missing_is_unchanged(self):
        self.assertEqual(len(lr.merge_with_next(self.s, 3)), 3)
        self.assertEqual(len(lr.merge_with_next(self.s, 99)), 3)
        self.assertEqual(lr.merge_with_next([], 1), [])


class TitleFromFilenameTest(unittest.TestCase):
    def test_common_names(self):
        cases = {
            "Onde_Eu_Fui.mp3": "Onde Eu Fui",
            "onde_eu_fui_2.mp3": "Onde Eu Fui",
            "03_onde_eu_fui (1).mp3": "Onde Eu Fui",
            "Onde Eu Fui [2].wav": "Onde Eu Fui",
            "onde eu fui.mp3": "Onde Eu Fui",
            "ONDE EU FUI.mp3": "Onde Eu Fui",
            "onde-eu-fui.mp3": "Onde Eu Fui",
            "01 - Onde Eu Fui.mp3": "Onde Eu Fui",
            "1. onde eu fui.mp3": "Onde Eu Fui",
            r"C:\Musicas\onde_eu_fui.mp3": "Onde Eu Fui",
        }
        for name, title in cases.items():
            self.assertEqual(lr.title_from_filename(name), title, name)

    def test_numbers_and_hyphens_that_belong_to_the_name(self):
        self.assertEqual(lr.title_from_filename("22 de Outubro.mp3"), "22 De Outubro")
        self.assertEqual(lr.title_from_filename("Guarda-chuva Azul.mp3"), "Guarda-chuva Azul")
        self.assertEqual(lr.title_from_filename("1.mp3"), "1")


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "sub", "config.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip_and_missing_file(self):
        self.assertEqual(lr.load_config(self.path), {})
        lr.save_config({"bg_image": "C:/fundo.png", "lyric_size": 50}, self.path)
        self.assertEqual(lr.load_config(self.path), {"bg_image": "C:/fundo.png", "lyric_size": 50})

    def test_corrupt_file_is_ignored(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            f.write("{nope")
        self.assertEqual(lr.load_config(self.path), {})

    def test_env_override(self):
        old = os.environ.get("LYRIC_RENDERER_CONFIG")
        os.environ["LYRIC_RENDERER_CONFIG"] = self.path
        try:
            self.assertEqual(lr.config_path(), self.path)
        finally:
            if old is None:
                del os.environ["LYRIC_RENDERER_CONFIG"]
            else:
                os.environ["LYRIC_RENDERER_CONFIG"] = old

    def test_apply_style_skips_wrong_types(self):
        p = lr.Project()
        lr.apply_style(p, {"lyric_size": 40, "fade_duration": 1, "fps": "30",
                           "text_color": "#ff0000", "title": "não é estilo"})
        self.assertEqual((p.lyric_size, p.fade_duration, p.fps, p.text_color, p.title),
                         (40, 1.0, 25, "#ff0000", "Título da Música"))

    def test_style_of_round_trip(self):
        p = lr.Project(lyric_size=33, transparent_bg=False)
        q = lr.Project()
        lr.apply_style(q, lr.style_of(p))
        self.assertEqual(lr.style_of(q), lr.style_of(p))


class OutputPathTest(unittest.TestCase):
    def test_final_output_path(self):
        self.assertEqual(lr.final_output_path("C:/v/a.mp4", False), "C:/v/a.mp4")
        self.assertEqual(lr.final_output_path("C:/v/a.mp4", True), "C:/v/a.webm")
        self.assertEqual(lr.final_output_path("C:/v/a.webm", True), "C:/v/a.webm")

    def test_project_is_transparent(self):
        self.assertTrue(lr.project_is_transparent(lr.Project(transparent_bg=True)))
        self.assertFalse(lr.project_is_transparent(lr.Project(transparent_bg=False)))
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img = f.name
        try:
            self.assertFalse(lr.project_is_transparent(lr.Project(transparent_bg=True, bg_image=img)))
        finally:
            os.remove(img)


class ProjectSerializationTest(unittest.TestCase):
    def test_round_trip(self):
        p = lr.Project(title="Minha Música", lyric_size=40, transparent_bg=False,
                       strophes=[lr.Strophe(1, 0, 2, "a"), lr.Strophe(2, 3, 4, "b")])
        data = json.loads(json.dumps(p.to_dict(), ensure_ascii=False))
        q = lr.Project.from_dict(data)
        self.assertEqual(q, p)

    def test_unknown_keys_are_ignored_and_input_not_mutated(self):
        data = {"title": "X", "future_option": 1,
                "strophes": [{"id": 1, "start_time": 0, "end_time": 1,
                              "text": "a", "future_field": True}]}
        q = lr.Project.from_dict(data)
        self.assertEqual(q.title, "X")
        self.assertEqual(q.strophes[0].text, "a")
        self.assertIn("strophes", data)


if __name__ == "__main__":
    unittest.main()
