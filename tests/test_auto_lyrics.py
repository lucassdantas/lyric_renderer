"""Automatic lyrics: grouping/alignment logic, WAV loading and (optionally) Whisper itself.

The real-Whisper test downloads a model and takes a while, so it only runs with
LYRIC_SLOW_TESTS=1 and a test file in LYRIC_TEST_AUDIO (+ LYRIC_TEST_LYRICS).
"""
import os
import sys
import tempfile
import unittest
import wave

import auto_lyrics as al
from auto_lyrics import Word


def words_from(spec):
    """'oi:1.0-1.5 tudo:1.5-2.0' → [Word]"""
    out = []
    for item in spec.split():
        text, times = item.rsplit(":", 1)
        a, b = times.split("-")
        out.append(Word(text, float(a), float(b)))
    return out


def sung(text, start, step=0.4):
    """One Word per lyric word, sung back to back from `start`."""
    out = []
    for i, w in enumerate(text.split()):
        out.append(Word(w, start + i * step, start + (i + 1) * step))
    return out


class NormalizeTest(unittest.TestCase):
    def test_normalize_word(self):
        self.assertEqual(al.normalize_word("Está,"), "esta")
        self.assertEqual(al.normalize_word("CORAÇÃO!"), "coracao")
        self.assertEqual(al.normalize_word("pra'"), "pra")
        self.assertEqual(al.normalize_word("..."), "")

    def test_split_lyrics(self):
        lyrics = "[Verso 1]\nLinha a\n  Linha b\n\n(Refrão)\nLinha c\n\n\n[Final]\n"
        self.assertEqual(al.split_lyrics(lyrics), [["Linha a", "Linha b"], ["Linha c"]])
        self.assertEqual(al.split_lyrics("   "), [])


class GroupLinesTest(unittest.TestCase):
    def test_pauses_make_lines_and_strophes(self):
        words = (sung("o sol nasceu", 1.0) + sung("na beira do mar", 3.0)   # 0.8s gap → new line
                 + sung("eu caminhei", 8.0))                                  # 3.4s gap → new strophe
        res = al.group_lines(words)
        self.assertEqual([s.text for s in res],
                         ["O sol nasceu\nNa beira do mar", "Eu caminhei"])
        self.assertAlmostEqual(res[0].start, 1.0 - al.PAD_BEFORE)
        self.assertAlmostEqual(res[1].end, 8.8 + al.PAD_AFTER)

    def test_long_line_split_near_middle_at_pause(self):
        # Whisper puts the pause inside "mar" (long word) — split right after it
        words = sung("o sol nasceu na beira do", 1.0) + [Word("mar", 3.4, 4.6)] \
            + sung("e a cidade acordou cedo", 4.6)
        res = al.group_lines(words)
        self.assertEqual(res[0].text, "O sol nasceu na beira do mar\nE a cidade acordou cedo")

    def test_max_lines_per_strophe(self):
        words = []
        for i in range(6):
            words += sung(f"linha{i} aqui", i * 2.0)  # 1.2s gaps: new line, same strophe
        res = al.group_lines(words)
        self.assertEqual([s.text.count("\n") + 1 for s in res], [al.MAX_STROPHE_LINES, 2])

    def test_strophes_never_overlap_and_respect_audio_end(self):
        words = sung("um dois", 0.1) + sung("tres quatro", 2.0) + sung("cinco", 9.5)
        res = al.group_lines(words, audio_end=10.0)
        for a, b in zip(res, res[1:]):
            self.assertLess(a.end, b.start)
        self.assertGreaterEqual(res[0].start, 0)
        self.assertLessEqual(res[-1].end, 10.0)


class AlignLyricsTest(unittest.TestCase):
    LYRICS = ("[Verso]\nO sol nasceu na beira do mar\nE a cidade começou a acordar\n\n"
              "Eu caminhei sozinho pela praia\n\n"
              "[Refrão]\nMas amanhã é outro dia")

    def test_exact_text_and_timing(self):
        words = (sung("O sol nasceu na beira do mar e a cidade começou a acordar.", 3.0)
                 + sung("Eu caminhei sozinho pela praia", 14.0)
                 + sung("Mas amanhã é outro dia", 25.0))
        res = al.align_lyrics(self.LYRICS, words, audio_end=40)
        self.assertEqual([s.text for s in res], [
            "O sol nasceu na beira do mar\nE a cidade começou a acordar",
            "Eu caminhei sozinho pela praia",
            "Mas amanhã é outro dia"])
        self.assertAlmostEqual(res[0].start, 3.0 - al.PAD_BEFORE)
        self.assertAlmostEqual(res[1].start, 14.0 - al.PAD_BEFORE)
        self.assertAlmostEqual(res[2].end, 25.0 + 5 * 0.4 + al.PAD_AFTER)

    def test_tolerates_misheard_words(self):
        words = (sung("o sol nasceu na veira do bar e a cidade comecou a acorda", 3.0)
                 + sung("eu caminhei sozinha pela praia", 14.0)
                 + sung("mas amanha e outro dia", 25.0))
        res = al.align_lyrics(self.LYRICS, words, audio_end=40)
        self.assertAlmostEqual(res[1].start, 14.0 - al.PAD_BEFORE)
        self.assertAlmostEqual(res[2].start, 25.0 - al.PAD_BEFORE)

    def test_strophe_not_heard_fills_the_gap(self):
        words = sung("O sol nasceu na beira do mar e a cidade começou a acordar", 3.0) \
            + sung("Mas amanhã é outro dia", 25.0)
        res = al.align_lyrics(self.LYRICS, words, audio_end=40)
        self.assertEqual(len(res), 3)
        self.assertGreater(res[1].start, res[0].end)
        self.assertLess(res[1].end, res[2].start)

    def test_empty(self):
        self.assertEqual(al.align_lyrics("", sung("oi", 1)), [])
        res = al.align_lyrics("a b\n\nc d", [], audio_end=20)
        self.assertEqual(len(res), 2)


class LoadAudioTest(unittest.TestCase):
    def _write_wav(self, path, rate, width, channels, frames):
        with wave.open(path, "wb") as w:
            w.setnchannels(channels)
            w.setsampwidth(width)
            w.setframerate(rate)
            w.writeframes(frames)

    def test_wav_reader_resamples_and_mixes_to_mono(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.wav")
            stereo = np.zeros((44100, 2), "<i2")
            stereo[:, 0] = 16384  # left at +0.5, right silent → mono 0.25
            self._write_wav(p, 44100, 2, 2, stereo.tobytes())
            data = al._load_wav(p)
            self.assertEqual(data.dtype, np.float32)
            self.assertAlmostEqual(len(data) / al.SAMPLE_RATE, 1.0, places=2)
            self.assertAlmostEqual(float(data.mean()), 0.25, places=3)

    def test_wav_24bit(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.wav")
            sample = (-4194304).to_bytes(3, "little", signed=True)  # -0.5
            self._write_wav(p, 16000, 3, 1, sample * 100)
            self.assertTrue(np.allclose(al._load_wav(p), -0.5))

    def test_missing_file(self):
        with self.assertRaises(al.AutoLyricsError):
            al.load_audio("nao_existe.mp3")

    def test_wav_works_without_ffmpeg(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.wav")
            self._write_wav(p, 16000, 2, 1, np.zeros(1600, "<i2").tobytes())
            old = os.environ.get("PATH", "")
            os.environ["PATH"] = ""
            try:
                data = al.load_audio(p)
            finally:
                os.environ["PATH"] = old
            self.assertEqual(len(data), 1600)


class ImportTest(unittest.TestCase):
    @unittest.skipUnless(al.is_available(), "faster-whisper não instalado")
    def test_whisper_imports_even_if_pyav_is_blocked(self):
        cls = al._import_whisper_model()
        self.assertEqual(cls.__name__, "WhisperModel")
        self.assertIn("av", sys.modules)


@unittest.skipUnless(os.environ.get("LYRIC_SLOW_TESTS") == "1" and al.is_available()
                     and os.path.isfile(os.environ.get("LYRIC_TEST_AUDIO", "")),
                     "teste lento: defina LYRIC_SLOW_TESTS=1 e LYRIC_TEST_AUDIO")
class RealWhisperTest(unittest.TestCase):
    def test_generate(self):
        audio = os.environ["LYRIC_TEST_AUDIO"]
        lyrics_path = os.environ.get("LYRIC_TEST_LYRICS", "")
        lyrics = open(lyrics_path, encoding="utf-8").read() if os.path.isfile(lyrics_path) else ""
        seen = []
        res = al.generate(audio, lyrics, progress=seen.append)
        self.assertTrue(res)
        if lyrics:
            self.assertEqual(len(res), len(al.split_lyrics(lyrics)))
        for a, b in zip(res, res[1:]):
            self.assertLess(a.end, b.start)
        self.assertTrue(seen)


if __name__ == "__main__":
    unittest.main()
