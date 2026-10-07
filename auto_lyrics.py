"""
Automatic lyric timing with Whisper (faster-whisper), running locally on CPU.

Two modes:
- Transcribe: no lyrics given — Whisper writes the lyrics, lines are grouped
  into strophes by the pauses in the singing.
- Align: the lyrics are given (strophes separated by blank lines) — Whisper
  transcribes with the lyrics as a hint, then each lyric word is matched to a
  transcribed word to find when every strophe starts and ends. The text in the
  result is exactly the given lyrics.

Pure helpers (normalize_word, group_lines, align_lyrics…) don't need Whisper
and are unit-tested; transcribe_words() is the only part that runs the model.
"""

import difflib
import os
import re
import subprocess
import sys
import types
import unicodedata
import wave
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

SAMPLE_RATE = 16000

MODELS = {
    "small": "Rápido (small)",
    "medium": "Preciso (medium)",
}
DEFAULT_MODEL = "small"
MODEL_REPOS = {
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
}
MODEL_DOWNLOAD_SIZE = {"small": "~0,5 GB", "medium": "~1,5 GB"}

# Strophe grouping (transcribe mode)
LINE_GAP = 0.6         # pause between words that starts a new line (s)
STROPHE_GAP = 1.8      # pause between lines that starts a new strophe (s)
MAX_LINE_WORDS = 9
MAX_STROPHE_LINES = 4

# Padding around the sung words, so fades don't eat the first/last syllable
PAD_BEFORE = 0.3
PAD_AFTER = 0.4
MIN_GAP = 0.1          # keep strophes from touching


class AutoLyricsError(Exception):
    """Error with a message that can be shown to the user as is."""


class Cancelled(Exception):
    pass


@dataclass
class Word:
    text: str
    start: float
    end: float


@dataclass
class TimedStrophe:
    start: float
    end: float
    text: str


# ─────────────────────────────────────────────
#  Audio
# ─────────────────────────────────────────────

def _subprocess_flags():
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def load_audio(path: str):
    """Decode any audio file to mono float32 at 16 kHz (numpy array).
    Uses ffmpeg; .wav files also work without it."""
    import numpy as np

    if not path or not os.path.isfile(path):
        raise AutoLyricsError("Arquivo de áudio não encontrado.")

    ffmpeg_error = None
    try:
        out = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-i", path,
             "-f", "f32le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"],
            capture_output=True, creationflags=_subprocess_flags(),
        )
        if out.returncode == 0 and out.stdout:
            return np.frombuffer(out.stdout, dtype=np.float32).copy()
        ffmpeg_error = out.stderr.decode(errors="replace").strip()[-300:]
    except OSError as e:  # not installed, or blocked by Windows
        ffmpeg_error = str(e)
        if getattr(e, "winerror", None) == 4551:
            ffmpeg_error = "O Windows bloqueou o FFmpeg (Smart App Control)."

    if path.lower().endswith(".wav"):
        return _load_wav(path)

    raise AutoLyricsError(
        "Não foi possível ler o áudio com o FFmpeg.\n"
        f"{ffmpeg_error}\n\nDica: arquivos .wav funcionam mesmo sem FFmpeg.")


def _load_wav(path: str):
    """Plain-Python WAV reader (PCM 8/16/24/32-bit), resampled to 16 kHz."""
    import numpy as np

    with wave.open(path, "rb") as w:
        channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        raw = w.readframes(w.getnframes())

    if width == 1:
        data = (np.frombuffer(raw, np.uint8).astype(np.float32) - 128) / 128
    elif width == 2:
        data = np.frombuffer(raw, "<i2").astype(np.float32) / 32768
    elif width == 3:
        b = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.int32)
        ints = (b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16))
        ints = np.where(ints & 0x800000, ints - 0x1000000, ints)
        data = ints.astype(np.float32) / 8388608
    elif width == 4:
        data = np.frombuffer(raw, "<i4").astype(np.float32) / 2147483648
    else:
        raise AutoLyricsError(f"Formato WAV não suportado ({width * 8} bits).")

    data = data.reshape(-1, channels).mean(axis=1)
    if rate != SAMPLE_RATE:
        n = int(len(data) * SAMPLE_RATE / rate)
        data = np.interp(np.linspace(0, len(data) - 1, n),
                         np.arange(len(data)), data).astype(np.float32)
    return data.astype(np.float32)


# ─────────────────────────────────────────────
#  Whisper
# ─────────────────────────────────────────────

_model_cache = {}


def is_available() -> bool:
    import importlib.util
    return importlib.util.find_spec("faster_whisper") is not None


def is_downloaded(size: str) -> bool:
    """True if the model files are already in the local Hugging Face cache."""
    if size in _model_cache:
        return True
    try:
        from huggingface_hub import try_to_load_from_cache
        return isinstance(try_to_load_from_cache(MODEL_REPOS[size], "model.bin"), str)
    except Exception:
        return False


def _import_whisper_model():
    # faster-whisper imports PyAV only to decode audio, which we do with
    # ffmpeg instead. PyAV's DLLs can be missing or blocked by Windows Smart
    # App Control, so fall back to a stub module that is never used.
    try:
        import av  # noqa: F401
    except Exception:
        for name in [m for m in sys.modules if m == "av" or m.startswith("av.")]:
            del sys.modules[name]
        sys.modules["av"] = types.ModuleType("av")
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise AutoLyricsError(
            "O Whisper não está instalado.\n\n"
            "Instale com:\n    pip install faster-whisper")
    return WhisperModel


def get_model(size: str):
    if size not in _model_cache:
        WhisperModel = _import_whisper_model()
        threads = max(1, (os.cpu_count() or 2) // 2)  # physical cores
        try:
            _model_cache[size] = WhisperModel(size, device="cpu", compute_type="int8",
                                              cpu_threads=threads)
        except Exception as e:
            raise AutoLyricsError(
                f"Não foi possível carregar o modelo '{size}'.\n"
                f"Na primeira vez ele é baixado da internet.\n\n{e}")
    return _model_cache[size]


def transcribe_words(audio, model_size: str = DEFAULT_MODEL, language: Optional[str] = "pt",
                     prompt: str = "", progress: Optional[Callable[[float], None]] = None,
                     cancel: Optional[Callable[[], bool]] = None) -> List[Word]:
    """Run Whisper on the decoded audio and return every word with its timing."""
    model = get_model(model_size)
    duration = len(audio) / SAMPLE_RATE
    segments, _info = model.transcribe(
        audio,
        language=language or None,
        word_timestamps=True,
        initial_prompt=prompt[:800] or None,
        condition_on_previous_text=False,  # avoids repeating lines forever
        vad_filter=False,                  # the speech detector skips singing
    )
    words = []
    for seg in segments:
        if cancel and cancel():
            raise Cancelled()
        for w in seg.words or []:
            if w.word.strip():
                words.append(Word(w.word.strip(), float(w.start), float(w.end)))
        if progress and duration:
            progress(min(seg.end / duration, 1.0))
    return words


# ─────────────────────────────────────────────
#  Pure helpers
# ─────────────────────────────────────────────

_TAG = re.compile(r"\[[^\]]*\]")  # [Refrão], [pré-refrão final]… (Suno meta tags)


def strip_tags(line: str) -> str:
    """Remove everything inside [ ] — like Suno, which doesn't sing it."""
    return re.sub(r"\s{2,}", " ", _TAG.sub("", line)).strip()


def normalize_word(word: str) -> str:
    """Lowercase, no accents, letters/digits only: 'Está,' → 'esta'."""
    w = unicodedata.normalize("NFKD", word.lower())
    w = "".join(c for c in w if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", w)


def split_lyrics(lyrics: str, ignore_tags: bool = True) -> List[List[str]]:
    """
    Lyrics text → strophes → lines. Blank lines separate strophes.
    With ignore_tags, whatever is inside [ ] is removed, and a line that was
    only a tag (e.g. "[refrão]") also starts a new strophe.
    """
    strophes = []
    for block in re.split(r"\n\s*\n", lyrics.strip()):
        current = []
        for raw in block.split("\n"):
            line = raw.strip()
            if ignore_tags and _TAG.search(line):
                line = strip_tags(line)
                if not line:  # tag-only line: section boundary
                    if current:
                        strophes.append(current)
                    current = []
                    continue
            if line:
                current.append(line)
        if current:
            strophes.append(current)
    return strophes


def _finish(strophes: List[TimedStrophe], audio_end: Optional[float]) -> List[TimedStrophe]:
    """Pad strophes, keep them from overlapping and round to centiseconds."""
    out = []
    for i, s in enumerate(strophes):
        start = max(0.0, s.start - PAD_BEFORE)
        end = s.end + PAD_AFTER
        if out:
            start = max(start, out[-1].end + MIN_GAP)
        if i + 1 < len(strophes):
            end = min(end, max(strophes[i + 1].start - MIN_GAP, s.end))
        if audio_end:
            end = min(end, audio_end)
        end = max(end, start + 0.5)
        out.append(TimedStrophe(round(start, 2), round(end, 2), s.text))
    return out


def group_lines(words: List[Word], audio_end: Optional[float] = None) -> List[TimedStrophe]:
    """Transcribe mode: words → lines (split on short pauses) → strophes
    (split on long pauses or every MAX_STROPHE_LINES lines)."""
    lines = []
    cur = []
    for w in words:
        if cur and w.start - cur[-1].end >= LINE_GAP:
            lines.extend(_split_long_line(cur))
            cur = []
        cur.append(w)
    if cur:
        lines.extend(_split_long_line(cur))

    strophes, block = [], []
    for line in lines:
        if block and (line[0].start - block[-1][-1].end >= STROPHE_GAP
                      or len(block) >= MAX_STROPHE_LINES):
            strophes.append(block)
            block = []
        block.append(line)
    if block:
        strophes.append(block)

    timed = []
    for block in strophes:
        text = "\n".join(_line_text(line) for line in block)
        timed.append(TimedStrophe(block[0][0].start, block[-1][-1].end, text))
    return _finish(timed, audio_end)


def _split_long_line(line: List[Word]) -> List[List[Word]]:
    """Split a line with too many words near its middle, where the singer
    pauses. Whisper folds a pause into the previous word, so the score is the
    gap plus the previous word's duration."""
    if len(line) <= MAX_LINE_WORDS:
        return [line]
    n = len(line)
    candidates = range(max(2, round(n * 0.35)), min(n - 1, round(n * 0.65)) + 1)
    k = max(candidates, key=lambda i: line[i].start - line[i - 1].start)
    return _split_long_line(line[:k]) + _split_long_line(line[k:])


def _line_text(line: List[Word]) -> str:
    text = " ".join(w.text for w in line).strip()
    return text[:1].upper() + text[1:]


def align_lyrics(lyrics: str, words: List[Word],
                 audio_end: Optional[float] = None,
                 ignore_tags: bool = True) -> List[TimedStrophe]:
    """
    Align mode: match the given lyrics to the transcribed words and time each
    strophe by its first and last matched word. Strophes with no match get the
    gap between their neighbours. With ignore_tags=False the [tags] stay in the
    text, but they're never matched against the audio (they aren't sung).
    """
    strophes = split_lyrics(lyrics, ignore_tags)
    if not strophes:
        return []

    lyric_tokens, owner = [], []  # normalized lyric words, strophe index of each
    for i, lines in enumerate(strophes):
        for w in strip_tags(" ".join(lines)).split():
            n = normalize_word(w)
            if n:
                lyric_tokens.append(n)
                owner.append(i)

    heard = [normalize_word(w.text) for w in words]
    spans: List[Optional[Tuple[float, float]]] = [None] * len(strophes)
    matcher = difflib.SequenceMatcher(None, lyric_tokens, heard, autojunk=False)
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            i = owner[block.a + k]
            w = words[block.b + k]
            if spans[i] is None:
                spans[i] = (w.start, w.end)
            else:
                spans[i] = (min(spans[i][0], w.start), max(spans[i][1], w.end))

    end_limit = audio_end or (words[-1].end if words else 10.0 * len(strophes))
    timed = []
    for i, lines in enumerate(strophes):
        if spans[i] is None:
            prev_end = timed[-1].end if timed else 0.0
            nxt = next((s[0] for s in spans[i + 1:] if s), end_limit)
            # strophes without any match share the gap evenly
            missing = 1 + next((j for j, s in enumerate(spans[i + 1:]) if s),
                               len(spans) - i - 1)
            length = max((nxt - prev_end) / missing, 0.5)
            spans[i] = (prev_end, prev_end + length)
        timed.append(TimedStrophe(spans[i][0], spans[i][1], "\n".join(lines)))
    return _finish(timed, audio_end)


def generate(audio_path: str, lyrics: str = "", model_size: str = DEFAULT_MODEL,
             language: Optional[str] = "pt",
             progress: Optional[Callable[[float], None]] = None,
             cancel: Optional[Callable[[], bool]] = None,
             ignore_tags: bool = True) -> List[TimedStrophe]:
    """Full pipeline: audio file (+ optional lyrics) → timed strophes."""
    audio = load_audio(audio_path)
    audio_end = len(audio) / SAMPLE_RATE
    # the hint for Whisper is only what is actually sung: never the [tags]
    prompt = " ".join(" ".join(lines) for lines in split_lyrics(lyrics, True)) if lyrics.strip() else ""
    words = transcribe_words(audio, model_size, language, prompt, progress, cancel)
    if lyrics.strip():
        return align_lyrics(lyrics, words, audio_end, ignore_tags)
    if not words:
        raise AutoLyricsError("Nenhuma voz foi reconhecida no áudio.")
    return group_lines(words, audio_end)
