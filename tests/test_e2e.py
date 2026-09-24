"""Production bootstrap, real HTTP/SQLite/yt-dlp and Textual's headless pilot."""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest
from textual.widgets import Button, DataTable, Input

from video_scout.bootstrap import build_runtime
from video_scout.tui.app import VideoScoutApp


async def test_input_scan_select_download_real_file_with_textual(demo_site, tmp_path):
    site = demo_site(total=7, per_page=2)
    runtime = build_runtime(database=tmp_path / "e2e.sqlite")
    app = VideoScoutApp(runtime)
    directory = tmp_path / "实际 下载目录"
    async with app.run_test(size=(120, 45)) as pilot:
        # These are normal user input values, with production defaults otherwise.
        app.query_one("#start-url", Input).value = site.url
        app.query_one("#limit", Input).value = "3"
        app.query_one("#directory", Input).value = str(directory)
        await pilot.press("f5")
        async with asyncio.timeout(25):
            while app._scan_task is None or not app._scan_task.done():
                await pilot.pause(0.05)
        await pilot.pause()
        assert len(app.items) == app.query_one("#results", DataTable).row_count == 3
        assert runtime.scan.reason == "达到视频上限"
        assert site.requests["/catalog?p=3"] == 0
        # The row's stable ID drives selection through the actual button handler.
        table = app.query_one("#results", DataTable)
        table.move_cursor(row=1)
        toggle = app.query_one("#toggle", Button)
        toggle.scroll_visible(immediate=True)
        await pilot.pause()
        assert await pilot.click("#toggle")
        await pilot.pause()
        assert len(app.selected_ids) == 1
        chosen_id = next(iter(app.selected_ids))
        download = app.query_one("#download", Button)
        download.scroll_visible(immediate=True)
        await pilot.pause()
        assert await pilot.click("#download")
        async with asyncio.timeout(30):
            while not runtime.downloads.tasks or any(
                task.status in {"queued", "downloading", "merging"}
                for task in runtime.downloads.tasks.values()
            ):
                await pilot.pause(0.05)
        await pilot.pause()
        [task] = runtime.downloads.tasks.values()
        assert task.video.id == chosen_id
        assert task.status == "completed", task.error
        final = Path(task.file_path)
        assert final.parent == directory.resolve()
        assert final.is_file() and final.stat().st_size > 0
        assert app.query_one("#download-table", DataTable).row_count == 1
        probe = shutil.which("ffprobe")
        if probe is None:
            pytest.skip("Real file downloaded; ffprobe codec verification unavailable")
        process = await asyncio.create_subprocess_exec(
            probe, "-v", "error", "-show_streams", "-of", "json", str(final),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        output, error = await process.communicate()
        assert process.returncode == 0, error.decode()
        assert any(stream["codec_type"] == "video" for stream in json.loads(output)["streams"])
        await pilot.press("ctrl+q")
    assert not runtime.scan.active
