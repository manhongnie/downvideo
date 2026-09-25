"""Headless UI checks use controlled services, not claims about live extraction."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.widgets import Button, DataTable, Input, Select, Static, TabbedContent, TextArea

from video_scout.domain.models import DownloadTask, Event, MediaVariant, VideoItem
from video_scout.tui.app import DetailScreen, VideoScoutApp


def sample(number: int, title: str | None = None) -> VideoItem:
    return VideoItem.create(
        title=title or f"视频 {number}", source_url="http://test.local/list",
        page_url=f"http://test.local/detail/{number}",
        variants=[MediaVariant(f"http://test.local/{number}.mp4?token=keep", ext="mp4")],
    )


class UIRepository:
    def __init__(self, videos: list[VideoItem]) -> None:
        self.videos = videos

    async def list_videos(self, session_id=None):
        return self.videos

    async def list_sessions(self):
        return [{"id": "historical", "started_at": "today", "reason": "exhausted"}]

    async def list_downloads(self):
        return []


class UIScan:
    def __init__(self) -> None:
        self.on_event = lambda event: None
        self.active = False
        self.paused = False
        self.reason = "用户停止"
        self.stop_event = asyncio.Event()

    async def run(self, config):
        self.active = True
        self.stop_event.clear()
        self.on_event(Event("session", "current"))
        self.on_event(Event("video", "current", video=sample(8)))
        await self.stop_event.wait()
        self.active = False
        return "current"

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def stop(self):
        self.stop_event.set()


class UIDownloads:
    def __init__(self) -> None:
        self.tasks = {}
        self.on_event = lambda event: None
        self.enqueued = []
        self.cancelled = False
        self.retried = None

    async def enqueue(self, items, directory, concurrency=2):
        self.enqueued.append((items, directory, concurrency))
        tasks = [DownloadTask.create(item, Path(directory), f"{item.id}.mp4") for item in items]
        self.tasks.update({task.id: task for task in tasks})
        return tasks

    async def retry(self, task_id):
        self.retried = task_id

    async def cancel(self, task_id=None):
        self.cancelled = True


def runtime(videos=()):
    result = SimpleNamespace(repository=UIRepository(list(videos)), scan=UIScan(), downloads=UIDownloads(), closed=False)

    async def configure(config):
        result.config = config

    async def close():
        result.closed = True
        result.scan.stop()

    result.configure = configure
    result.close = close
    return result


@pytest.mark.asyncio
async def test_input_shortcuts_and_stable_selection_filter_sort_new_rows():
    first, second = sample(1, "Zed [red]"), sample(2, "Alpha")
    rt = runtime([first, second])
    app = VideoScoutApp(rt)
    async with app.run_test(size=(110, 42)) as pilot:
        await pilot.pause()
        url = app.query_one("#start-url", Input)
        url.focus()
        await pilot.press("a", "p", "s", "d")
        assert url.value == "apsd"
        assert not rt.scan.active
        table = app.query_one("#results", DataTable)
        table.focus()
        table.move_cursor(row=0)
        await pilot.press("enter")
        assert app.selected_ids == {first.id}
        app.query_one("#sort", Select).value = "title"
        await pilot.pause()
        assert app.visible_ids == [second.id, first.id]
        assert app.selected_ids == {first.id}
        app.query_one("#filter", Input).value = "Alpha"
        await pilot.pause()
        assert app.visible_ids == [second.id]
        assert app.selected_ids == {first.id}
        app._receive_event(Event("video", video=sample(3, "Alpha two")))
        await pilot.pause()
        assert len(app.visible_ids) == 2
        assert app.selected_ids == {first.id}
        app.query_one("#select-visible", Button).press()
        await pilot.pause()
        assert len(app.selected_ids) == 3
        app.query_one("#clear-selection", Button).press()
        await pilot.pause()
        assert not app.selected_ids
    assert rt.closed


@pytest.mark.asyncio
async def test_scan_validation_pause_resume_stop_stale_events_and_exit():
    rt = runtime()
    app = VideoScoutApp(rt)
    async with app.run_test(size=(110, 42)) as pilot:
        app.query_one("#start-url", Input).value = "http://test.local/"
        app.query_one("#limit", Input).value = "0"
        await pilot.press("f5")
        await pilot.pause()
        assert not rt.scan.active
        assert "正整数" in str(app.query_one("#status", Static).render())
        app.query_one("#limit", Input).value = "3"
        await pilot.press("f5")
        await pilot.pause()
        assert rt.scan.active
        assert len(app.items) == 1
        assert app.query_one("#download", Button).disabled
        await pilot.press("f6")
        assert rt.scan.paused
        await pilot.press("f6")
        assert not rt.scan.paused
        app._receive_event(Event("video", "old-session", video=sample(99)))
        await pilot.pause()
        assert len(app.items) == 1
        await pilot.press("f7")
        await pilot.pause()
        assert not rt.scan.active
        assert len(app.items) == 1
        assert not app.query_one("#download", Button).disabled
        await pilot.press("ctrl+q")
    assert rt.closed


@pytest.mark.asyncio
async def test_narrow_details_download_target_and_retry(tmp_path):
    item = sample(1, "\x1b[31m标题 [bold]")
    rt = runtime([item])
    app = VideoScoutApp(rt)
    async with app.run_test(size=(48, 24)) as pilot:
        await pilot.pause()
        button = app.query_one("#show-details", Button)
        button.scroll_visible(animate=False, immediate=True)
        await pilot.pause()
        assert button.region.width > 0
        assert await pilot.click("#show-details")
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)
        detail = app.screen.query_one("#detail-text", TextArea).text
        assert "清晰度：未知" in detail
        assert "token=keep" in detail
        assert "\x1b" not in detail
        assert "[bold]" in detail
        await pilot.press("escape")
        app.toggle_selection(item.id)
        directory = str(tmp_path / "中文 目录")
        app.query_one("#directory", Input).value = directory
        app.query_one("#download", Button).press()
        await pilot.pause()
        assert rt.downloads.enqueued[0][1] == directory
        app.query_one("#directory", Input).value = "changed"
        assert next(iter(rt.downloads.tasks.values())).target_dir == directory
        assert app.query_one("#tabs", TabbedContent).active == "downloads-tab"
        task = next(iter(rt.downloads.tasks.values()))
        app._receive_event(Event("download", download=replace(task, status="failed", error="failure")))
        await pilot.pause()
        app.query_one("#retry-download", Button).press()
        await pilot.pause()
        assert rt.downloads.retried == task.id
        app.query_one("#cancel-all", Button).press()
        await pilot.pause()
        assert rt.downloads.cancelled


@pytest.mark.asyncio
async def test_export_scopes_history_and_initial_url(tmp_path):
    first, second = sample(1), sample(2)
    rt = runtime([first, second])
    app = VideoScoutApp(rt)
    app.initial_url = "http://test.local/from-cli"
    async with app.run_test(size=(100, 40)) as pilot:
        assert app.query_one("#start-url", Input).value == app.initial_url
        app.toggle_selection(second.id)
        path = tmp_path / "勾选 地址.json"
        app.query_one("#export-format", Select).value = "json"
        app.query_one("#export-scope", Select).value = "selected"
        app.query_one("#export-path", Input).value = str(path)
        app.query_one("#export", Button).press()
        await pilot.pause()
        assert [item["id"] for item in json.loads(path.read_text())] == [second.id]
        assert "headers" not in path.read_text()
        path = tmp_path / "all.txt"
        app.query_one("#export-format", Select).value = "txt"
        app.query_one("#export-scope", Select).value = "all"
        app.query_one("#export-path", Input).value = str(path)
        app.query_one("#export", Button).press()
        await pilot.pause()
        assert path.read_text().splitlines() == [first.primary_url, second.primary_url]
        app.query_one("#history", Select).value = "historical"
        app.query_one("#load-history", Button).press()
        await pilot.pause()
        assert app.session_id == "historical"
        assert not app.selected_ids
        assert len(app.items) == 2
