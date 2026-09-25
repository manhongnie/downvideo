from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from video_scout.adapters.http import HtmlDiscovery, HttpPageFetcher
from video_scout.adapters.media import HybridVideoResolver, YtDlpDownloader
from video_scout.adapters.sqlite import SQLiteRepository
from video_scout.domain.models import DownloadTask, MediaVariant, ScanConfig, ScoutError, VideoItem
from video_scout.services.download import DownloadService, prepare_directory, safe_filename
from video_scout.services.scan import ScanService


def item(number=1, *, title="../中文 / 标题\\test"):
    return VideoItem.create(title=title, source_url="http://example.test/source",
                            page_url="http://example.test/watch",
                            variants=[MediaVariant(f"http://example.test/{number}.mp4", ext="mp4")])


class ControlledDownloader:
    """Service tests only: deterministic download errors and cancellation points."""

    def __init__(self, *, fail_first=False, blocked=False, missing=False):
        self.fail_first = fail_first
        self.missing = missing
        self.release = asyncio.Event()
        self.entered = asyncio.Event()
        if not blocked:
            self.release.set()
        self.attempts = 0
        self.active = 0
        self.maximum_active = 0
        self.cancelled = 0
        self.closed = False
        self.destinations = []

    async def download(self, task, progress):
        self.attempts += 1
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        self.destinations.append(task.target_dir)
        self.entered.set()
        try:
            await self.release.wait()
            if self.fail_first and self.attempts == 1:
                raise ScoutError("temporary failure")
            path = Path(task.target_dir) / (task.filename + ".mp4")
            if not self.missing:
                progress("merging", None)
                path.write_bytes(Path("demo/tiny.mp4").read_bytes())
            return path
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.active -= 1

    async def close(self):
        self.closed = True


async def test_real_scan_select_download_generates_valid_video(demo_site, tmp_path):
    site = demo_site(total=3)
    database = tmp_path / "history.sqlite"
    repo = SQLiteRepository(database)
    fetcher, discovery, resolver = HttpPageFetcher(), HtmlDiscovery(), HybridVideoResolver()
    scan = ScanService(fetcher, discovery, discovery, resolver, repo)
    observed = []
    downloader = YtDlpDownloader()
    downloads = DownloadService(downloader, repo,
                                lambda event: observed.append(event.download.status))
    destination = tmp_path / "中文 视频目录"
    try:
        await scan.run(ScanConfig(site.url, limit=3, host_interval=0, retries=0))
        chosen = list(scan.items.values())[1]
        tasks = await downloads.enqueue([chosen], str(destination))
        await asyncio.wait_for(downloads.wait(), 40)
        assert len(tasks) == 1
        task = tasks[0]
        assert task.status == "completed", task.error
        final = Path(task.file_path)
        assert final.parent == destination.resolve()
        assert final.stat().st_size > 0
        assert final.suffix == ".mp4"
        assert "queued" in observed and "downloading" in observed and "completed" in observed
        if shutil.which("ffprobe"):
            result = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(final)],
                                    capture_output=True, text=True, check=True)
            assert any(stream["codec_type"] == "video" for stream in json.loads(result.stdout)["streams"])
        else:
            pytest.skip("ffprobe is unavailable; real download completed but codec validation was not run")
        original_bytes = final.read_bytes()
        repeated = await downloads.enqueue([chosen], str(destination))
        await asyncio.wait_for(downloads.wait(), 40)
        # Existing output must never be silently replaced.
        assert final.read_bytes() == original_bytes
        assert repeated[0].status == "failed"
        assert "未覆盖" in repeated[0].error
        persisted = await repo.list_downloads()
        assert persisted[0].file_path == task.file_path
    finally:
        await downloads.close()
        await resolver.close()
        await fetcher.close()
        await repo.close()


async def test_failed_task_retry_uses_original_destination_and_persists(tmp_path):
    repo = SQLiteRepository(tmp_path / "history.sqlite")
    adapter = ControlledDownloader(fail_first=True)
    service = DownloadService(adapter, repo)
    original = tmp_path / "原始 路径"
    try:
        [task] = await service.enqueue([item()], str(original))
        await service.wait()
        assert task.status == "failed"
        assert not list(original.glob("*.mp4"))
        await service.retry(task.id)
        await service.wait()
        assert task.status == "completed"
        assert Path(task.file_path).parent == original.resolve()
        assert adapter.destinations == [str(original.resolve())] * 2
        assert (await repo.list_downloads())[0].status == "completed"
    finally:
        await service.close()
        await repo.close()


async def test_retry_all_failed_skips_other_states_and_continues_after_bad_destination(tmp_path):
    repo = SQLiteRepository(tmp_path / "history.sqlite")
    good = tmp_path / "good"
    occupied = tmp_path / "occupied"
    occupied.write_text("not a directory")
    tasks = []
    for number, status, destination in [
        (1, "failed", good), (2, "failed", occupied),
        (3, "cancelled", good), (4, "completed", good),
    ]:
        video = item(number)
        task = DownloadTask.create(video, destination, safe_filename(video))
        task.status = status
        task.error = "old error"
        await repo.save_download(task)
        tasks.append(task)
    adapter = ControlledDownloader()
    service = DownloadService(adapter, repo)
    # A service may already hold newer tasks while older failed tasks remain in SQLite.
    service.tasks[tasks[3].id] = tasks[3]
    try:
        retried, errors = await service.retry_all_failed()
        assert retried == 1
        assert set(errors) == {tasks[1].id}
        assert "下载目录不可写" in errors[tasks[1].id]
        await service.wait()
        assert adapter.attempts == 1
        assert adapter.destinations == [str(good)]
        assert service.tasks[tasks[0].id].id == tasks[0].id
        assert service.tasks[tasks[0].id].filename == tasks[0].filename
        assert [service.tasks[task.id].status for task in tasks] == [
            "completed", "failed", "cancelled", "completed",
        ]
        persisted = {task.id: task for task in await repo.list_downloads()}
        assert [persisted[task.id].status for task in tasks] == [
            "completed", "failed", "cancelled", "completed",
        ]
    finally:
        await service.close()
        await repo.close()


async def test_no_final_file_cannot_be_completed(tmp_path):
    repo = SQLiteRepository(":memory:")
    service = DownloadService(ControlledDownloader(missing=True), repo)
    try:
        [task] = await service.enqueue([item()], str(tmp_path))
        await service.wait()
        assert task.status == "failed"
        assert task.file_path is None
        assert "最终文件" in task.error
    finally:
        await service.close()
        await repo.close()


async def test_cancellation_covers_queued_and_running_tasks_and_can_retry(tmp_path):
    repo = SQLiteRepository(tmp_path / "history.sqlite")
    adapter = ControlledDownloader(blocked=True)
    service = DownloadService(adapter, repo)
    try:
        tasks = await service.enqueue([item(i) for i in range(4)], str(tmp_path), concurrency=1)
        await adapter.entered.wait()
        assert adapter.maximum_active == 1
        service.cancel()
        await service.wait()
        assert all(task.status == "cancelled" for task in tasks)
        assert all(task.file_path is None for task in tasks)
        assert not list(tmp_path.glob("*.mp4"))
        assert adapter.cancelled == 1
        assert all(task.status == "cancelled" for task in await repo.list_downloads())
        adapter.release.set()
        await service.retry(tasks[-1].id)
        await service.wait()
        assert tasks[-1].status == "completed"
    finally:
        await service.close()
        await repo.close()


async def test_downloading_requires_finished_scan(tmp_path):
    repo = SQLiteRepository(":memory:")
    service = DownloadService(ControlledDownloader(), repo, can_download=lambda: False)
    try:
        with pytest.raises(ScoutError, match="扫描"):
            await service.enqueue([item()], str(tmp_path))
        assert not service.tasks
    finally:
        await service.close()
        await repo.close()


async def test_restart_restores_interrupted_task_for_retry(tmp_path):
    repo = SQLiteRepository(tmp_path / "history.sqlite")
    from video_scout.domain.models import DownloadTask
    task = DownloadTask.create(item(), tmp_path, safe_filename(item()))
    task.status = "downloading"
    await repo.save_download(task)
    await repo.close()
    reopened = SQLiteRepository(tmp_path / "history.sqlite")
    service = DownloadService(ControlledDownloader(), reopened)
    try:
        await service.restore()
        assert service.tasks[task.id].status == "cancelled"
        await service.retry(task.id)
        await service.wait()
        assert service.tasks[task.id].status == "completed"
    finally:
        await service.close()
        await reopened.close()


def test_safe_filename_and_invalid_directory(tmp_path):
    name = safe_filename(item(title="../../escape/..\\name\x1b[31m"))
    assert "/" not in name and "\\" not in name and "\x1b" not in name
    assert not name.startswith(".")
    assert name == safe_filename(item(title="../../escape/..\\name\x1b[31m"))
    assert name != safe_filename(item(2, title="../../escape/..\\name\x1b[31m"))
    occupied = tmp_path / "not a directory"
    occupied.write_text("existing file")
    with pytest.raises(ScoutError):
        prepare_directory(str(occupied))
    with pytest.raises(ScoutError):
        prepare_directory("  ")
