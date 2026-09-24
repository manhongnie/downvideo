"""HTTP navigation and conservative, bounded HTML discovery."""
from __future__ import annotations

import asyncio
import heapq
import time
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from video_scout.domain.models import FetchedPage, PageTask, ScanConfig, ScoutError, VideoCandidate
from video_scout.domain.urls import in_scope, safe_text, validate_url

MEDIA_EXTENSIONS = (".mp4", ".webm", ".mov", ".mkv", ".m4v", ".m3u8", ".mpd", ".ogg")
MEDIA_TYPES = ("video/", "application/vnd.apple.mpegurl", "application/x-mpegurl", "application/dash+xml")


class HttpPageFetcher:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client or httpx.AsyncClient(follow_redirects=False, trust_env=False,
                                                headers={"User-Agent": "video-scout/0.1"})
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}

    async def _pace(self, url: str, config: ScanConfig) -> None:
        host = urlsplit(url).hostname or ""
        async with self._locks.setdefault(host, asyncio.Lock()):
            delay = config.host_interval - (time.monotonic() - self._last.get(host, 0))
            if delay > 0:
                await asyncio.sleep(delay)
            self._last[host] = time.monotonic()

    async def fetch(self, task: PageTask, config: ScanConfig) -> FetchedPage:
        url = task.url
        redirects = 0
        attempts = 0
        while True:
            if not in_scope(url, config):
                raise ScoutError("页面地址或重定向超出允许范围")
            await self._pace(url, config)
            try:
                async with self.client.stream("GET", url, timeout=config.request_timeout) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location or redirects >= 5:
                            raise ScoutError("页面重定向次数超限或缺少目标")
                        url = urljoin(url, location)
                        redirects += 1
                        continue
                    if response.status_code in {429, 500, 502, 503, 504} and attempts < config.retries:
                        try:
                            retry_after = float(response.headers.get("retry-after", "0"))
                        except ValueError:
                            retry_after = 0
                        delay = min(30, max(retry_after, 0.5 * 2 ** attempts))
                        attempts += 1
                    else:
                        if response.status_code >= 400:
                            raise ScoutError(f"HTTP {response.status_code}（访问受限不无限重试）")
                        content_type = response.headers.get("content-type", "").split(";")[0].lower()
                        if any(content_type.startswith(mime) for mime in MEDIA_TYPES):
                            # Resolver confirms a bounded sample; never consume a full video here.
                            return FetchedPage(str(response.url), b"", content_type)
                        body = bytearray()
                        async for chunk in response.aiter_bytes(65536):
                            if len(body) + len(chunk) > config.max_body_bytes:
                                raise ScoutError("页面响应体超过配置上限")
                            body.extend(chunk)
                        return FetchedPage(str(response.url), bytes(body), content_type)
                await asyncio.sleep(delay)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempts >= config.retries:
                    raise ScoutError(f"页面请求失败：{type(exc).__name__}") from exc
                await asyncio.sleep(min(5, 0.5 * 2 ** attempts))
                attempts += 1

    async def close(self) -> None:
        await self.client.aclose()


def _url(base: str, value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if value.startswith("#") and not value.startswith(("#/", "#!")):
        return None
    if value.startswith(("javascript:", "mailto:", "data:")):
        return None
    joined = urljoin(base, value)
    if joined.startswith("blob:"):
        return joined
    try:
        return validate_url(joined)
    except ScoutError:
        return None


def _looks_media(url: str) -> bool:
    return urlsplit(url).path.lower().endswith(MEDIA_EXTENSIONS)


class HtmlDiscovery:
    """Two narrow ports share one HTML adapter; neither performs network requests."""

    def __init__(self, supports_site=lambda url: False):
        self.supports_site = supports_site

    def extract(self, page: FetchedPage, config: ScanConfig) -> list[VideoCandidate]:
        if any(page.content_type.startswith(mime) for mime in MEDIA_TYPES) or _looks_media(page.url):
            return [VideoCandidate(page.url, page.url)]
        soup = BeautifulSoup(page.body, "html.parser")
        title = safe_text(soup.title.get_text(" ", strip=True), 500) if soup.title else ""
        candidates: list[VideoCandidate] = []
        seen: set[str] = set()

        def add(url: str | None, kind: str = "media", group: tuple[str, ...] = (), label: str = "") -> None:
            if url and url not in seen and len(candidates) < config.max_queue + 1:
                seen.add(url)
                candidates.append(VideoCandidate(url, page.url, label or title, kind, group))

        for video in soup.find_all("video"):
            sources = [_url(page.url, video.get("src"))]
            sources.extend(_url(page.url, source.get("src")) for source in video.find_all("source"))
            urls = tuple(dict.fromkeys(source for source in sources if source))
            if urls:
                add(urls[0], group=urls, label=safe_text(video.get("title", ""), 500))
                seen.update(urls)
        for tag in soup.find_all(["a", "iframe", "embed", "object", "meta"]):
            if tag.name == "a":
                url = _url(page.url, tag.get("href"))
                if url and _looks_media(url):
                    add(url, label=safe_text(tag.get_text(" ", strip=True), 500))
            elif tag.name == "meta":
                prop = tag.get("property", "")
                if prop in {"og:video", "og:video:url", "og:video:secure_url"}:
                    url = _url(page.url, tag.get("content"))
                    # External hints may be media, but do not grant permission to
                    # navigate an external player page or enumerate its playlist.
                    add(url, "embed" if url and in_scope(url, config) and not _looks_media(url) else "media")
            else:
                url = _url(page.url, tag.get("src") or tag.get("data"))
                # External embedded players are webpage navigation, not CDN media.
                if url and (_looks_media(url) or in_scope(url, config)):
                    add(url, "media" if _looks_media(url) else "embed")
        for url in page.media_urls:
            add(_url(page.url, url))
        # Let yt-dlp's site extractor handle a visited play page when HTML offers no media.
        # Resolve only pages with play/embed hints, avoiding yt-dlp on every list page.
        path = urlsplit(page.url).path.lower()
        if not candidates and (self.supports_site(page.url) or any(hint in path for hint in ("/watch", "/video", "/play", "/embed", "/playlist"))
                               or soup.find("video") is not None):
            add(page.url, "page")
        return candidates

    def discover(self, page: FetchedPage, task: PageTask, config: ScanConfig) -> list[PageTask]:
        if any(page.content_type.startswith(mime) for mime in MEDIA_TYPES):
            return []
        soup = BeautifulSoup(page.body, "html.parser")
        # Keep the best bounded set even when navigation links precede pagination
        # in document order. One extra entry lets the service report truncation.
        links: list[tuple[int, int, PageTask]] = []
        seen: set[str] = set()
        order = 0
        for tag in soup.find_all(["a", "link", "iframe"]):
            url = _url(page.url, tag.get("href") if tag.name != "iframe" else tag.get("src"))
            if not url or url in seen or _looks_media(url) or not in_scope(url, config):
                continue
            seen.add(url)
            rel = tag.get("rel", [])
            text = tag.get_text(" ", strip=True).lower()
            classes = " ".join(tag.get("class", [])).lower()
            parent_classes = " ".join(tag.parent.get("class", [])) if tag.parent else ""
            pagination = "next" in rel or text in {"next", "next page", "下一页", "下页", "›", "»"}
            pagination = pagination or "pagination" in classes or "pagination" in parent_classes
            detail = tag.name == "iframe" or any(hint in classes for hint in ("video", "detail", "play"))
            detail = detail or any(hint in urlsplit(url).path.lower() for hint in ("/watch", "/video/", "/detail/", "/play/"))
            kind = "pagination" if pagination else "detail" if detail else "page"
            depth = task.depth if pagination else task.depth + 1
            if depth <= config.max_depth:
                order += 1
                priority = {"detail": 0, "pagination": 1, "page": 2}[kind]
                entry = (-priority, -order, PageTask(url, depth, kind, page.url))
                if len(links) < config.max_queue + 1:
                    heapq.heappush(links, entry)
                elif entry[:2] > links[0][:2]:
                    heapq.heapreplace(links, entry)
        return [entry[2] for entry in sorted(links, reverse=True)]
