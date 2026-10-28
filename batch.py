"""
Batch mode: many songs at once.

Each song (BatchItem) has its audio, title and pasted lyrics. The runner goes
through the queue one song at a time: generates the timed lyrics and/or
renders the video. The heavy work is injected (generate/render functions), so
this module doesn't depend on Tk or on the renderer and is easy to test.

The batch is saved to a JSON file after every change, so closing the app
(or a crash) doesn't lose it.
"""

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from typing import Callable, Iterable, List, Optional

# Status shown in the list (derived from the item, see BatchItem.status)
PENDING = "pending"        # no lyrics yet
LYRICS = "lyrics"          # lyrics generated, not reviewed
REVIEWED = "reviewed"      # lyrics checked by the user
DONE = "done"              # video rendered
ERROR = "error"
GENERATING = "generating"  # transient, while the runner works on it
RENDERING = "rendering"

STATUS_LABELS = {
    PENDING: "sem legenda",
    LYRICS: "legenda pronta",
    REVIEWED: "revisada",
    DONE: "vídeo pronto",
    ERROR: "erro",
    GENERATING: "gerando legenda…",
    RENDERING: "renderizando…",
}


class Cancelled(Exception):
    """Raised by generate/render functions when the user stops the batch."""


@dataclass
class BatchItem:
    audio_file: str
    title: str
    lyrics: str = ""
    # Strophes as plain dicts (Strophe fields): this module stays independent
    # of the app's classes and the JSON is the same as in a .lyr file
    strophes: List[dict] = field(default_factory=list)
    reviewed: bool = False
    rendered: bool = False
    output_dir: str = ""       # overrides the batch folder for this song
    output_file: str = ""      # where the last render went
    error: str = ""
    busy: str = ""             # GENERATING / RENDERING while running (not saved)

    @property
    def status(self) -> str:
        if self.busy:
            return self.busy
        if self.error:
            return ERROR
        if self.rendered:
            return DONE
        if self.strophes:
            return REVIEWED if self.reviewed else LYRICS
        return PENDING

    def set_strophes(self, strophes: List[dict]):
        """New lyrics: the previous review and video no longer apply."""
        self.strophes = strophes
        self.reviewed = False
        self.rendered = False
        self.error = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("busy")
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "BatchItem":
        valid = {f for f in cls.__dataclass_fields__} - {"busy"}
        return cls(**{k: v for k, v in d.items() if k in valid})


def find_lyrics_file(audio_path: str) -> str:
    """Lyrics saved next to the audio with the same name (musica.mp3 → musica.txt)."""
    base = os.path.splitext(audio_path)[0]
    folder, name = os.path.split(base)
    try:
        candidates = os.listdir(folder or ".")
    except OSError:
        return ""
    for f in candidates:  # case-insensitive, like Windows
        stem, ext = os.path.splitext(f)
        if ext.lower() == ".txt" and stem.lower() == name.lower():
            for enc in ("utf-8-sig", "cp1252"):
                try:
                    with open(os.path.join(folder, f), encoding=enc) as fh:
                        return fh.read().strip()
                except UnicodeDecodeError:
                    continue
                except OSError:
                    return ""
    return ""


def new_items(paths: Iterable[str], title_fn: Callable[[str], str],
              existing: Iterable[BatchItem] = ()) -> List[BatchItem]:
    """Items for the chosen audio files (skips ones already in the batch)."""
    have = {os.path.normcase(os.path.abspath(i.audio_file)) for i in existing}
    out = []
    for p in paths:
        key = os.path.normcase(os.path.abspath(p))
        if key in have:
            continue
        have.add(key)
        out.append(BatchItem(audio_file=p, title=title_fn(p), lyrics=find_lyrics_file(p)))
    return out


def output_path(item: BatchItem, folder: str, safe_name: Callable[[str], str],
                ext: str = ".mp4") -> str:
    return os.path.join(item.output_dir or folder, safe_name(item.title) + ext)


def save_batch(path: str, items: List[BatchItem], folder: str):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"folder": folder, "items": [i.to_dict() for i in items]},
                      f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass  # never interrupt the user over the autosave


def load_batch(path: str):
    """(items, folder); an empty batch if the file is missing or broken."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        items = [BatchItem.from_dict(d) for d in data.get("items", []) if isinstance(d, dict)]
        return items, data.get("folder", "") if isinstance(data.get("folder"), str) else ""
    except (OSError, ValueError, TypeError, AttributeError):
        return [], ""


class BatchRunner:
    """
    Processes songs one at a time (Whisper and the renderer already use the
    whole CPU, running two at once would only make both slower).

    generate(item, progress, cancel) -> list of strophe dicts
    render(item, out_path, progress, cancel) -> (ok, message)
    on_event(kind, index, value) is called from the worker thread with:
      "start"    value = GENERATING / RENDERING
      "progress" value = 0–1
      "item"     value = None (item finished one step, see item.status/error)
      "finished" value = summary dict
    """

    def __init__(self, items: List[BatchItem], generate: Callable, render: Callable,
                 out_path: Callable[[BatchItem], str], on_event: Callable):
        self.items = items
        self.generate = generate
        self.render = render
        self.out_path = out_path
        self.on_event = on_event
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def run(self, indices: List[int], do_lyrics: bool, do_render: bool,
            skip_render: Iterable[int] = ()):
        """Lyrics for songs without them, then the video (if asked), song by song."""
        skip = set(skip_render)
        summary = {"lyrics": 0, "videos": 0, "errors": 0, "cancelled": False}
        for i in indices:
            if self.cancelled:
                break
            item = self.items[i]
            try:
                if do_lyrics and not item.strophes:
                    self._step(i, GENERATING)
                    strophes = self.generate(item, lambda p, i=i: self.on_event("progress", i, p),
                                             self._cancel.is_set)
                    item.busy = ""
                    if not strophes:
                        raise RuntimeError("Nenhuma estrofe foi gerada.")
                    item.set_strophes(strophes)
                    summary["lyrics"] += 1
                    self.on_event("item", i, None)
                if do_render and item.strophes and not item.rendered and i not in skip:
                    out = self.out_path(item)
                    self._step(i, RENDERING)
                    ok, msg = self.render(item, out, lambda p, i=i: self.on_event("progress", i, p),
                                          self._cancel.is_set)
                    item.busy = ""
                    if self.cancelled:
                        raise Cancelled()
                    if not ok:
                        raise RuntimeError(msg)
                    item.rendered, item.output_file = True, msg
                    summary["videos"] += 1
                    self.on_event("item", i, None)
            except Cancelled:
                item.busy = ""
                self.on_event("item", i, None)
                break
            except Exception as e:  # one bad song doesn't stop the others
                item.busy = ""
                item.error = str(e) or e.__class__.__name__
                summary["errors"] += 1
                self.on_event("item", i, None)
        summary["cancelled"] = self.cancelled
        self.on_event("finished", -1, summary)
        return summary

    def _step(self, i: int, what: str):
        item = self.items[i]
        item.busy, item.error = what, ""
        self.on_event("start", i, what)
