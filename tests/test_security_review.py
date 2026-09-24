"""Focused regressions from a second pass over queue, scope, and cancellation."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from video_scout.adapters.http import HtmlDiscovery, HttpPageFetcher
from video_scout.adapters.sqlite import SQLiteRepository
from video_scout.domain.models import (
    FetchedPage,
    MediaVariant,
    PageTask,
    ScanConfig,
    ScoutError,
    VideoItem,
)
from video_scout.services.download import DownloadService


def test_queue_budget_keeps_later_priority_links():
    url = "http://example.test/catalog"
    body = b'''<a href="/noise/1">one</a><a href="/noise/2">two</a>
        <a rel="next" href="/catalog?p=2">Next</a>
        <a class="video-detail" href="/detail/7">Watch</a>'''
    config = ScanConfig(url, max_queue=2)
    links = HtmlDiscovery().discover(FetchedPage(url, body), PageTask(url), config)
    # The extra link is an overflow sentinel; the service owns the hard queue cap.
    assert len(links) <= config.max_queue + 1
    assert [link.kind for link in links[:2]] == ["detail", "pagination"]


async def test_path_scope_cannot_be_bypassed_before_httpx_normalization():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, text="private")

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    fetcher = HttpPageFetcher(client)
    config = ScanConfig("http://example.test/allowed/", allowed_paths=("/allowed",), host_interval=0)
    try:
        with pytest.raises(ScoutError):
            await fetcher.fetch(PageTask("http://example.test/allowed/../private"), config)
        assert not requests
    finally:
        await fetcher.close()


async def test_repeated_cancel_preserves_adapter_cleanup(tmp_path):
    class CleanupDownloader:
        def __init__(self):
            self.entered = asyncio.Event()
            self.cleaning = asyncio.Event()
            self.release = asyncio.Event()
            self.cleaned = False

        async def download(self, task, progress):
            self.entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cleaning.set()
                await self.release.wait()
                self.cleaned = True
                raise

        async def close(self):
            pass

    downloader = CleanupDownloader()
    repo = SQLiteRepository(":memory:")
    service = DownloadService(downloader, repo)
    item = VideoItem.create(title="test", source_url="http://example.test/", page_url="",
                            variants=[MediaVariant("http://example.test/a.mp4")])
    try:
        [task] = await service.enqueue([item], str(tmp_path))
        await asyncio.wait_for(downloader.entered.wait(), 2)
        service.cancel(task.id)
        await asyncio.wait_for(downloader.cleaning.wait(), 2)
        service.cancel(task.id)
        downloader.release.set()
        await asyncio.wait_for(service.wait(), 2)
        assert downloader.cleaned
        assert task.status == "cancelled"
        assert (await repo.list_downloads())[0].status == "cancelled"
    finally:
        downloader.release.set()
        await service.close()
        await repo.close()
