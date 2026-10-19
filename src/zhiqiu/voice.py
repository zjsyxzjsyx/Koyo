"""Local voice input: SenseVoice-Small through sherpa-onnx on the CPU, never a cloud service.

Installed with the base dependencies since 9.0; the model is downloaded by voice_install (the workbench
asks on first use, or `zhiqiu voice install`). The workbench sends a 16 kHz mono WAV,
gets the transcript back and shows it in the input box; nothing is sent until the user does. Audio stays in
memory and is dropped after transcribing. SenseVoice has no hotwords, so terms from the user's own knowledge
points fix same-sounding mistakes afterwards (中续 → 中序), each shown and undoable.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import sqlite3
import threading
import time
import unicodedata
import wave
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

MODEL_NAME = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
             "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2")
MODEL_FILES = ("model.int8.onnx", "tokens.txt")
SAMPLE_RATE = 16_000
MIN_SECONDS = 0.3
MAX_SECONDS = 60
MAX_WAV_BYTES = 2 * 1024 * 1024
LANGUAGES = ("auto", "zh", "en", "ja", "ko", "yue")
# Terms come from the knowledge points and the latest lessons (section titles, diagram labels).
RECENT_LESSONS = 10
_TAG = re.compile(r"<\|[^|>]*\|>")
_SPLIT = re.compile(r"[（）()\[\]【】「」《》“”\"'、，,；;：:。.！!？?/\\|·\s]+|与|和")
_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9+#.\-]*[A-Za-z0-9+#]|[A-Za-z]{2,}")


class VoiceError(ValueError):
    """Audio the service will not transcribe; the message says why, for the page."""


class Busy(RuntimeError):
    """Another recording is being transcribed; one at a time."""


def model_dir() -> Path:
    configured = os.getenv("ZHIQIU_VOICE_MODEL")
    if configured:
        return Path(configured)
    root = os.getenv("LOCALAPPDATA")
    return (Path(root) if root else Path.home() / ".local" / "share") / "zhiqiu" / "voice" / MODEL_NAME


def language() -> str:
    value = (os.getenv("ZHIQIU_VOICE_LANGUAGE") or "auto").strip().lower()
    if value not in LANGUAGES:
        raise ValueError(f"ZHIQIU_VOICE_LANGUAGE 只能是 {'、'.join(LANGUAGES)}")
    return value


def threads() -> int:
    value = os.getenv("ZHIQIU_VOICE_THREADS")
    if value:
        try:
            parsed = int(value)
        except ValueError as exc:
            raise ValueError("ZHIQIU_VOICE_THREADS 必须是正整数") from exc
        if parsed < 1:
            raise ValueError("ZHIQIU_VOICE_THREADS 必须是正整数")
        return parsed
    return max(1, min(4, os.cpu_count() or 1))


def status(directory: Path | None = None) -> dict[str, Any]:
    """Whether voice input can be offered, without loading the model."""
    directory = directory or model_dir()
    from . import voice_install
    if importlib.util.find_spec("sherpa_onnx") is None or importlib.util.find_spec("numpy") is None:
        reason = "not_installed"
    elif voice_install.JOB.running:
        reason = "downloading"
    elif not all((directory / name).is_file() for name in MODEL_FILES):
        reason = "model_missing"
    else:
        reason = "ok"
    try:
        lang = language()
    except ValueError:
        lang, reason = "auto", ("bad_language" if reason == "ok" else reason)
    return {"available": reason == "ok", "reason": reason, "model": str(directory), "language": lang,
            "max_seconds": MAX_SECONDS}


REASONS = {"not_installed": "依赖未安装（在项目目录运行 uv sync）", "model_missing": "还没有下载语音模型",
           "downloading": "正在下载语音模型",
           "bad_language": "ZHIQIU_VOICE_LANGUAGE 取值无效", "ok": "可用"}


def parse_wav(data: bytes) -> tuple[bytes, int]:
    """The PCM frames of a 16-bit mono 16 kHz WAV and its length in milliseconds."""
    if len(data) > MAX_WAV_BYTES:
        raise VoiceError(f"录音超过 {MAX_WAV_BYTES // (1024 * 1024)} MB")
    try:
        with wave.open(io.BytesIO(data), "rb") as reader:
            if reader.getcomptype() != "NONE":
                raise VoiceError("只接受未压缩的 PCM WAV")
            if reader.getnchannels() != 1:
                raise VoiceError("只接受单声道录音")
            if reader.getsampwidth() != 2:
                raise VoiceError("只接受 16 位采样")
            if reader.getframerate() != SAMPLE_RATE:
                raise VoiceError(f"采样率必须是 {SAMPLE_RATE} Hz")
            frames = reader.readframes(reader.getnframes())
    except VoiceError:
        raise
    except Exception as exc:  # wave raises Error, EOFError, RuntimeError or struct.error on bad input
        raise VoiceError(f"不是有效的 WAV：{type(exc).__name__}") from exc
    duration_ms = len(frames) // 2 * 1000 // SAMPLE_RATE
    if duration_ms < MIN_SECONDS * 1000:
        raise VoiceError("录音太短，按住说话，松开结束")
    if duration_ms > MAX_SECONDS * 1000:
        raise VoiceError(f"录音超过 {MAX_SECONDS} 秒")
    return frames, duration_ms


def _samples(frames: bytes) -> Any:
    try:
        import numpy
    except ImportError:  # only the fake recognizer in tests gets here
        from array import array
        values = array("h")
        values.frombytes(frames)
        return [value / 32768 for value in values]
    return numpy.frombuffer(frames, dtype="<i2").astype(numpy.float32) / 32768


def _sense_voice(directory: Path, lang: str, count: int) -> Any:
    import sherpa_onnx
    return sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(directory / "model.int8.onnx"), tokens=str(directory / "tokens.txt"), num_threads=count,
        use_itn=True, language=lang, debug=False)


class Transcriber:
    """One recognizer per process, loaded on first use; one transcription at a time."""

    def __init__(self, directory: Path | None = None, lang: str | None = None, count: int | None = None,
                 factory: Callable[[Path, str, int], Any] = _sense_voice):
        self.directory = directory or model_dir()
        self.language = lang or language()
        self.threads = count or threads()
        self.factory = factory
        self._recognizer: Any = None
        self._lock = threading.Lock()

    def transcribe(self, data: bytes) -> dict[str, Any]:
        frames, duration_ms = parse_wav(data)
        if not self._lock.acquire(blocking=False):
            raise Busy("上一段还在识别")
        try:
            began = time.perf_counter()
            if self._recognizer is None:
                self._recognizer = self.factory(self.directory, self.language, self.threads)
            stream = self._recognizer.create_stream()
            stream.accept_waveform(SAMPLE_RATE, _samples(frames))
            self._recognizer.decode_stream(stream)
            text = _TAG.sub("", getattr(stream.result, "text", "") or "").strip()
            elapsed_ms = int((time.perf_counter() - began) * 1000)
        finally:
            self._lock.release()
            del frames  # the audio is never kept
        return {"raw": text, "duration_ms": duration_ms, "elapsed_ms": elapsed_ms, "language": self.language}


# ----- terms -------------------------------------------------------------------------------------------

def _is_cjk(char: str) -> bool:
    return "CJK" in unicodedata.name(char, "")


def split_terms(texts: Iterable[str]) -> list[str]:
    """Pieces of titles worth correcting toward: runs of at least two Chinese characters, and English words."""
    found: list[str] = []
    for text in texts:
        for piece in _SPLIT.split(text or ""):
            piece = piece.strip()
            if not piece:
                continue
            for word in _LATIN.findall(piece):
                if len(word) >= 2 and word not in found:
                    found.append(word)
            for run in re.findall(r"[㐀-鿿]{2,}", piece):
                if run not in found:
                    found.append(run)
    return found


def terms(db: sqlite3.Connection) -> list[str]:
    titles = [row[0] for row in db.execute("SELECT title FROM knowledge_points WHERE state='active'")]
    for row in db.execute("""SELECT payload_json FROM events WHERE type='LessonSections'
            ORDER BY id DESC LIMIT ?""", (RECENT_LESSONS,)):
        try:
            sections = json.loads(row[0]).get("sections", [])
        except (ValueError, AttributeError):
            continue
        for section in sections:
            titles.append(section.get("title", ""))
            diagram = section.get("diagram") or {}
            titles.extend(node.get("label", "") for node in diagram.get("nodes", []))
    return split_terms(titles)


def _pinyin_function() -> Callable[[str], list[str]] | None:
    try:
        from pypinyin import lazy_pinyin
    except ImportError:
        return None
    return lambda text: lazy_pinyin(text, errors="ignore")


def correct(text: str, known: Sequence[str],
            pinyin: Callable[[str], list[str]] | None = None) -> tuple[str, list[dict[str, Any]]]:
    """Replace same-sounding characters with a known term (longest first) and fix the case of English terms.

    Every change is returned as {from, to, at}; a window that already reads as some term is left alone."""
    pinyin = pinyin if pinyin is not None else _pinyin_function()
    chars = list(text)
    taken = [False] * len(chars)
    replacements: list[dict[str, Any]] = []
    chinese = sorted({term for term in known if all(_is_cjk(char) for char in term) and len(term) >= 2},
                     key=lambda term: (-len(term), term))
    if pinyin is not None and chinese:
        sounds = {term: pinyin(term) for term in chinese}
        own: list[str | None] = [pinyin(char)[0] if _is_cjk(char) and pinyin(char) else None for char in chars]
        for term in chinese:
            size = len(term)
            if len(sounds[term]) != size:
                continue
            for start in range(len(chars) - size + 1):
                window = "".join(chars[start:start + size])
                if (any(taken[start:start + size]) or window == term or window in chinese
                        or own[start:start + size] != sounds[term]):
                    continue
                replacements.append({"from": window, "to": term, "at": start})
                chars[start:start + size] = list(term)
                for index in range(start, start + size):
                    taken[index] = True
    corrected = "".join(chars)
    latin = {term.lower(): term for term in known if _LATIN.fullmatch(term)}

    def fix(match: re.Match[str]) -> str:
        word = match.group(0)
        wanted = latin.get(word.lower())
        if wanted and wanted != word:
            replacements.append({"from": word, "to": wanted, "at": match.start()})
            return wanted
        return word
    corrected = _LATIN.sub(fix, corrected)
    replacements.sort(key=lambda item: item["at"])
    return corrected, replacements


# ----- evaluation --------------------------------------------------------------------------------------

def _normalized(text: str) -> str:
    return "".join(char.lower() for char in text if char.isalnum() or _is_cjk(char))


def _distance(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, left in enumerate(a, 1):
        current = [i]
        for j, right in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (left != right)))
        previous = current
    return previous[-1]


def cer(reference: str, hypothesis: str) -> float:
    ref, hyp = _normalized(reference), _normalized(hypothesis)
    return _distance(ref, hyp) / max(1, len(ref))


def evaluate(folder: Path, transcriber: Transcriber, known: Sequence[str],
             pinyin: Callable[[str], list[str]] | None = None) -> dict[str, Any]:
    """Each x.wav against x.txt: character error rate, terms right before and after correction, speed."""
    items = []
    for wav in sorted(folder.glob("*.wav")):
        reference_file = wav.with_suffix(".txt")
        if not reference_file.is_file():
            continue
        reference = reference_file.read_text(encoding="utf-8").strip()
        result = transcriber.transcribe(wav.read_bytes())
        fixed, changes = correct(result["raw"], known, pinyin)
        wanted = [term for term in known if term.lower() in reference.lower()]
        items.append({"file": wav.name, "reference": reference, "raw": result["raw"], "corrected": fixed,
                      "cer_raw": round(cer(reference, result["raw"]), 4),
                      "cer": round(cer(reference, fixed), 4), "replacements": changes,
                      "terms": len(wanted),
                      "terms_raw": sum(term.lower() in result["raw"].lower() for term in wanted),
                      "terms_corrected": sum(term.lower() in fixed.lower() for term in wanted),
                      "duration_ms": result["duration_ms"], "elapsed_ms": result["elapsed_ms"]})
    total_terms = sum(item["terms"] for item in items)
    audio = sum(item["duration_ms"] for item in items)
    return {"files": len(items), "language": transcriber.language, "items": items,
            "cer_raw": round(sum(item["cer_raw"] for item in items) / len(items), 4) if items else None,
            "cer": round(sum(item["cer"] for item in items) / len(items), 4) if items else None,
            "term_accuracy_raw": round(sum(item["terms_raw"] for item in items) / total_terms, 4) if total_terms else None,
            "term_accuracy": round(sum(item["terms_corrected"] for item in items) / total_terms, 4) if total_terms else None,
            "rtf": round(sum(item["elapsed_ms"] for item in items) / audio, 4) if audio else None}
