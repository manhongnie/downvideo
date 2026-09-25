"""Bounded probes and cancellable, structured yt-dlp subprocesses."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from collections.abc import AsyncIterator
from contextlib import aclosing
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from video_scout.domain.models import (
    DownloadTask,
    MediaVariant,
    ProgressSink,
    ScanConfig,
    ScoutError,
    VideoCandidate,
    VideoItem,
    video_dict,
)
from video_scout.domain.urls import redact, safe_text, validate_url


@lru_cache(maxsize=1)
def _site_extractors() -> tuple:
    from yt_dlp.extractor import gen_extractor_classes
    return tuple(extractor for extractor in gen_extractor_classes() if extractor.IE_NAME != "generic")


@lru_cache(maxsize=2048)
def supports_site(url: str) -> bool:
    """Offline hint for discovery; a matching extractor is not a success guarantee."""
    try:
        validate_url(url)
        return any(extractor.suitable(url) for extractor in _site_extractors())
    except (ScoutError, ValueError):
        return False


class _Worker:
    """One private process group, including any FFmpeg children, per operation."""

    def __init__(self) -> None:
        self.process: asyncio.subprocess.Process | None = None

    async def start(self, request: dict) -> None:
        self.process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "video_scout.adapters.ytdlp_worker",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True, limit=8_000_000,
        )
        await self.send(request)

    async def send(self, message: dict) -> None:
        assert self.process and self.process.stdin
        self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
        await self.process.stdin.drain()

    async def receive(self, timeout: float | None = None) -> dict:
        assert self.process and self.process.stdout
        try:
            line = await asyncio.wait_for(self.process.stdout.readline(), timeout)
            if not line:
                raise ScoutError("媒体工作进程意外退出")
            result = json.loads(line)
            if result.get("type") == "error":
                raise ScoutError(redact(result.get("message", "媒体处理失败")))
            return result
        except (ValueError, asyncio.TimeoutError) as exc:
            raise ScoutError("媒体解析超时或工作进程返回无效数据") from exc

    async def close(self) -> None:
        process = self.process
        if process is None:
            return
        self.process = None
        # Even if the Python parent exited, FFmpeg may still own the group.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), 1.5)
        except asyncio.TimeoutError:
            pass
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
            if process.stdin:
                process.stdin.close()


def _item_from_info(info: dict, candidate: VideoCandidate) -> VideoItem:
    variants = []
    seen = set()
    for fmt in info.get("formats", []):
        url = fmt.get("url", "")
        try:
            validate_url(url)
        except ScoutError:
            continue
        key = (url, str(fmt.get("format_id", "direct")))
        if key in seen:
            continue
        seen.add(key)
        host = urlsplit(url).hostname or ""
        headers = {str(k): str(v) for k, v in fmt.get("http_headers", {}).items()
                   if "\n" not in str(v) and "\r" not in str(v)}
        variants.append(MediaVariant(
            url=url, format_id=key[1], ext=fmt.get("ext"),
            protocol=fmt.get("protocol") or urlsplit(url).scheme,
            width=fmt.get("width"), height=fmt.get("height"),
            vcodec=fmt.get("vcodec"), acodec=fmt.get("acodec"),
            headers={host: headers} if headers else {},
        ))
    if not variants or all(v.vcodec == "none" for v in variants):
        raise ScoutError("解析器未返回可确认的视频媒体格式（不收录纯音频或图片）")
    extractor = info.get("extractor_key") or info.get("extractor") or "yt-dlp"
    # Generic IDs are usually URL basenames: not globally stable video identities.
    if extractor.lower() == "generic":
        extractor = "direct"
    return VideoItem.create(
        title=safe_text(info.get("title") or candidate.title or "未命名视频", 500),
        source_url=candidate.source_url, page_url=info.get("webpage_url") or candidate.url,
        variants=variants, extractor=extractor,
        extractor_id=str(info["id"]) if info.get("id") and extractor != "direct" else None,
        duration=info.get("duration"),
    )


class HybridVideoResolver:
    """HTTP confirms direct media; yt-dlp handles manifests and supported sites."""

    def __init__(self, client: httpx.AsyncClient | None = None, *, proxy: str | None = None) -> None:
        self.client = client or httpx.AsyncClient(follow_redirects=False, trust_env=False, proxy=proxy)
        self.proxy = proxy
        self._owns_client = client is None
        self._workers: set[_Worker] = set()
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._last_request: dict[str, float] = {}

    async def _probe(self, url: str, config: ScanConfig,
                     context: dict[str, str] | None = None) -> tuple[str, str | None]:
        """Read at most 4 KiB; even a server ignoring Range cannot stream a full movie."""
        current = validate_url(url)
        context_host = urlsplit(current).hostname
        for _redirect in range(6):
            host = urlsplit(current).hostname or ""
            async with self._host_locks.setdefault(host, asyncio.Lock()):
                loop = asyncio.get_running_loop()
                delay = self._last_request.get(host, 0) + config.host_interval - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                for attempt in range(config.retries + 1):
                    self._last_request[host] = loop.time()
                    try:
                        async with self.client.stream(
                            "GET", current, headers={
                                **(context or {} if host == context_host else {}),
                                "Range": "bytes=0-4095", "Accept-Encoding": "identity"},
                            timeout=config.request_timeout, follow_redirects=False,
                        ) as response:
                            if response.status_code in {301, 302, 303, 307, 308}:
                                current = validate_url(urljoin(current, response.headers.get("location", "")))
                                break
                            if response.status_code in {429, 500, 502, 503, 504} and attempt < config.retries:
                                retry = response.headers.get("retry-after", "")
                                delay = min(float(retry), 10) if retry.isdigit() else min(2 ** attempt, 5)
                                await asyncio.sleep(delay)
                                continue
                            if response.status_code >= 400:
                                raise ScoutError(f"媒体探测 HTTP {response.status_code}")
                            body = b""
                            async for chunk in response.aiter_bytes(chunk_size=4096):
                                body += chunk[:4096 - len(body)]
                                if len(body) >= 4096:
                                    break
                            mime = response.headers.get("content-type", "").split(";")[0].lower()
                            stripped = body.lstrip()
                            if stripped.startswith(b"#EXTM3U"):
                                return "manifest", "m3u8"
                            if b"<MPD" in body[:4096]:
                                return "manifest", "mpd"
                            if (mime == "text/html" or stripped.lower().startswith((b"<!doctype html", b"<html"))):
                                return "page", None
                            if len(body) > 12 and body[4:8] == b"ftyp":
                                return "media", "mp4"
                            if body.startswith(b"\x1aE\xdf\xa3"):
                                return "media", "webm"
                            if body.startswith(b"FLV"):
                                return "media", "flv"
                            if mime.startswith("video/") and body:
                                known = {"video/mp4": "mp4", "video/webm": "webm", "video/quicktime": "mov",
                                         "video/x-msvideo": "avi", "video/ogg": "ogv", "video/mp2t": "ts"}
                                return "media", known.get(mime)
                            return "unknown", None
                    except httpx.HTTPError as exc:
                        if attempt == config.retries:
                            raise ScoutError(f"媒体探测失败：{type(exc).__name__}") from exc
                        await asyncio.sleep(min(2 ** attempt, 5))
                else:
                    raise ScoutError("媒体探测失败")
        raise ScoutError("媒体重定向次数过多")

    async def resolve(self, candidate: VideoCandidate, config: ScanConfig,
                      budget: int) -> AsyncIterator[VideoItem]:
        validate_url(candidate.url)
        if budget <= 0:
            return
        if candidate.kind == "media":
            variants = []
            errors = []
            parsed_title = ""
            duration = None
            for url in dict.fromkeys((candidate.url, *candidate.group_urls)):
                context = {"User-Agent": "video-scout/0.1"}
                if candidate.source_url and candidate.source_url != url:
                    validate_url(candidate.source_url)
                    context["Referer"] = candidate.source_url
                try:
                    kind, ext = await self._probe(url, config, context)
                except ScoutError as exc:
                    if not candidate.group_urls:
                        raise
                    errors.append(str(exc))
                    continue
                if kind == "media":
                    variants.append(MediaVariant(url=url, ext=ext, protocol=urlsplit(url).scheme,
                                                 headers={urlsplit(url).hostname: context}))
                elif kind == "manifest":
                    manifest_candidate = VideoCandidate(url, candidate.source_url, candidate.title)
                    try:
                        async with aclosing(self._resolve_ytdlp(manifest_candidate, config)) as records:
                            parsed = await anext(records)
                            variants.extend(parsed.variants)
                            parsed_title = parsed.title
                            duration = parsed.duration
                    except (ScoutError, StopAsyncIteration) as exc:
                        if not candidate.group_urls:
                            raise ScoutError(str(exc) or "清单没有可解析的视频格式") from exc
                        errors.append(str(exc))
            if variants:
                title = candidate.title or parsed_title or unquote(Path(urlsplit(candidate.url).path).name) or "未命名视频"
                yield VideoItem.create(title=safe_text(title, 500), source_url=candidate.source_url,
                                       page_url=candidate.source_url, variants=variants, duration=duration)
                return
            raise ScoutError(errors[-1] if errors else "未确认视频媒体类型；后缀或 blob 地址不能作为有效视频")
        async with aclosing(self._resolve_ytdlp(candidate, config)) as records:
            async for item in records:
                yield item

    async def _resolve_ytdlp(self, candidate: VideoCandidate,
                            config: ScanConfig) -> AsyncIterator[VideoItem]:
        worker = _Worker()
        context = {"User-Agent": "video-scout/0.1"}
        if candidate.source_url and candidate.source_url != candidate.url:
            validate_url(candidate.source_url)
            context["Referer"] = candidate.source_url
        self._workers.add(worker)
        try:
            await worker.start({"mode": "resolve", "url": candidate.url, "budget": config.max_queue,
                                "timeout": config.request_timeout, "retries": config.retries,
                                "max_entries": config.max_queue, "host_interval": config.host_interval,
                                "max_body_bytes": config.max_body_bytes,
                                "proxy": self.proxy,
                                "contexts": {urlsplit(candidate.url).hostname: context}})
            # Every pull is permission from ScanService. Duplicate records consume no
            # global quota, so the initial remaining quota cannot cap raw records.
            for _ in range(config.max_queue):
                await worker.send({"command": "next"})
                result = await worker.receive(timeout=config.request_timeout * (config.retries + 2))
                if result["type"] == "end":
                    return
                yield _item_from_info(result["info"], candidate)
            raise ScoutError("播放列表候选保护上限；已有结果已保留")
        finally:
            await worker.close()
            self._workers.discard(worker)

    async def close(self) -> None:
        await asyncio.gather(*(worker.close() for worker in tuple(self._workers)))
        if self._owns_client:
            await self.client.aclose()


class YtDlpDownloader:
    """Keep incomplete files in .video-scout-partials; publish without overwriting."""

    def __init__(self, *, proxy: str | None = None) -> None:
        self._workers: set[_Worker] = set()
        self.proxy = proxy

    async def download(self, task: DownloadTask, progress: ProgressSink) -> Path:
        directory = Path(task.target_dir).expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        if not task.filename or Path(task.filename).name != task.filename or task.filename in {".", ".."}:
            raise ScoutError("下载文件名不安全")
        if not task.id.isalnum() or len(task.id) > 100:
            raise ScoutError("下载任务标识不安全")
        staging = directory / ".video-scout-partials" / task.id
        staging.mkdir(parents=True, exist_ok=True)
        worker = _Worker()
        self._workers.add(worker)
        try:
            progress("downloading", None)
            await worker.start({"mode": "download", "video": video_dict(task.video, include_context=True),
                                "staging": str(staging), "filename": task.filename, "proxy": self.proxy})
            while True:
                message = await worker.receive()
                if message["type"] == "progress":
                    progress(message["status"], message.get("fraction"))
                elif message["type"] == "complete":
                    source = Path(message["path"]).resolve()
                    if source.parent != staging.resolve() or not source.is_file() or source.stat().st_size == 0:
                        raise ScoutError("下载器没有生成有效最终文件")
                    if source.suffix in {".part", ".ytdl", ".temp"}:
                        raise ScoutError("下载器返回的仍为临时文件")
                    final = directory / source.name
                    # link is atomic and fails on existing destination. Same filesystem staging.
                    try:
                        os.link(source, final)
                    except FileExistsError as exc:
                        raise ScoutError("目标文件已存在，未覆盖；请使用新目录或检查已有文件") from exc
                    source.unlink()
                    return final
        except OSError as exc:
            raise ScoutError(f"下载文件操作失败：{type(exc).__name__}") from exc
        finally:
            await worker.close()
            self._workers.discard(worker)

    async def close(self) -> None:
        await asyncio.gather(*(worker.close() for worker in tuple(self._workers)))
