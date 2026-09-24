from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from video_scout.adapters.http import HtmlDiscovery, HttpPageFetcher
from video_scout.adapters.media import HybridVideoResolver
from video_scout.adapters.sqlite import SQLiteRepository
from video_scout.domain.models import PageTask, ScanConfig, ScoutError, VideoCandidate
from video_scout.services.scan import ScanService


async def scan_local(site, database, **config):
    fetcher = HttpPageFetcher()
    discovery = HtmlDiscovery()
    resolver = HybridVideoResolver()
    repository = SQLiteRepository(database)
    events = []
    scan = ScanService(fetcher, discovery, discovery, resolver, repository, events.append)
    try:
        cfg = ScanConfig(site.url, host_interval=0, retries=0, max_depth=2, **config)
        session_id = await asyncio.wait_for(scan.run(cfg), 30)
        saved = await repository.list_videos(session_id)
        assert {item.id for item in saved} == set(scan.items)
        return list(scan.items.values()), scan.reason, events, session_id
    finally:
        await scan.close()
        await resolver.close()
        await fetcher.close()
        await repository.close()


@pytest.mark.parametrize("limit,concurrency", [(1, 1), (3, 1), (100, 1), (100, 3)])
async def test_local_real_media_strict_limit_and_no_later_pagination(demo_site, tmp_path, limit, concurrency):
    site = demo_site(total=137)
    items, reason, events, _ = await scan_local(site, tmp_path / "scan.sqlite", limit=limit,
                                               page_concurrency=concurrency)
    assert len(items) == len({item.identity for item in items}) == limit
    assert reason == "达到视频上限"
    assert all(item.primary_url.startswith(site.base_url + "/media/") for item in items)
    assert site.requests[f"/catalog?p={(limit - 1) // site.per_page + 2}"] == 0
    assert sum(event.kind == "video" and not event.data.get("updated") for event in events) == limit
    assert events[-1].data["state"] == "finished"


async def test_37_results_normal_completion_deep_details_pagination_and_history(demo_site, tmp_path):
    site = demo_site(total=37, per_page=3)
    db_path = tmp_path / "history.sqlite"
    items, reason, _, session_id = await scan_local(site, db_path)
    assert len(items) == 37
    assert reason == "没有更多任务"
    # 13 sequential list pages pass the depth=2 budget; both detail levels are visited.
    assert site.requests["/catalog?p=13"] == 1
    assert site.requests["/detail/36"] == 1
    assert site.requests["/watch/36"] == 1
    # Neither repeated media links nor source tags increase the confirmed count.
    assert len({item.primary_url for item in items}) == 37
    assert all(len(item.variants) == 1 for item in items)
    assert site.requests["/catalog?p=1"] == 1
    assert site.requests["/forbidden"] == 0
    reopened = SQLiteRepository(db_path)
    try:
        assert len(await reopened.list_videos()) == 37
        history = await reopened.list_sessions()
        assert history[0]["id"] == session_id
        assert history[0]["reason"] == "没有更多任务"
    finally:
        await reopened.close()


async def test_page_protection_limit_preserves_partial_results(demo_site, tmp_path):
    site = demo_site(total=137, per_page=1)
    items, reason, _, _ = await scan_local(site, tmp_path / "limit.sqlite", max_pages=7)
    assert 0 < len(items) < 100
    assert "最大访问页面数" in reason
    assert sum(count for path, count in site.requests.items()
               if not path.startswith("/media/")) == 7


async def test_queue_truncation_is_reported_instead_of_claiming_exhaustion(demo_site, tmp_path):
    site = demo_site(total=137)
    items, reason, _, _ = await scan_local(site, tmp_path / "queue.sqlite", max_queue=2)
    assert len(items) < 100
    assert "容量" in reason or "队列" in reason
    assert reason != "没有更多任务"


async def test_direct_media_input_is_confirmed_without_html(demo_site, tmp_path):
    site = demo_site(total=1)
    site.url = site.base_url + "/media/0.mp4?token=signed-value&quality=hd"
    items, _, _, _ = await scan_local(site, tmp_path / "direct.sqlite", limit=1)
    assert len(items) == 1
    assert items[0].primary_url == site.url  # Signed query is not normalized away.
    assert not any(path.startswith("/catalog") for path in site.requests)


async def test_http_retry_access_limits_body_limit_and_redirect_scope(demo_site):
    site = demo_site(total=0)
    fetcher = HttpPageFetcher()
    config = ScanConfig(site.url, host_interval=0, retries=1)
    try:
        page = await fetcher.fetch(PageTask(site.base_url + "/flaky"), config)
        assert b"<html" in page.body
        assert site.requests["/flaky"] == 2
        with pytest.raises(ScoutError, match="429"):
            await fetcher.fetch(PageTask(site.base_url + "/limited"), config)
        assert site.requests["/limited"] == 2
        with pytest.raises(ScoutError, match="403"):
            await fetcher.fetch(PageTask(site.base_url + "/restricted"), config)
        assert site.requests["/restricted"] == 1
        with pytest.raises(ScoutError, match="范围"):
            await fetcher.fetch(PageTask(site.base_url + "/outside"), config)
        assert site.requests["/forbidden"] == 0
        with pytest.raises(ScoutError, match="上限"):
            await fetcher.fetch(PageTask(site.base_url + "/oversized"),
                                replace(config, max_body_bytes=1024))
    finally:
        await fetcher.close()


async def test_fake_mp4_suffix_and_blob_are_not_confirmed(demo_site):
    site = demo_site(total=1)
    resolver = HybridVideoResolver()
    config = ScanConfig(site.url, host_interval=0, retries=0)
    try:
        for url in [site.base_url + "/not-video.mp4", "blob:http://example.test/id"]:
            with pytest.raises(ScoutError):
                async for _ in resolver.resolve(VideoCandidate(url, site.url), config, 1):
                    pytest.fail("An unconfirmed URL must not produce a video")
    finally:
        await resolver.close()
