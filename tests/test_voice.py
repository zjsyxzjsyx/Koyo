import io
import json
import threading
import wave
from datetime import date

import pytest
from typer.testing import CliRunner

from zhiqiu import voice
from zhiqiu.cli import app
from zhiqiu.clock import FixedClock
from zhiqiu.store import Store
from zhiqiu.voice import Busy, Transcriber, VoiceError, correct, parse_wav, split_terms

# A tiny pinyin table so the correction logic is tested without pypinyin installed.
PINYIN = {"中": "zhong", "终": "zhong", "序": "xu", "续": "xu", "须": "xu", "遍": "bian", "变": "bian",
          "历": "li", "力": "li", "前": "qian", "钱": "qian", "后": "hou", "厚": "hou", "层": "ceng",
          "二": "er", "叉": "cha", "树": "shu", "数": "shu", "是": "shi", "先": "xian", "左": "zuo", "根": "gen",
          "再": "zai", "右": "you", "的": "de", "沙": "sha", "箱": "xiang", "想": "xiang"}


def fake_pinyin(text):
    return [PINYIN.get(char, f"?{char}") for char in text]


def wav_bytes(seconds=1.0, rate=16000, channels=1, width=2):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        writer.writeframes(b"\x00\x00" * int(seconds * rate) * channels * (width // 2))
    return buffer.getvalue()


class FakeStream:
    def __init__(self, text):
        self.text = text
        self.waveform = None

    def accept_waveform(self, rate, samples):
        self.waveform = (rate, len(samples))

    @property
    def result(self):
        return type("R", (), {"text": self.text})()


class FakeRecognizer:
    def __init__(self, text="<|zh|><|NEUTRAL|>中续遍历是先左再根再右。", gate=None):
        self.text, self.gate, self.streams = text, gate, []

    def create_stream(self):
        self.streams.append(FakeStream(self.text))
        return self.streams[-1]

    def decode_stream(self, stream):
        if self.gate:
            self.gate.wait(5)


def transcriber(recognizer, loads=None):
    def factory(directory, lang, count):
        if loads is not None:
            loads.append((directory, lang, count))
        return recognizer
    return Transcriber(directory=voice.model_dir(), lang="auto", count=2, factory=factory)


@pytest.mark.parametrize("data, reason", [
    (b"RIFF0000WAVEnot really", "不是有效的 WAV"),
    (wav_bytes(channels=2), "单声道"),
    (wav_bytes(rate=8000), "采样率必须是 16000"),
    (wav_bytes(width=4), "16 位"),
    (wav_bytes(0.2), "录音太短"),
    (wav_bytes(61), "超过 60 秒"),
    (b"\x00" * (voice.MAX_WAV_BYTES + 1), "超过 2 MB"),
], ids=["garbage", "stereo", "8khz", "32bit", "short", "long", "huge"])
def test_bad_audio_is_refused_with_a_reason(data, reason):
    with pytest.raises(VoiceError, match=reason):
        parse_wav(data)


def test_transcribing_loads_once_strips_tags_and_keeps_no_audio(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    loads = []
    recognizer = FakeRecognizer()
    service = transcriber(recognizer, loads)
    first = service.transcribe(wav_bytes(1.5))
    second = service.transcribe(wav_bytes(0.5))
    assert first["raw"] == "中续遍历是先左再根再右。" and first["duration_ms"] == 1500 and second["duration_ms"] == 500
    assert len(loads) == 1 and loads[0][1:] == ("auto", 2)
    assert recognizer.streams[0].waveform == (16000, 24000)
    assert list(tmp_path.iterdir()) == []  # nothing written to disk


def test_only_one_recording_is_transcribed_at_a_time():
    gate = threading.Event()
    service = transcriber(FakeRecognizer(gate=gate))
    worker = threading.Thread(target=service.transcribe, args=(wav_bytes(),))
    worker.start()
    while not getattr(service, "_recognizer", None) or not service._recognizer.streams:
        pass
    with pytest.raises(Busy):
        service.transcribe(wav_bytes())
    gate.set()
    worker.join(5)
    assert service.transcribe(wav_bytes())["raw"]


def test_status_reports_what_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(voice.importlib.util, "find_spec", lambda name: None)
    assert voice.status(tmp_path) == {"available": False, "reason": "not_installed", "model": str(tmp_path),
                                      "language": "auto", "max_seconds": 60}
    monkeypatch.setattr(voice.importlib.util, "find_spec", lambda name: object())
    assert voice.status(tmp_path)["reason"] == "model_missing"
    for name in voice.MODEL_FILES:
        (tmp_path / name).write_bytes(b"x")
    assert voice.status(tmp_path)["available"] is True
    monkeypatch.setenv("ZHIQIU_VOICE_LANGUAGE", "fr")
    assert voice.status(tmp_path)["reason"] == "bad_language"


def test_terms_come_from_titles_lessons_and_diagrams(tmp_path):
    assert split_terms(["二叉树遍历（前序、中序、后序、层序）", "Agent 沙箱的实现原理（隔离机制与架构）", "Python's GIL"]) == [
        "二叉树遍历", "前序", "中序", "后序", "层序", "Agent", "沙箱的实现原理", "隔离机制", "架构", "Python", "GIL"]
    store = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 17)))
    kp = store.ensure_knowledge_point("二叉树遍历（前序、中序）")
    store.record_event(kp, None, "LessonSections", {"message_id": "m", "sections": [
        {"title": "层序遍历", "content": "", "check": "",
         "diagram": {"nodes": [{"id": "a", "label": "队列"}, {"id": "b", "label": "BFS"}]}}]})
    assert voice.terms(store.db) == ["二叉树遍历", "前序", "中序", "层序遍历", "队列", "BFS"]


def test_same_sounding_characters_become_the_term():
    known = ["二叉树遍历", "中序", "前序", "Agent", "GIL"]
    text, changes = correct("中续遍历是先左再根再右，二叉数变历用 agent 和 gil", known, fake_pinyin)
    assert text == "中序遍历是先左再根再右，二叉树遍历用 Agent 和 GIL"
    assert changes == [{"from": "中续", "to": "中序", "at": 0}, {"from": "二叉数变历", "to": "二叉树遍历", "at": 12},
                       {"from": "agent", "to": "Agent", "at": 19}, {"from": "gil", "to": "GIL", "at": 27}]


def test_longer_terms_win_and_correct_text_is_left_alone():
    # 中序 inside 二叉树中序 is taken by the longer term first; text that already is a term never changes.
    text, changes = correct("二叉数终须", ["二叉树中序", "中序"], fake_pinyin)
    assert text == "二叉树中序" and [c["to"] for c in changes] == ["二叉树中序"]
    assert correct("中序和前序", ["中序", "前序"], fake_pinyin) == ("中序和前序", [])


def test_without_a_pinyin_function_chinese_is_untouched(monkeypatch):
    monkeypatch.setattr(voice, "_pinyin_function", lambda: None)
    assert correct("中续 agent", ["中序", "Agent"]) == ("中续 Agent", [{"from": "agent", "to": "Agent", "at": 3}])


def test_evaluation_compares_against_reference_text(tmp_path):
    (tmp_path / "a.wav").write_bytes(wav_bytes(2))
    (tmp_path / "a.txt").write_text("中序遍历是先左再根再右。", encoding="utf-8")
    (tmp_path / "lonely.wav").write_bytes(wav_bytes(1))  # no reference: skipped
    data = voice.evaluate(tmp_path, transcriber(FakeRecognizer("中续遍历是先左再根再右")), ["中序"], fake_pinyin)
    assert data["files"] == 1 and data["cer_raw"] == pytest.approx(1 / 11, abs=1e-4) and data["cer"] == 0
    assert data["term_accuracy_raw"] == 0 and data["term_accuracy"] == 1
    assert data["items"][0]["replacements"] == [{"from": "中续", "to": "中序", "at": 0}]


def test_voice_commands_say_what_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("ZHIQIU_VOICE_MODEL", str(tmp_path / "missing"))
    result = CliRunner().invoke(app, ["--db", str(tmp_path / "state.sqlite"), "voice", "check"])
    assert result.exit_code == 1
    assert "模型目录：" in result.output
