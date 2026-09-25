"""Optional bounded browser enhancement. Disabled mode imports no browser SDK."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from urllib.parse import urlsplit

from video_scout.domain.models import FetchedPage, PageTask, ScanConfig, ScoutError
from video_scout.domain.ports import PageFetcher
from video_scout.domain.urls import in_scope, validate_url


class OptionalBrowserFetcher:
    def __init__(self, base: PageFetcher,
                 warning: Callable[[str], None] | None = None,
                 proxy: str | None = None) -> None:
        self.base = base
        self.warning = warning or (lambda _message: None)
        self.proxy = proxy
        self._playwright = None
        self._browser = None
        self._browser_visible: bool | None = None
        self._unavailable_modes: set[bool] = set()
        self._lock = asyncio.Lock()

    async def _ensure_browser(self, visible: bool) -> bool:
        if visible in self._unavailable_modes:
            return False
        async with self._lock:
            if self._browser is not None and self._browser_visible == visible:
                return True
            try:
                if self._browser is not None:
                    await self._browser.close()
                    self._browser = None
                from playwright.async_api import async_playwright
                if self._playwright is None:
                    self._playwright = await async_playwright().start()
                launch = {"headless": not visible}
                if self.proxy:
                    launch["proxy"] = {"server": self.proxy}
                self._browser = await self._playwright.chromium.launch(**launch)
                self._browser_visible = visible
                return True
            except Exception as exc:
                # Import/launch errors include a missing installed Chromium binary.
                self._unavailable_modes.add(visible)
                if self._playwright is not None:
                    await self._playwright.stop()
                    self._playwright = None
                self.warning(f"浏览器增强不可用（{type(exc).__name__}）；继续基础 HTTP 模式。"
                             "安装 video-scout[browser] 和 Chromium；可见模式还需要桌面显示。")
                return False

    async def fetch(self, task: PageTask, config: ScanConfig) -> FetchedPage:
        base: FetchedPage | None = None
        challenge_error: ScoutError | None = None
        try:
            base = await self.base.fetch(task, config)
        except ScoutError as exc:
            if not config.browser or "Cloudflare 人机验证" not in str(exc):
                raise
            challenge_error = exc
        if base is not None and (not config.browser or "html" not in base.content_type):
            return base
        if base is not None and b"<title>just a moment" in base.body.lower():
            challenge_error = ScoutError("Cloudflare 人机验证阻止了自动扫描")
            base = None
        if not await self._ensure_browser(config.browser_visible):
            if base is not None:
                return base
            raise ScoutError("Cloudflare 人机验证阻止了扫描，且浏览器增强不可用；"
                             "请安装 Playwright 与 Chromium，并确认可见模式有桌面显示") from challenge_error

        context = None
        challenge_seen = False
        media: list[str] = []
        blocked = [False]
        try:
            context = await self._browser.new_context(accept_downloads=False, service_workers="block")
            page = await context.new_page()
            main_status = [None]

            def main_frame_request(request) -> bool:
                try:
                    return request.frame == page.main_frame
                except Exception:
                    return False

            def preview_url(url: str) -> bool:
                return urlsplit(url).path.lower().endswith("/preview.mp4")

            def media_url(url: str) -> bool:
                return urlsplit(url).path.lower().endswith(
                    (".mp4", ".webm", ".mov", ".mkv", ".m4v", ".m3u8", ".mpd", ".ogg"))

            async def route_request(route) -> None:
                request = route.request
                try:
                    validate_url(request.url)
                    if request.is_navigation_request() and not in_scope(request.url, config):
                        # External ad frames must not invalidate the main page.
                        if main_frame_request(request):
                            blocked[0] = True
                        await route.abort()
                        return
                    # Capture the URL while avoiding the media body. Preview clips are not videos.
                    if request.resource_type == "media" or media_url(request.url):
                        if main_frame_request(request) and not preview_url(request.url) and len(media) < config.max_queue:
                            media.append(request.url)
                        await route.abort()
                        return
                    await route.continue_()
                except ScoutError:
                    await route.abort()

            def response_received(response) -> None:
                request = response.request
                if request.is_navigation_request() and main_frame_request(request):
                    main_status[0] = response.status
                mime = response.headers.get("content-type", "").lower()
                if (main_frame_request(request) and not preview_url(response.url)
                        and (mime.startswith("video/") or "mpegurl" in mime or "dash+xml" in mime)
                        and len(media) < config.max_queue):
                    media.append(response.url)

            await context.route("**/*", route_request)
            page.on("response", response_received)
            async with asyncio.timeout(config.browser_seconds):
                await page.goto(base.url if base is not None else task.url, wait_until="domcontentloaded",
                                timeout=int(config.request_timeout * 1000))
                while "just a moment" in (await page.title()).lower():
                    challenge_seen = True
                    await page.wait_for_timeout(300)
                for _ in range(config.browser_steps):
                    await page.evaluate("window.scrollBy(0, window.innerHeight)")
                    await page.wait_for_timeout(150)
                    if len((await page.content()).encode()) > config.max_body_bytes:
                        raise ScoutError("浏览器页面超过响应体保护限制")
                if blocked[0] or not in_scope(page.url, config):
                    raise ScoutError("浏览器网页导航超出允许范围")
                if main_status[0] is not None and main_status[0] >= 400:
                    if challenge_error is not None and main_status[0] == 403:
                        raise ScoutError("Cloudflare 人机验证仍阻止浏览器扫描；"
                                         "请启用可见浏览器并在窗口中完成验证")
                    raise ScoutError(f"浏览器 HTTP {main_status[0]}（访问受限）")
                content = (await page.content()).encode()
                if len(content) > config.max_body_bytes:
                    raise ScoutError("浏览器页面超过响应体保护限制")
                return FetchedPage(page.url, content, "text/html", tuple(dict.fromkeys(media)))
        except asyncio.CancelledError:
            raise
        except ScoutError:
            raise
        except Exception as exc:
            if blocked[0]:
                raise ScoutError("浏览器网页导航超出允许范围") from exc
            if challenge_error is not None:
                if challenge_seen:
                    raise ScoutError("Cloudflare 人机验证未在浏览器时间预算内完成；"
                                     "请启用可见浏览器并适当增加浏览器操作时间预算") from exc
                raise ScoutError(f"Cloudflare 页面浏览器渲染失败：{type(exc).__name__}；"
                                 "请启用可见浏览器并确认桌面显示可用") from exc
            self.warning(f"浏览器渲染未完成（{type(exc).__name__}）；使用已获取的基础页面。")
            assert base is not None
            return base
        finally:
            if context is not None:
                await context.close()

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        await self.base.close()
