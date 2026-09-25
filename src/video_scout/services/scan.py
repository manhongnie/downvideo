"""Bounded scanning use case. All admissions pass through one session-aware lock."""
from __future__ import annotations

import asyncio
import heapq
from collections import deque
from contextlib import aclosing
from dataclasses import dataclass, field
from uuid import uuid4

from video_scout.domain.models import (
    Event,
    EventSink,
    PageTask,
    ScanConfig,
    ScoutError,
    VideoCandidate,
    VideoItem,
)
from video_scout.domain.ports import (
    PageFetcher,
    PageLinkDiscoverer,
    Repository,
    VideoExtractor,
    VideoResolver,
)
from video_scout.domain.urls import in_scope, normalize_url, redact, safe_text, validate_url


class _PersistenceFailure(ScoutError):
    """A storage failure ends the session; it is never a candidate parse failure."""


@dataclass
class _Session:
    id: str
    config: ScanConfig
    items: dict[str, VideoItem] = field(default_factory=dict)
    identities: dict[str, str] = field(default_factory=dict)
    pages: list[tuple[int, int, PageTask]] = field(default_factory=list)
    visited: set[str] = field(default_factory=set)
    queued: set[str] = field(default_factory=set)
    candidates: deque[VideoCandidate] = field(default_factory=deque)
    candidate_keys: set[str] = field(default_factory=set)
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    resume: asyncio.Event = field(default_factory=asyncio.Event)
    pending: set[asyncio.Task] = field(default_factory=set)
    background: set[asyncio.Task] = field(default_factory=set)
    sequence: int = 0
    reason: str = ""
    truncated: bool = False


class ScanService:
    def __init__(self, fetcher: PageFetcher, discoverer: PageLinkDiscoverer,
                 extractor: VideoExtractor, resolver: VideoResolver,
                 repository: Repository, on_event: EventSink = lambda event: None):
        self.fetcher = fetcher
        self.discoverer = discoverer
        self.extractor = extractor
        self.resolver = resolver
        self.repository = repository
        self.on_event = on_event
        self._session: _Session | None = None
        self._lock = asyncio.Lock()
        self._runner: asyncio.Task | None = None
        self.active = False

    @property
    def session_id(self) -> str:
        return self._session.id if self._session else ""

    @property
    def items(self) -> dict[str, VideoItem]:
        return self._session.items if self._session else {}

    @property
    def reason(self) -> str:
        return self._session.reason if self._session else ""

    @property
    def paused(self) -> bool:
        return bool(self.active and self._session and not self._session.resume.is_set())

    def _emit(self, kind: str, message: str = "", **kwargs) -> None:
        self.on_event(Event(kind, self.session_id, redact(message), **kwargs))

    def pause(self) -> None:
        if self.active and self._session and not self._session.stop.is_set():
            self._session.resume.clear()
            self._emit("state", "已暂停派发；已发出的请求可能继续收尾", data={"state": "paused"})

    def resume(self) -> None:
        if self.active and self._session:
            self._session.resume.set()
            self._emit("state", "继续扫描", data={"state": "running"})

    def stop(self, reason: str = "用户停止") -> None:
        ctx = self._session
        if not self.active or ctx is None or ctx.stop.is_set():
            return
        ctx.reason = reason
        ctx.stop.set()
        ctx.resume.set()
        self._emit("state", f"{reason}；已停止派发，正在等待在途任务退出", data={"state": "stopping"})
        current = asyncio.current_task()
        for task in tuple(ctx.pending):
            if task is not current and not task.cancelling():
                task.cancel()

    async def _checkpoint(self, ctx: _Session) -> bool:
        await ctx.resume.wait()
        return self._session is ctx and not ctx.stop.is_set()

    async def _work(self, ctx: _Session, awaitable, *, cancellable: bool = True):
        task = asyncio.create_task(awaitable)
        group = ctx.pending if cancellable else ctx.background
        group.add(task)
        try:
            return await task if cancellable else await asyncio.shield(task)
        finally:
            if task.done():
                group.discard(task)

    def _enqueue(self, ctx: _Session, page: PageTask) -> None:
        if ctx.stop.is_set() or page.depth > ctx.config.max_depth or not in_scope(page.url, ctx.config):
            return
        key = normalize_url(page.url)
        if key in ctx.queued or key in ctx.visited:
            return
        if len(ctx.pages) >= ctx.config.max_queue:
            ctx.truncated = True
            return
        ctx.sequence += 1
        priority = {"detail": 0, "pagination": 1, "page": 2}.get(page.kind, 2)
        heapq.heappush(ctx.pages, (priority, ctx.sequence, page))
        ctx.queued.add(key)

    async def admit(self, session_id: str, item: VideoItem) -> bool:
        """Atomic identity / remaining budget / persistence / publication boundary."""
        async with self._lock:
            ctx = self._session
            if ctx is None or ctx.id != session_id or ctx.stop.is_set() or not self.active:
                return False
            if not item.variants:
                return False
            try:
                for variant in item.variants:
                    validate_url(variant.url)
            except ScoutError:
                return False
            item.title = safe_text(item.title or "未知标题", 500)
            previous_id = ctx.identities.get(item.identity)
            # Explicit grouping can connect a later single source to an existing video.
            if previous_id is None and item.extractor == "direct":
                previous_id = next((ctx.identities.get(f"media:{v.url}") for v in item.variants
                                    if f"media:{v.url}" in ctx.identities), None)
            if previous_id is not None:
                from copy import deepcopy
                previous = deepcopy(ctx.items[previous_id])
                known = {(v.url, v.format_id) for v in previous.variants}
                previous.variants.extend(v for v in item.variants if (v.url, v.format_id) not in known)
                previous.sources = list(dict.fromkeys([*previous.sources, item.source_url, *item.sources]))
                try:
                    await self.repository.save_video(ctx.id, previous)
                except Exception as exc:
                    raise _PersistenceFailure(f"视频持久化失败：{type(exc).__name__}") from exc
                ctx.items[previous_id] = previous
                if previous.extractor == "direct":
                    for variant in previous.variants:
                        ctx.identities[f"media:{variant.url}"] = previous.id
                self._emit("video", video=previous, data={"count": len(ctx.items), "updated": True})
                return False
            if len(ctx.items) >= ctx.config.limit:
                self.stop("达到视频上限")
                return False
            ctx.items[item.id] = item
            ctx.identities[item.identity] = item.id
            if item.extractor == "direct":
                for variant in item.variants:
                    ctx.identities[f"media:{variant.url}"] = item.id
            # Reserve and close dispatch immediately, before yielding to SQLite I/O.
            if len(ctx.items) == ctx.config.limit:
                self.stop("达到视频上限")
            try:
                await self.repository.save_video(ctx.id, item)
            except Exception as exc:
                ctx.items.pop(item.id, None)
                ctx.identities = {key: value for key, value in ctx.identities.items() if value != item.id}
                ctx.reason = f"视频持久化失败：{type(exc).__name__}"
                self.stop(ctx.reason)
                raise _PersistenceFailure(ctx.reason) from exc
            self._emit("video", video=item, data={"count": len(ctx.items), "limit": ctx.config.limit})
            return True

    async def _process_page(self, ctx: _Session, task: PageTask, page) -> None:
        if ctx.stop.is_set():
            return
        ctx.visited.add(normalize_url(page.url))
        candidates = await self._work(ctx, asyncio.to_thread(self.extractor.extract, page, ctx.config), cancellable=False)
        for candidate in candidates:
            if len(ctx.candidates) >= ctx.config.max_queue:
                ctx.truncated = True
                break
            key = candidate.url + "\n" + "\n".join(candidate.group_urls)
            if key not in ctx.candidate_keys:
                ctx.candidate_keys.add(key)
                ctx.candidates.append(candidate)
        while ctx.candidates and await self._checkpoint(ctx):
            candidate = ctx.candidates.popleft()
            produced = False
            try:
                iterator = self.resolver.resolve(candidate, ctx.config, ctx.config.limit - len(ctx.items))
                async with aclosing(iterator):
                    while await self._checkpoint(ctx):
                        try:
                            item = await self._work(ctx, anext(iterator))
                        except StopAsyncIteration:
                            break
                        produced = True
                        await self.admit(ctx.id, item)
                if not produced and not ctx.stop.is_set():
                    raise ScoutError("未解析出有效媒体；可能需要站点专用适配")
            except asyncio.CancelledError:
                if not ctx.stop.is_set():
                    raise
                return
            except _PersistenceFailure:
                raise
            except Exception as exc:
                message = redact(f"候选解析失败：{type(exc).__name__}: {exc}")
                await self.repository.save_candidate_error(ctx.id, candidate, message)
                self._emit("candidate_error", message)
        if not ctx.stop.is_set():
            links = await self._work(ctx, asyncio.to_thread(self.discoverer.discover, page, task, ctx.config), cancellable=False)
            for next_page in links:
                self._enqueue(ctx, next_page)

    async def run(self, config: ScanConfig) -> str:
        config.validate()
        if self.active:
            raise ScoutError("请先停止当前扫描并等待在途任务退出")
        ctx = _Session(uuid4().hex, config)
        ctx.resume.set()
        self._session = ctx
        self.active = True
        self._runner = asyncio.current_task()
        try:
            await self.repository.start_session(ctx.id, config)
            self._emit("session", "开始扫描", data={"limit": config.limit})
            self._enqueue(ctx, PageTask(config.start_url))
            async with asyncio.timeout(config.deadline_seconds):
                while ctx.pages and await self._checkpoint(ctx):
                    if len(ctx.visited) >= config.max_pages:
                        self.stop("达到最大访问页面数")
                        break
                    batch: list[tuple[PageTask, asyncio.Task]] = []
                    priority = ctx.pages[0][0]
                    while ctx.pages and len(batch) < config.page_concurrency and len(ctx.visited) < config.max_pages:
                        # Do not prefetch the next list page while its higher-priority
                        # detail pages can still fill the remaining video quota.
                        if ctx.pages[0][0] != priority:
                            break
                        _, _, task = heapq.heappop(ctx.pages)
                        key = normalize_url(task.url)
                        ctx.queued.discard(key)
                        if key in ctx.visited or not in_scope(task.url, config):
                            continue
                        ctx.visited.add(key)
                        future = asyncio.create_task(self.fetcher.fetch(task, config))
                        ctx.pending.add(future)
                        batch.append((task, future))
                    # Process each page's candidates before dispatching another wave.
                    for task, future in batch:
                        try:
                            page = await future
                            if await self._checkpoint(ctx):
                                await self._process_page(ctx, task, page)
                        except asyncio.CancelledError:
                            if not ctx.stop.is_set():
                                raise
                        except _PersistenceFailure:
                            raise
                        except Exception as exc:
                            detail = str(exc) if isinstance(exc, ScoutError) else f"{type(exc).__name__}: {exc}"
                            self._emit("log", f"页面失败：{detail}")
                            if task.url == config.start_url and not ctx.items:
                                ctx.reason = redact(f"起始网页访问失败：{detail}")
                        finally:
                            ctx.pending.discard(future)
                        if ctx.stop.is_set():
                            break
                if not ctx.reason:
                    ctx.reason = "队列容量限制，已保留现有结果" if ctx.truncated else "没有更多任务"
        except TimeoutError:
            self.stop("达到扫描截止时间")
        except asyncio.CancelledError:
            self.stop("退出或任务取消")
        except _PersistenceFailure as exc:
            ctx.reason = str(exc)
            self.stop(ctx.reason)
        except Exception as exc:
            self.stop(f"扫描失败：{type(exc).__name__}: {redact(exc)}")
        finally:
            ctx.stop.set()
            ctx.resume.set()
            for task in ctx.pending:
                if not task.cancelling():
                    task.cancel()
            if ctx.pending:
                await asyncio.gather(*ctx.pending, return_exceptions=True)
            ctx.pending.clear()
            if ctx.background:
                await asyncio.gather(*ctx.background, return_exceptions=True)
                ctx.background.clear()
            # Wait for any concurrent accepted admission to finish persistence. Keep
            # the session active until its final database write and event are complete.
            async with self._lock:
                try:
                    await self.repository.finish_session(ctx.id, ctx.reason or "结束")
                finally:
                    self.on_event(Event("state", ctx.id,
                        f"{redact(ctx.reason)}；所有在途任务已退出，共 {len(ctx.items)} 个视频",
                        data={"state": "finished", "count": len(ctx.items), "reason": ctx.reason}))
                    self.active = False
                    self._runner = None
        return ctx.id

    async def close(self) -> None:
        self.stop("退出")
        runner = self._runner
        if runner and runner is not asyncio.current_task():
            await runner
