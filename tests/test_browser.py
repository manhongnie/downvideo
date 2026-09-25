"""Browser fallback keeps challenge handling bounded and ignores card previews."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from video_scout.adapters.browser import OptionalBrowserFetcher
from video_scout.domain.models import PageTask, ScanConfig, ScoutError


class ChallengeFetcher:
    async def fetch(self, task, config):
        raise ScoutError("HTTP 403（Cloudflare 人机验证阻止了自动扫描）")

    async def close(self):
        pass


class FakeRequest:
    def __init__(self, url, frame, *, kind="document", navigation=False):
        self.url = url
        self.frame = frame
        self.resource_type = kind
        self._navigation = navigation

    def is_navigation_request(self):
        return self._navigation


class FakeRoute:
    def __init__(self, request):
        self.request = request
        self.action = ""

    async def abort(self):
        self.action = "aborted"

    async def continue_(self):
        self.action = "continued"


class FakePage:
    def __init__(self, context, challenge=False):
        self.context = context
        self.challenge = challenge
        self.main_frame = object()
        self.url = ""
        self.responses = {}
        self.routes = []

    def on(self, event, callback):
        self.responses[event] = callback

    async def goto(self, url, **kwargs):
        self.url = url
        main = FakeRequest(url, self.main_frame, navigation=True)
        route = FakeRoute(main)
        await self.context.route_handler(route)
        self.routes.append(route)
        self.responses["response"](SimpleNamespace(request=main, status=200,
                                                     headers={"content-type": "text/html"}, url=url))
        for request in (
            FakeRequest("https://ad.test/frame", object(), navigation=True),
            FakeRequest("https://cdn.test/clip/preview.mp4", self.main_frame, kind="media"),
            FakeRequest("https://cdn.test/clip/master.m3u8", self.main_frame, kind="media"),
        ):
            route = FakeRoute(request)
            await self.context.route_handler(route)
            self.routes.append(route)
        return SimpleNamespace(status=200)

    async def title(self):
        return "Just a moment..." if self.challenge else "Movie list"

    async def wait_for_timeout(self, milliseconds):
        if self.challenge:
            raise asyncio.TimeoutError

    async def evaluate(self, script):
        pass

    async def content(self):
        return "<html><title>Movie list</title></html>"


class FakeContext:
    def __init__(self, challenge=False):
        self.page = FakePage(self, challenge)
        self.route_handler = None
        self.closed = False

    async def route(self, pattern, handler):
        self.route_handler = handler

    async def new_page(self):
        return self.page

    async def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, challenge=False):
        self.context = FakeContext(challenge)

    async def new_context(self, **kwargs):
        assert kwargs == {"accept_downloads": False, "service_workers": "block"}
        return self.context

    async def close(self):
        pass


@pytest.mark.parametrize("visible", [False, True])
async def test_challenge_uses_browser_and_excludes_card_preview(monkeypatch, visible):
    url = "https://site.test/list"
    fetcher = OptionalBrowserFetcher(ChallengeFetcher())
    fake = FakeBrowser()
    fetcher._browser = fake
    used_modes = []

    async def ready(mode):
        used_modes.append(mode)
        return True

    monkeypatch.setattr(fetcher, "_ensure_browser", ready)
    config = ScanConfig(url, browser=True, browser_visible=visible, browser_steps=0)
    page = await fetcher.fetch(PageTask(url), config)

    assert used_modes == [visible]
    assert page.url == url
    assert page.media_urls == ("https://cdn.test/clip/master.m3u8",)
    assert [route.action for route in fake.context.page.routes] == [
        "continued", "aborted", "aborted", "aborted",
    ]
    assert fake.context.closed
    await fetcher.close()


async def test_challenge_remains_clear_when_browser_cannot_complete(monkeypatch):
    url = "https://site.test/list"
    fetcher = OptionalBrowserFetcher(ChallengeFetcher())
    fetcher._browser = FakeBrowser(challenge=True)

    async def ready(mode):
        return True

    monkeypatch.setattr(fetcher, "_ensure_browser", ready)
    with pytest.raises(ScoutError, match="Cloudflare 人机验证未在浏览器时间预算内完成"):
        await fetcher.fetch(PageTask(url), ScanConfig(url, browser=True, browser_visible=True))
    await fetcher.close()


def test_visible_mode_requires_browser_enhancement():
    with pytest.raises(ScoutError, match="需要先启用浏览器增强"):
        ScanConfig("https://site.test/", browser_visible=True).validate()
