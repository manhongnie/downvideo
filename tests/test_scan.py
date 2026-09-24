from __future__ import annotations

import asyncio

import pytest

from video_scout.domain.models import (
    FetchedPage,
    MediaVariant,
    ScanConfig,
    ScoutError,
    VideoCandidate,
    VideoItem,
)
from video_scout.services.scan import ScanService


def video(number: int, *, title: str = "Same title") -> VideoItem:
    url = f"http://example.test/media/{number}.mp4?signature=keep-{number}"
    return VideoItem.create(title=title, source_url="http://example.test/catalog",
                            page_url="http://example.test/watch",
                            variants=[MediaVariant(url, ext="mp4")])


class MemoryRepository:
    """A test-only in-memory implementation of the Repository port."""

    def __init__(self):
        self.sessions = {}
        self.videos = {}
        self.failures = []

    async def start_session(self, session_id, config):
        self.sessions[session_id] = {"config": config}
        self.videos[session_id] = {}

    async def finish_session(self, session_id, reason):
        self.sessions[session_id]["reason"] = reason

    async def save_video(self, session_id, item):
        # Yield at persistence to exercise the atomic admission lock.
        await asyncio.sleep(0)
        self.videos[session_id][item.id] = item

    async def save_candidate_error(self, session_id, candidate, reason):
        self.failures.append((session_id, candidate, reason))


class ControlledFetcher:
    def __init__(self, *, blocked=False, candidates=True):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()
        self.cancelled = False
        self.candidates = candidates

    async def fetch(self, task, config):
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return FetchedPage(task.url, b"", media_urls=("http://example.test/list",) if self.candidates else ())

    async def close(self):
        pass


class Discovery:
    def extract(self, page, config):
        return [VideoCandidate(url, page.url) for url in page.media_urls]

    def discover(self, page, task, config):
        return []


class LazyResolver:
    def __init__(self, items=(), *, fail=False):
        self.items = list(items)
        self.fail = fail
        self.yielded = 0
        self.closed = False
        self.budgets = []

    async def resolve(self, candidate, config, budget):
        self.budgets.append(budget)
        try:
            if self.fail:
                raise ScoutError("unsupported page")
            for item in self.items:
                self.yielded += 1
                yield item
        finally:
            self.closed = True

    async def close(self):
        pass


def make_scan(items=(), *, blocked=False, events=None, fail=False):
    fetcher = ControlledFetcher(blocked=blocked)
    discovery = Discovery()
    resolver = LazyResolver(items, fail=fail)
    repository = MemoryRepository()
    scanner = ScanService(fetcher, discovery, discovery, resolver, repository,
                          events.append if events is not None else lambda event: None)
    return scanner, fetcher, resolver, repository


@pytest.mark.parametrize("limit", [0, -1, 1.5, True, "3"])
async def test_invalid_limit_does_not_create_session(limit):
    scan, _, _, repository = make_scan()
    with pytest.raises(ScoutError):
        await scan.run(ScanConfig("http://example.test/", limit=limit))
    assert not repository.sessions


@pytest.mark.parametrize("limit", [1, 3, 100])
async def test_lazy_playlist_stops_exactly_at_budget(limit):
    events = []
    scan, _, resolver, repository = make_scan([video(i) for i in range(137)], events=events)
    session_id = await scan.run(ScanConfig("http://example.test/", limit=limit))
    assert len(scan.items) == limit
    assert len(repository.videos[session_id]) == limit
    assert resolver.yielded == limit
    assert resolver.closed
    assert events[-1].data["state"] == "finished"
    assert "所有在途任务已退出" in events[-1].message
    assert any(e.data.get("state") == "stopping" for e in events)


async def test_atomic_admission_near_99_never_overshoots():
    scan, fetcher, _, repository = make_scan(blocked=True)
    running = asyncio.create_task(scan.run(ScanConfig("http://example.test/", limit=100)))
    await fetcher.entered.wait()
    session_id = scan.session_id
    for number in range(99):
        assert await scan.admit(session_id, video(number))
    admitted = await asyncio.gather(*(scan.admit(session_id, video(i)) for i in range(99, 110)))
    await running
    assert sum(admitted) == 1
    assert len(scan.items) == len(repository.videos[session_id]) == 100
    assert fetcher.cancelled


async def test_old_session_results_cannot_pollute_new_scan():
    scan, fetcher, _, _ = make_scan(blocked=True)
    running = asyncio.create_task(scan.run(ScanConfig("http://example.test/")))
    await fetcher.entered.wait()
    old_id = scan.session_id
    scan.stop()
    await running
    fetcher.entered.clear()
    second = asyncio.create_task(scan.run(ScanConfig("http://example.test/")))
    await fetcher.entered.wait()
    assert old_id != scan.session_id
    assert not await scan.admit(old_id, video(1))
    assert not scan.items
    scan.stop()
    await second


async def test_duplicate_titles_are_distinct_but_stable_ids_merge_formats():
    first = video(1)
    duplicate = video(1)
    duplicate.source_url = "http://example.test/another-source"
    identified = video(2)
    identified.extractor = "test-site"
    identified.extractor_id = "42"
    alternate = VideoItem.create(title="different title", source_url=identified.source_url,
                                 page_url=identified.page_url,
                                 variants=[MediaVariant("http://cdn.test/hd.mp4")],
                                 extractor="test-site", extractor_id="42")
    scan, _, _, _ = make_scan([first, duplicate, identified, alternate, video(3)])
    await scan.run(ScanConfig("http://example.test/"))
    assert len(scan.items) == 3
    assert len(scan.items[identified.id].variants) == 2
    assert duplicate.source_url in scan.items[first.id].sources


async def test_playlist_duplicates_do_not_spend_unique_video_budget():
    class TwoCandidates(Discovery):
        def extract(self, page, config):
            return [VideoCandidate("http://example.test/first", page.url),
                    VideoCandidate("http://example.test/playlist", page.url)]

    class BudgetedResolver(LazyResolver):
        async def resolve(self, candidate, config, budget):
            entries = [video(0)] if candidate.url.endswith("first") else [video(0), video(1), video(2)]
            # Remaining unique budget is a hint: duplicate outputs must not consume it.
            for entry in entries[:config.max_queue]:
                yield entry

    scan, _, _, _ = make_scan()
    scan.extractor = TwoCandidates()
    scan.resolver = BudgetedResolver()
    await scan.run(ScanConfig("http://example.test/", limit=3))
    assert len(scan.items) == 3
    assert scan.reason == "达到视频上限"


async def test_failed_candidates_are_recorded_without_counting():
    scan, _, _, repository = make_scan(fail=True)
    await scan.run(ScanConfig("http://example.test/"))
    assert not scan.items
    assert len(repository.failures) == 1
    assert scan.reason == "没有更多任务"


async def test_pause_prevents_next_candidate_and_resume_continues():
    reached = asyncio.Event()
    scan, _, resolver, _ = make_scan([video(i) for i in range(10)])

    def on_event(event):
        if event.kind == "video" and len(scan.items) == 1:
            scan.pause()
            reached.set()

    scan.on_event = on_event
    running = asyncio.create_task(scan.run(ScanConfig("http://example.test/", limit=3)))
    await asyncio.wait_for(reached.wait(), 2)
    await asyncio.sleep(0.03)
    assert scan.paused
    assert resolver.yielded == 1
    scan.resume()
    await asyncio.wait_for(running, 2)
    assert len(scan.items) == 3


async def test_stop_retains_results_and_stops_lazy_resolution():
    scan, _, resolver, _ = make_scan([video(i) for i in range(10)])
    scan.on_event = lambda event: scan.stop() if event.kind == "video" else None
    await scan.run(ScanConfig("http://example.test/"))
    assert len(scan.items) == resolver.yielded == 1
    assert scan.reason == "用户停止"
    assert resolver.closed


async def test_close_cancels_inflight_and_waits_for_exit():
    scan, fetcher, _, _ = make_scan(blocked=True)
    running = asyncio.create_task(scan.run(ScanConfig("http://example.test/")))
    await fetcher.entered.wait()
    await asyncio.wait_for(scan.close(), 2)
    assert running.done()
    assert fetcher.cancelled
    assert not scan.active


async def test_deadline_interrupts_stalled_request():
    scan, fetcher, _, _ = make_scan(blocked=True)
    await scan.run(ScanConfig("http://example.test/", deadline_seconds=0.05))
    assert fetcher.cancelled
    assert "截止时间" in scan.reason


async def test_persistence_failure_cannot_report_successful_limit():
    class FailingRepository(MemoryRepository):
        async def save_video(self, session_id, item):
            raise OSError("simulated full disk")

    scan, _, _, _ = make_scan([video(1)])
    scan.repository = FailingRepository()
    await scan.run(ScanConfig("http://example.test/", limit=1))
    assert "失败" in scan.reason
    assert "达到视频上限" not in scan.reason


async def test_old_finish_event_keeps_its_session_id_during_overlap():
    class FinishingRepository(MemoryRepository):
        def __init__(self):
            super().__init__()
            self.finishing = asyncio.Event()
            self.release_finish = asyncio.Event()

        async def finish_session(self, session_id, reason):
            self.finishing.set()
            await self.release_finish.wait()
            await super().finish_session(session_id, reason)

    events = []
    scan, _, _, _ = make_scan(events=events)
    repository = FinishingRepository()
    scan.repository = repository
    first = asyncio.create_task(scan.run(ScanConfig("http://example.test/")))
    await repository.finishing.wait()
    old_id = scan.session_id
    second = asyncio.create_task(scan.run(ScanConfig("http://example.test/")))
    await asyncio.sleep(0)
    repository.release_finish.set()
    outcomes = await asyncio.gather(first, second, return_exceptions=True)
    assert outcomes[0] == old_id
    assert any(event.data.get("state") == "finished" and event.session_id == old_id
               for event in events)
    # Either serialization or explicit old-session event tagging is acceptable.
    assert isinstance(outcomes[1], (ScoutError, str))
