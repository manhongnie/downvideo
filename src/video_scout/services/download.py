"""Download lifecycle and immutable destination snapshots, independent of media SDKs."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Callable

from video_scout.domain.models import DownloadTask, Event, EventSink, ScoutError, VideoItem
from video_scout.domain.ports import Repository, VideoDownloader
from video_scout.domain.urls import redact, safe_text


def safe_filename(video: VideoItem) -> str:
    title = safe_text(video.title, 100).replace("\n", " ")
    title = re.sub(r'[\\/<>:"|?*\x00-\x1f]', "_", title).strip(" .")
    title = title[:70]
    while len(title.encode("utf-8")) > 180:
        title = title[:-1]
    # Stable ID is hashed by the domain factory; still reject unexpected caller input.
    from hashlib import sha256
    identifier = sha256(video.id.encode()).hexdigest()[:10]
    return f"{title or 'video'}-{identifier}"


def prepare_directory(directory: str) -> Path:
    if not directory.strip():
        raise ScoutError("下载目录不能为空")
    path = Path(directory).expanduser().resolve()
    try:
        path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            raise OSError("不是目录")
        # Actual write check rather than only os.access (ACLs and read-only mounts).
        import tempfile
        with tempfile.TemporaryFile(dir=path):
            pass
    except OSError as exc:
        raise ScoutError(f"下载目录不可写：{type(exc).__name__}") from exc
    return path


class DownloadService:
    def __init__(self, downloader: VideoDownloader, repository: Repository,
                 on_event: EventSink = lambda event: None,
                 can_download: Callable[[], bool] = lambda: True):
        self.downloader = downloader
        self.repository = repository
        self.on_event = on_event
        self.can_download = can_download
        self.tasks: dict[str, DownloadTask] = {}
        self._running: dict[str, asyncio.Task] = {}
        self._semaphore = asyncio.Semaphore(2)
        self._closing = False

    def _emit(self, task: DownloadTask) -> None:
        self.on_event(Event("download", download=task))

    async def restore(self) -> None:
        for task in await self.repository.list_downloads():
            self.tasks[task.id] = task

    async def enqueue(self, items: list[VideoItem], directory: str, concurrency: int = 2) -> list[DownloadTask]:
        if self._closing:
            raise ScoutError("下载服务正在退出")
        if not self.can_download():
            raise ScoutError("请等待扫描结束或停止并完全退出后再下载")
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
            raise ScoutError("下载并发必须为正整数")
        target = await asyncio.to_thread(prepare_directory, directory)
        if not self._running:
            self._semaphore = asyncio.Semaphore(concurrency)
        created = []
        for video in items:
            if not video.variants:
                continue
            if any(t.video.id == video.id and t.target_dir == str(target) and
                   t.status in {"queued", "downloading", "merging"} for t in self.tasks.values()):
                continue
            task = DownloadTask.create(video, target, safe_filename(video))
            self.tasks[task.id] = task
            await self.repository.save_download(task)
            self._emit(task)
            self._schedule(task)
            created.append(task)
        return created

    def _schedule(self, task: DownloadTask) -> None:
        running = asyncio.create_task(self._execute(task))
        self._running[task.id] = running
        def done(future):
            self._running.pop(task.id, None)
            # Cancellation before the coroutine's first step still needs a durable state.
            if future.cancelled() and task.status == "queued":
                task.status = "cancelled"
                self._emit(task)
            if not future.cancelled():
                future.exception()  # handled within _execute; avoid orphan warnings on DB failure
        running.add_done_callback(done)

    async def _execute(self, task: DownloadTask) -> None:
        try:
            async with self._semaphore:
                task.status = "downloading"
                task.error = ""
                await self.repository.save_download(task)
                self._emit(task)
                def progress(state: str, percent: float | None) -> None:
                    if state in {"downloading", "merging"}:
                        task.status = state
                    task.progress = percent
                    self._emit(task)
                result = await self.downloader.download(task, progress)
                if not result.is_file() or result.stat().st_size <= 0:
                    raise ScoutError("下载器没有生成有效的最终文件")
                if result.resolve().parent != Path(task.target_dir).resolve():
                    raise ScoutError("下载器返回的文件不在任务目标目录")
                task.file_path = str(result.resolve())
                task.progress = 100
                task.status = "completed"
        except asyncio.CancelledError:
            task.status = "cancelled"
            task.error = "已取消；可重试，支持时续传"
        except Exception as exc:
            task.status = "failed"
            task.error = redact(f"{type(exc).__name__}: {exc}")
        finally:
            await self.repository.save_download(task)
            self._emit(task)

    def cancel(self, task_id: str | None = None) -> None:
        for key, future in tuple(self._running.items()):
            if task_id is None or task_id == key:
                if not future.cancelling():
                    future.cancel()

    async def wait(self) -> None:
        while self._running:
            await asyncio.gather(*tuple(self._running.values()), return_exceptions=True)
        # Also covers a task cancelled before _execute began.
        for task in self.tasks.values():
            if task.status == "cancelled":
                await self.repository.save_download(task)

    async def retry(self, task_id: str) -> None:
        if not self.can_download() or self._closing:
            raise ScoutError("扫描或退出期间不能重试下载")
        if not self.tasks:
            await self.restore()
        task = self.tasks.get(task_id)
        if task is None:
            raise ScoutError("找不到下载记录")
        if task.status not in {"failed", "cancelled"} or task.id in self._running:
            raise ScoutError("仅能重试失败或取消的任务")
        await asyncio.to_thread(prepare_directory, task.target_dir)
        task.status, task.error, task.progress = "queued", "", None
        await self.repository.save_download(task)
        self._emit(task)
        self._schedule(task)

    async def retry_all_failed(self) -> tuple[int, dict[str, str]]:
        """Queue every failed task, leaving cancelled and completed tasks untouched.

        A bad destination for one task must not prevent the others from retrying.
        The returned errors are keyed by task ID so the UI can report each failure.
        """
        if not self.can_download() or self._closing:
            raise ScoutError("扫描或退出期间不能重试下载")
        for task in await self.repository.list_downloads():
            self.tasks.setdefault(task.id, task)
        failed_ids = [task.id for task in self.tasks.values() if task.status == "failed"]
        retried = 0
        errors: dict[str, str] = {}
        for task_id in failed_ids:
            try:
                await self.retry(task_id)
            except Exception as exc:
                errors[task_id] = redact(f"{type(exc).__name__}: {exc}")
            else:
                retried += 1
        return retried, errors

    async def close(self) -> None:
        self._closing = True
        self.cancel()
        try:
            await self.wait()
        finally:
            await self.downloader.close()
