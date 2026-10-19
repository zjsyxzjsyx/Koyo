"""9.0: the voice model downloads itself: resume, SHA-256, a safe unpack, one job at a time, and the HTTP route."""
import hashlib
import io
import tarfile
import threading
import time

import httpx
import pytest

from zhiqiu import voice, voice_install
from zhiqiu.voice_install import InstallError, Job, install

from test_workbench_http import call, served, wait_event  # noqa: F401  (served is a fixture)

# Any single top folder will do; a short one keeps the test paths under Windows' 260 characters.
TOP = "model"


def archive(members):
    """A .tar.bz2 in memory; members: name -> bytes (None for a directory)."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:bz2") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            if data is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


GOOD = archive({TOP: None, f"{TOP}/model.int8.onnx": b"onnx" * 1000, f"{TOP}/tokens.txt": b"a 0\n",
                f"{TOP}/test_wavs/zh.wav": b"RIFF"})


class Mirror:
    """Serves one file with Range support; `cut` breaks the first response after that many bytes."""

    def __init__(self, data, cut=None, status=None):
        self.data, self.cut, self.status = data, cut, status
        self.ranges = []

    def client(self, **kwargs):
        return httpx.Client(transport=httpx.MockTransport(self.handle), **kwargs)

    def handle(self, request):
        if self.status:
            return httpx.Response(self.status)
        header = request.headers.get("range")
        self.ranges.append(header)
        start = int(header.split("=")[1].rstrip("-")) if header else 0
        body = self.data[start:]
        status = 206 if header else 200
        if self.cut is not None:
            cut, self.cut = self.cut, None

            class Broken(httpx.SyncByteStream):
                def __iter__(self):
                    yield body[:cut]
                    raise httpx.ReadError("reset")
            return httpx.Response(status, stream=Broken())
        return httpx.Response(status, content=body)


def run(tmp_path, mirror, data=GOOD, **kwargs):
    stages = []
    target = tmp_path / "voice" / TOP
    result = install(target, lambda stage, done, total: stages.append(stage), url="https://mirror.example/m.tar.bz2",
                     sha256=hashlib.sha256(data).hexdigest(), expected_bytes=len(data), client=mirror.client, **kwargs)
    return result, stages


def test_an_interrupted_download_resumes_and_the_model_lands_in_one_piece(tmp_path):
    half = len(GOOD) // 2
    mirror = Mirror(GOOD, cut=half)
    with pytest.raises(InstallError, match="从断点继续"):
        run(tmp_path, mirror)
    target = tmp_path / "voice" / TOP
    assert not target.exists()
    result, stages = run(tmp_path, mirror)
    assert mirror.ranges == [None, f"bytes={half}-"]
    assert result == target and voice_install.installed(target)
    assert (target / "tokens.txt").read_bytes() == b"a 0\n"
    assert stages[-3:] == ["verify", "extract", "done"] and "download" in stages
    assert not list((tmp_path / "voice" / ".download").iterdir())
    # Installed already: no request at all.
    again = Mirror(b"", status=500)
    assert run(tmp_path, again)[0] == target and again.ranges == []


def test_a_wrong_checksum_deletes_the_file_and_installs_nothing(tmp_path):
    target = tmp_path / "voice" / TOP
    with pytest.raises(InstallError, match="SHA-256"):
        install(target, url="https://mirror.example/m", sha256="0" * 64, expected_bytes=len(GOOD),
                client=Mirror(GOOD).client)
    assert not target.exists() and not list((tmp_path / "voice" / ".download").iterdir())


@pytest.mark.parametrize("members, message", [
    ({"../evil.txt": b"x", f"{TOP}/model.int8.onnx": b"m", f"{TOP}/tokens.txt": b"t"}, "不安全"),
    # The data filter strips the leading slash; the extra folder then breaks the expected layout.
    ({"/abs/evil.txt": b"x", f"{TOP}/model.int8.onnx": b"m", f"{TOP}/tokens.txt": b"t"}, "目录结构"),
    ({"a/model.int8.onnx": b"m", "b/tokens.txt": b"t"}, "目录结构"),
    ({f"{TOP}/model.int8.onnx": b"m"}, "目录结构"),
])
def test_unsafe_or_unexpected_archives_are_refused(tmp_path, members, message):
    data = archive(members)
    with pytest.raises(InstallError, match=message):
        run(tmp_path, Mirror(data), data=data)
    assert not (tmp_path / "voice" / TOP).exists() and not (tmp_path / "evil.txt").exists()
    assert not (tmp_path / "voice" / "evil.txt").exists()


def test_http_errors_are_reported(tmp_path):
    with pytest.raises(InstallError, match="HTTP 404"):
        run(tmp_path, Mirror(GOOD, status=404))


def test_one_job_at_a_time_and_its_progress(monkeypatch):
    gate = threading.Event()
    seen = []

    def slow(report, cancel):
        report("download", 10, 100)
        gate.wait(5)
        report("done", 1, 1)

    job = Job()
    assert job.start(seen.append, runner=slow) is True
    assert job.start(seen.append, runner=slow) is False
    monkeypatch.setattr(voice_install, "JOB", job)
    monkeypatch.setattr(voice.importlib.util, "find_spec", lambda name: object())
    assert voice.status()["reason"] == "downloading" and not voice.status()["available"]
    gate.set()
    deadline = time.monotonic() + 5
    while job.running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert [item["stage"] for item in seen] == ["download", "done"] and job.snapshot()["stage"] == "done"

    def broken(report, cancel):
        raise InstallError("下载中断（ReadError）；再次安装会从断点继续")
    assert job.start(seen.append, runner=broken)
    while job.running:
        time.sleep(0.01)
    assert job.snapshot()["stage"] == "error" and "断点" in job.snapshot()["error"]


def test_the_workbench_route_starts_one_download_and_streams_progress(served, monkeypatch, tmp_path):
    server, bench, _ = served
    monkeypatch.setattr(voice, "status", lambda directory=None: {
        "available": False, "reason": "model_missing", "model": "x", "language": "auto", "max_seconds": 60})
    gate = threading.Event()

    def fake(report, cancel):
        report("download", 50, 100)
        gate.wait(5)
        report("done", 1, 1)
    job = Job()
    monkeypatch.setattr(voice_install, "JOB", job)
    real_start = job.start
    monkeypatch.setattr(job, "start", lambda on_change=None: real_start(on_change, runner=fake))
    assert call(server, "/api/voice/install", "POST", body={}, token=False)[0] == 403
    status, data = call(server, "/api/voice/install", "POST", body={})
    assert status == 202 and data["started"] and data["size_mb"] == 163
    assert call(server, "/api/voice/install", "POST", body={})[0] == 409
    event, _ = wait_event(server, ("voice_install",))
    assert event["stage"] == "download" and event["done"] == 50
    server._transcriber = object()
    gate.set()
    deadline = time.monotonic() + 5
    while job.running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server._transcriber is None
    state = call(server, "/api/chat/state")[1]["voice"]
    assert state["installable"] is True and state["install"]["stage"] == "done"


def test_never_means_the_route_refuses(served, monkeypatch):
    server, _, _ = served
    monkeypatch.setenv("ZHIQIU_VOICE_AUTO_DOWNLOAD", "never")
    monkeypatch.setattr(voice, "status", lambda directory=None: {
        "available": False, "reason": "model_missing", "model": "x", "language": "auto", "max_seconds": 60})
    status, data = call(server, "/api/voice/install", "POST", body={})
    assert status == 400 and "never" in data["error"]
    assert call(server, "/api/chat/state")[1]["voice"]["installable"] is False
