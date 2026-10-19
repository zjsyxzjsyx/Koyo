"""Download and unpack the SenseVoice model, so voice input needs no manual steps.

The archive comes from the sherpa-onnx GitHub release (or voice.mirror_url) and is checked against
the SHA-256 GitHub publishes for it before anything is unpacked. Unpacking goes to a temporary folder
with tarfile's "data" filter (no absolute paths, no "..", no links out, no device files) and is moved
into place in one step, so a half-unpacked model never looks installed. An interrupted download
resumes where it stopped. Nothing about the user is sent: it is one GET of a public file.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from . import voice

# From the GitHub release asset (api.github.com/repos/k2-fsa/sherpa-onnx/releases/tags/asr-models).
ARCHIVE_SHA256 = "7d1efa2138a65b0b488df37f8b89e3d91a60676e416f515b952358d83dfd347e"
ARCHIVE_BYTES = 163_002_883
ARCHIVE_NAME = voice.MODEL_URL.rsplit("/", 1)[-1]
REPORT_S = 0.25

Report = Callable[[str, int, int], None]


class InstallError(RuntimeError):
    """Why the model could not be installed; the message is for the user."""


def source_url() -> str:
    return os.getenv("ZHIQIU_VOICE_MIRROR_URL", "").strip() or voice.MODEL_URL


def auto_download() -> str:
    value = os.getenv("ZHIQIU_VOICE_AUTO_DOWNLOAD", "").strip() or "ask"
    return value if value in ("ask", "always", "never") else "ask"


def installed(target: Path | None = None) -> bool:
    target = target or voice.model_dir()
    return all((target / name).is_file() for name in voice.MODEL_FILES)


def _download(url: str, part: Path, report: Report, cancel: threading.Event | None,
              client: Callable[..., httpx.Client], expected: int) -> None:
    have = part.stat().st_size if part.exists() else 0
    if expected and have > expected:
        part.unlink()
        have = 0
    if expected and have == expected:
        return
    headers = {"Range": f"bytes={have}-"} if have else {}
    last = 0.0
    try:
        with client(timeout=httpx.Timeout(30.0, connect=15.0), follow_redirects=True) as session:
            with session.stream("GET", url, headers=headers) as response:
                if response.status_code == 416 and have:
                    # The server holds less than we have: start again.
                    part.unlink()
                    return _download(url, part, report, cancel, client, expected)
                if response.status_code == 200 and have:
                    have = 0  # the server ignored the range; take the whole file
                elif response.status_code not in (200, 206):
                    raise InstallError(f"下载失败（HTTP {response.status_code}）：{url}")
                total = expected or have + int(response.headers.get("content-length", "0") or 0)
                with open(part, "ab" if have else "wb") as out:
                    for chunk in response.iter_bytes():
                        if cancel is not None and cancel.is_set():
                            raise InstallError("已取消下载；再次安装会从断点继续")
                        out.write(chunk)
                        have += len(chunk)
                        now = time.monotonic()
                        if now - last >= REPORT_S:
                            last = now
                            report("download", have, total)
                report("download", have, total)
    except httpx.HTTPError as exc:
        raise InstallError(f"下载中断（{type(exc).__name__}）；再次安装会从断点继续") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def install(target: Path | None = None, report: Report | None = None, cancel: threading.Event | None = None, *,
            url: str | None = None, sha256: str = ARCHIVE_SHA256, expected_bytes: int = ARCHIVE_BYTES,
            client: Callable[..., httpx.Client] = httpx.Client) -> Path:
    """Put the model at `target` (voice.model_dir() by default); a model already there is kept."""
    target = Path(target or voice.model_dir())
    report = report or (lambda stage, done, total: None)
    if installed(target):
        report("done", 1, 1)
        return target
    work = target.parent / ".download"
    work.mkdir(parents=True, exist_ok=True)
    part = work / (ARCHIVE_NAME + ".part")
    _download(url or source_url(), part, report, cancel, client, expected_bytes)
    report("verify", 0, 1)
    if _sha256(part) != sha256:
        part.unlink(missing_ok=True)
        raise InstallError("下载的文件校验不通过（SHA-256 不一致），已删除；请重试，或检查镜像地址")
    report("extract", 0, 1)
    # Short names: Windows paths stop at 260 characters and the model's own folder name is long.
    unpacked = work / f"x{uuid.uuid4().hex[:8]}"
    try:
        try:
            with tarfile.open(part, "r:bz2") as archive:
                archive.extractall(unpacked, filter="data")
        except (tarfile.TarError, OSError, EOFError) as exc:
            raise InstallError(f"模型压缩包已损坏或含不安全的路径：{type(exc).__name__}") from exc
        tops = [item for item in unpacked.iterdir()]
        if len(tops) != 1 or not tops[0].is_dir() or not installed(tops[0]):
            raise InstallError("模型压缩包的内容不是预期的目录结构")
        if target.exists():
            # Only an incomplete model is ever here (a complete one returned above).
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tops[0], target)
    finally:
        shutil.rmtree(unpacked, ignore_errors=True)
    part.unlink(missing_ok=True)
    report("done", 1, 1)
    return target


class Job:
    """The workbench's one background installation: started once, watched through snapshot()."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.cancel = threading.Event()
        self.state: dict[str, Any] = {"stage": "idle", "done": 0, "total": ARCHIVE_BYTES, "error": None}

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self.state)

    def start(self, on_change: Callable[[dict[str, Any]], None] | None = None,
              runner: Callable[..., Path] = install) -> bool:
        """False when an installation is already running."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self.cancel.clear()
            self.state = {"stage": "download", "done": 0, "total": ARCHIVE_BYTES, "error": None}

        def changed(stage: str, done: int, total: int) -> None:
            with self._lock:
                self.state = {**self.state, "stage": stage, "done": done, "total": total}
                snapshot = dict(self.state)
            if on_change is not None:
                on_change(snapshot)

        def run() -> None:
            try:
                runner(report=changed, cancel=self.cancel)
            except Exception as exc:
                with self._lock:
                    self.state = {**self.state, "stage": "error",
                                  "error": str(exc) if isinstance(exc, InstallError) else f"安装失败：{type(exc).__name__}"}
                    snapshot = dict(self.state)
                if on_change is not None:
                    on_change(snapshot)

        with self._lock:
            self._thread = threading.Thread(target=run, name="zhiqiu-voice-install", daemon=True)
            self._thread.start()
        return True


JOB = Job()


def describe() -> dict[str, Any]:
    """What the page and the terminal say before downloading."""
    return {"source": urlsplit(source_url()).hostname or source_url(), "url": source_url(),
            "size_mb": round(ARCHIVE_BYTES / 1_000_000), "target": str(voice.model_dir())}
