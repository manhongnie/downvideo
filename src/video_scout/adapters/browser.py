"""Optional bounded browser enhancement. Disabled mode imports no browser SDK."""
from __future__ import annotations

import asyncio
from collections.abc import Callable

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
        self._unavailable = False
        self._lock = asyncio.Lock()

    async def _ensure_browser(self) -> bool:
        if self._unavailable:
            return False
        async with self._lock:
            if self._browser is not None:
                return True
            try:
                from playwright.async_api import async_playwright
                self._playwright = await async_playwright().start()
                launch = {"headless": True}
                if self.proxy:
                    launch["proxy"] = {"server": self.proxy}
                self._browser = await self._playwright.chromium.launch(**launch)
                return True
            except Exception as exc:
                # Import/launch errors include a missing installed Chromium binary.
                self._unavailable = True
                if self._playwright is not None:
                    await self._playwright.stop()
                    self._playwright = None
                self.warning(f"浏览器增强不可用（{type(exc).__name__}）；继续基础 HTTP 模式。"
                             "安装 video-scout[browser] 并运行 playwright install chromium。")
                return False

    async def fetch(self, task: PageTask, config: ScanConfig) -> FetchedPage:
        base = await self.base.fetch(task, config)
        if not config.browser or "html" not in base.content_type or not await self._ensure_browser():
            return base
        context = await self._browser.new_context(accept_downloads=False, service_workers="block")
        page = await context.new_page()
        media: list[str] = []
        blocked = [False]

        async def route_request(route) -> None:
            request = route.request
            try:
                validate_url(request.url)
                if request.is_navigation_request() and not in_scope(request.url, config):
                    blocked[0] = True
                    await route.abort()
                    return
                # Avoid video streaming; discover its URL before aborting the body.
                if request.resource_type == "media":
                    if len(media) < config.max_queue:
                        media.append(request.url)
                    await route.abort()
                    return
                await route.continue_()
            except ScoutError:
                await route.abort()

        def response_received(response) -> None:
            mime = response.headers.get("content-type", "").lower()
            if (mime.startswith("video/") or "mpegurl" in mime or "dash+xml" in mime) and len(media) < config.max_queue:
                media.append(response.url)

        await context.route("**/*", route_request)
        page.on("response", response_received)
        try:
            async with asyncio.timeout(config.browser_seconds):
                await page.goto(base.url, wait_until="domcontentloaded",
                                timeout=int(config.request_timeout * 1000))
                for _ in range(config.browser_steps):
                    await page.evaluate("window.scrollBy(0, window.innerHeight)")
                    await page.wait_for_timeout(150)
                    if len((await page.content()).encode()) > config.max_body_bytes:
                        raise ScoutError("浏览器页面超过响应体保护限制")
                if blocked[0] or not in_scope(page.url, config):
                    raise ScoutError("浏览器网页导航超出允许范围")
                content = (await page.content()).encode()
                if len(content) > config.max_body_bytes:
                    raise ScoutError("浏览器页面超过响应体保护限制")
                return FetchedPage(page.url, content, "text/html", tuple(dict.fromkeys(media)))
        except asyncio.CancelledError:
            raise
        except ScoutError:
            raise
        except Exception as exc:
            self.warning(f"浏览器渲染未完成（{type(exc).__name__}）；使用已获取的基础页面。")
            return base
        finally:
            await context.close()

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        await self.base.close()
