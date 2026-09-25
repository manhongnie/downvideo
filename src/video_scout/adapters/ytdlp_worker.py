"""Private JSON-lines subprocess protocol. Never parse yt-dlp's human output."""
from __future__ import annotations

import contextlib
import json
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

from video_scout.domain.urls import redact, validate_url


def emit(message: dict) -> None:
    sys.__stdout__.write(json.dumps(message, ensure_ascii=False, allow_nan=False) + "\n")
    sys.__stdout__.flush()


class QuietLogger:
    def debug(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        pass


def options(request: dict) -> dict:
    opts = {
        "quiet": True, "no_warnings": True, "noprogress": True,
        "logger": QuietLogger(), "cachedir": False, "cookiefile": None,
        "cookiesfrombrowser": None, "socket_timeout": request.get("timeout", 20),
        "retries": request.get("retries", 2), "fragment_retries": 2,
        "extractor_retries": request.get("retries", 2), "skip_unavailable_fragments": False,
        "lazy_playlist": True, "ignoreerrors": False, "allow_unplayable_formats": False,
        "sleep_interval_requests": request.get("host_interval", 0.25),
        "overwrites": False, "continuedl": True, "nopart": False,
        "concurrent_fragment_downloads": 1,
    }
    if request.get("proxy"):
        opts["proxy"] = request["proxy"]
    return opts


def safe_youtube_dl(opts: dict, contexts: dict | None = None, body_limit: int | None = None):
    """Use one audited HTTP(S) transport with redirect-aware domain isolation.

    The pinned yt-dlp urllib transport normally forwards Authorization on redirect.
    Our process-local subclass validates every redirect and rebuilds cross-domain
    headers from a small public allowlist plus explicitly recorded target context.
    """
    import yt_dlp
    from yt_dlp.networking import Request, Response
    from yt_dlp.networking._urllib import RedirectHandler, UrllibRH

    contexts = contexts or {}

    class BoundedResponse(Response):
        def __init__(self, response):
            super().__init__(response, response.url, response.headers, response.status, response.reason)
            self.consumed = 0

        def read(self, amt=None):
            remaining = body_limit - self.consumed
            data = self.fp.read(min(remaining + 1, amt) if amt is not None else remaining + 1)
            self.consumed += len(data)
            if self.consumed > body_limit:
                self.close()
                raise ValueError("媒体解析响应超过大小保护限制")
            return data

    class ScopedRedirectHandler(RedirectHandler):
        handler_order = 400

        def redirect_request(self, req, fp, code, msg, headers, newurl):
            validate_url(newurl)
            redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
            previous = urlsplit(req.full_url)
            target = urlsplit(newurl)
            if (previous.scheme, previous.hostname) != (target.scheme, target.hostname):
                public_headers = {"user-agent", "accept", "accept-language", "accept-encoding",
                                  "content-type", "content-length", "range"}
                for name in tuple(redirected.headers):
                    if name.lower() not in public_headers:
                        redirected.remove_header(name)
                for name, value in contexts.get(target.hostname, {}).items():
                    if "\r" not in value and "\n" not in value:
                        redirected.add_header(name, value)
            return redirected

    class ScopedUrllibRH(UrllibRH):
        _SUPPORTED_URL_SCHEMES = ("http", "https")

        def _create_instance(self, *args, **kwargs):
            opener = super()._create_instance(*args, **kwargs)
            opener.add_handler(ScopedRedirectHandler())
            return opener

    class ScopedYoutubeDL(yt_dlp.YoutubeDL):
        def build_request_director(self, handlers, preferences=None):
            return super().build_request_director([ScopedUrllibRH], preferences=[])

        def urlopen(self, req):
            if isinstance(req, str):
                req = Request(req)
            validate_url(req.url)
            host = urlsplit(req.url).hostname or ""
            for name, value in contexts.get(host, {}).items():
                if "\r" not in value and "\n" not in value:
                    req.headers[name] = value
            if body_limit:
                req.headers["Accept-Encoding"] = "identity"
            response = super().urlopen(req)
            return BoundedResponse(response) if body_limit else response

    return ScopedYoutubeDL(opts)


def info_records(ydl, info: dict, remaining: list[int], depth: int = 0) -> Iterator[dict]:
    """No list(), playlist processing or lookahead: next() owns every page request."""
    if depth > 10:
        raise ValueError("解析器嵌套层级过多")
    if info is None:
        return
    kind = info.get("_type", "video")
    if kind in {"playlist", "multi_video"}:
        for entry in info.get("entries") or ():
            if remaining[0] <= 0:
                raise ValueError("播放列表候选数达到保护上限")
            remaining[0] -= 1
            if entry:
                yield from info_records(ydl, entry, remaining, depth + 1)
        return
    if kind in {"url", "url_transparent"}:
        validate_url(info["url"])
        resolved = ydl.extract_info(info["url"], download=False, process=False,
                                    ie_key=info.get("ie_key"))
        yield from info_records(ydl, resolved, remaining, depth + 1)
        return
    resolved = ydl.process_ie_result(info, download=False)
    if resolved.get("has_drm"):
        raise ValueError("不支持 DRM 媒体")
    fields = ("id", "title", "webpage_url", "extractor", "extractor_key", "duration")
    record = {key: resolved.get(key) for key in fields}
    formats = resolved.get("formats") or [resolved]
    format_fields = ("url", "format_id", "ext", "protocol", "width", "height", "vcodec", "acodec", "http_headers")
    record["formats"] = [
        {key: fmt.get(key) for key in format_fields if fmt.get(key) is not None}
        for fmt in formats if fmt.get("url") and not fmt.get("has_drm")
    ]
    if not record["formats"]:
        raise ValueError("没有可用媒体格式")
    yield record


def resolve(request: dict) -> None:
    contexts = request.get("contexts", {})
    with safe_youtube_dl(options(request), contexts, body_limit=request.get("max_body_bytes", 2_000_000)) as ydl:
        def records():
            root = ydl.extract_info(request["url"], download=False, process=False)
            yield from info_records(ydl, root, [request.get("max_entries", 2000)])
        iterator = records()
        for _ in range(request["budget"]):
            command = sys.stdin.readline()
            if not command or json.loads(command).get("command") != "next":
                return
            try:
                info = next(iterator)
            except StopIteration:
                emit({"type": "end"})
                return
            for fmt in info["formats"]:
                host = urlsplit(fmt["url"]).hostname
                fmt["http_headers"] = {**fmt.get("http_headers", {}), **contexts.get(host, {})}
            emit({"type": "item", "info": info})


def download(request: dict) -> None:
    video = request["video"]
    staging = Path(request["staging"]).resolve()
    contexts: dict[str, dict[str, str]] = {}
    for variant in video["variants"]:
        for domain, headers in variant.get("headers", {}).items():
            contexts.setdefault(domain, {}).update(headers)

    last_emit = [0.0]
    paths = []

    def progress(data: dict) -> None:
        if data["status"] == "downloading":
            now = time.monotonic()
            if now - last_emit[0] < 0.1:
                return
            last_emit[0] = now
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            fraction = min(100.0, data.get("downloaded_bytes", 0) / total * 100) if total else None
            emit({"type": "progress", "status": "downloading", "fraction": fraction})
        elif data["status"] == "finished":
            paths.append(data.get("filename"))

    def postprocess(data: dict) -> None:
        processor = data.get("postprocessor", "")
        if data.get("status") in {"started", "processing"} and any(
            operation in processor for operation in ("Merger", "Remux", "Fixup")
        ):
            emit({"type": "progress", "status": "merging", "fraction": None})
        if data.get("status") == "finished":
            paths.append(data.get("info_dict", {}).get("filepath"))

    opts = options(request)
    opts.update({"outtmpl": str(staging / (request["filename"].replace("%", "%%") + ".%(ext)s")),
                 "progress_hooks": [progress], "postprocessor_hooks": [postprocess],
                 "noplaylist": True, "playlist_items": "1", "format": "bv*+ba/b"})
    refresh = video.get("extractor") != "direct" and video.get("page_url")
    url = video["page_url"] if refresh else video["variants"][0]["url"]
    validate_url(url)
    with safe_youtube_dl(opts, contexts) as ydl:
        info = ydl.extract_info(url, download=False)
        if not info or info.get("_type") in {"playlist", "multi_video"}:
            raise ValueError("下载前刷新返回播放列表，不能确定所选视频")
        if refresh and str(info.get("id")) != video.get("extractor_id"):
            raise ValueError("刷新后的媒体标识改变，已停止以避免下载错误视频")
        if info.get("has_drm"):
            raise ValueError("不支持 DRM 媒体")
        result = ydl.process_ie_result(info, download=True)
        paths.extend([result.get("filepath"), result.get("_filename"), ydl.prepare_filename(result)])
    # Prefer postprocessed final output; all remaining candidates must really exist.
    existing = [Path(path).resolve() for path in paths if path and Path(path).is_file()
                and Path(path).parent.resolve() == staging and Path(path).suffix not in {".part", ".ytdl", ".temp"}]
    if not existing:
        raise ValueError("下载结束但未生成最终文件")
    emit({"type": "complete", "path": str(existing[-1])})


def main() -> None:
    try:
        request = json.loads(sys.stdin.readline())
        # Third-party accidental prints cannot corrupt our machine-readable channel.
        with contextlib.redirect_stdout(sys.stderr):
            if request["mode"] == "resolve":
                resolve(request)
            elif request["mode"] == "download":
                download(request)
            else:
                raise ValueError("未知媒体工作模式")
    except Exception as exc:
        emit({"type": "error", "message": redact(exc)})


if __name__ == "__main__":
    main()
