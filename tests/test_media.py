from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from demo.server import DemoServer
from video_scout.adapters.browser import OptionalBrowserFetcher
from video_scout.adapters.media import (
    HybridVideoResolver,
    YtDlpDownloader,
    _item_from_info,
    _Worker,
)
from video_scout.adapters.ytdlp_worker import info_records, options, safe_youtube_dl
from video_scout.domain.models import (
    DownloadTask,
    FetchedPage,
    MediaVariant,
    PageTask,
    ScanConfig,
    ScoutError,
    VideoCandidate,
    VideoItem,
)


async def test_probe_confirms_type_preserves_signature_and_groups_sources():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(206, headers={"content-type": "video/mp4"},
                              content=b"\x00\x00\x00\x18ftypisom" + b"\x00" * 100)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resolver = HybridVideoResolver(client)
        url = "https://cdn.test/clip.mp4?signature=secret&quality=1"
        candidate = VideoCandidate(url, "https://page.test/watch", "title",
                                   group_urls=(url, "https://cdn.test/clip.mp4?signature=other&quality=2"))
        items = [item async for item in resolver.resolve(candidate, ScanConfig(candidate.source_url, host_interval=0), 1)]
        assert len(items) == 1
        assert len(items[0].variants) == 2
        assert items[0].primary_url == url
        assert all(request.headers["range"] == "bytes=0-4095" for request in requests)
        assert all(request.headers["referer"] == candidate.source_url for request in requests)
        assert items[0].variants[0].headers["cdn.test"]["Referer"] == candidate.source_url


async def test_suffix_is_not_confirmation_and_access_denied_not_retried():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/restricted":
            return httpx.Response(403, content=b"denied")
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>login</html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resolver = HybridVideoResolver(client)
        config = ScanConfig("https://test.test", host_interval=0)
        for path in ("/fake.mp4", "/restricted"):
            with pytest.raises(ScoutError):
                _ = [item async for item in resolver.resolve(VideoCandidate(config.start_url + path, config.start_url), config, 1)]
        assert len(requests) == 2


async def test_probe_bound_when_server_ignores_range():
    class Stream(httpx.AsyncByteStream):
        chunks = 0

        async def __aiter__(self):
            for _ in range(1000):
                self.chunks += 1
                yield b"a" * 4096

    stream = Stream()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, headers={"content-type": "video/mp4"}, stream=stream)
    )) as client:
        resolver = HybridVideoResolver(client)
        config = ScanConfig("https://test.test", host_interval=0)
        items = [item async for item in resolver.resolve(VideoCandidate(config.start_url, config.start_url), config, 1)]
        assert len(items) == 1
        assert stream.chunks == 1


def test_playlist_enumeration_is_lazy_and_generic_ids_are_not_stable():
    observed = []

    def entries():
        for number in range(1000):
            observed.append(number)
            yield {"id": str(number), "title": "clip", "url": f"https://test.test/{number}.mp4"}

    class YDL:
        def process_ie_result(self, entry, download):
            assert download is False
            return entry

    iterator = info_records(YDL(), {"_type": "playlist", "entries": entries()}, [1000])
    assert observed == []
    assert next(iterator)["id"] == "0"
    assert observed == [0]
    iterator.close()
    assert observed == [0]
    info = {"id": "same", "extractor_key": "Generic", "formats": [{"url": "https://test.test/1.mp4"}]}
    item = _item_from_info(info, VideoCandidate("https://test.test/1.mp4", "https://test.test"))
    assert item.identity == "media:https://test.test/1.mp4"


async def test_duplicate_playlist_entries_do_not_consume_unique_quota(monkeypatch):
    records = iter(["a", "a", "b", "should-not-be-requested"])
    commands = []

    class Worker:
        async def start(self, request):
            assert request["budget"] > 2

        async def send(self, command):
            commands.append(command)

        async def receive(self, timeout=None):
            identity = next(records)
            return {"type": "item", "info": {
                "id": identity, "extractor_key": "TestSite",
                "formats": [{"url": f"https://cdn.test/{identity}.mp4"}],
            }}

        async def close(self):
            pass

    monkeypatch.setattr("video_scout.adapters.media._Worker", Worker)
    resolver = HybridVideoResolver()
    config = ScanConfig("https://site.test/playlist", limit=2)
    iterator = resolver.resolve(VideoCandidate(config.start_url, config.start_url, kind="page"), config, 2)
    unique = set()
    try:
        async for item in iterator:
            unique.add(item.identity)
            if len(unique) == 2:
                break
    finally:
        await iterator.aclose()
        await resolver.close()
    assert len(commands) == 3
    assert len(unique) == 2


@pytest.mark.parametrize("first,expected", [("bad", {"stream.m3u8"}),
                                             ("clip.mp4", {"clip.mp4", "stream.m3u8"})])
async def test_explicit_source_group_resolves_actual_manifest_and_merges(monkeypatch, first, expected):
    started = []

    class Worker:
        async def start(self, request):
            started.append(request)

        async def send(self, command):
            pass

        async def receive(self, timeout=None):
            return {"type": "item", "info": {"id": "stream", "extractor_key": "Generic", "formats": [
                {"url": "https://cdn.test/stream.m3u8", "protocol": "m3u8_native", "ext": "mp4"},
            ]}}

        async def close(self):
            pass

    def response(request):
        if request.url.path.endswith("mp4"):
            return httpx.Response(200, headers={"content-type": "video/mp4"}, content=b"video")
        if request.url.path.endswith("m3u8"):
            return httpx.Response(200, content=b"#EXTM3U\n#EXT-X-ENDLIST")
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>no video</html>")

    monkeypatch.setattr("video_scout.adapters.media._Worker", Worker)
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        resolver = HybridVideoResolver(client)
        candidate = VideoCandidate(f"https://cdn.test/{first}", "https://page.test/watch",
                                   group_urls=("https://cdn.test/stream.m3u8",))
        items = [item async for item in resolver.resolve(candidate, ScanConfig(candidate.source_url, host_interval=0), 1)]
        assert len(items) == 1
        assert {variant.url.rsplit("/", 1)[-1] for variant in items[0].variants} == expected
        assert len(started) == 1
        assert started[0]["url"] == "https://cdn.test/stream.m3u8"
        assert started[0]["contexts"]["cdn.test"]["Referer"] == candidate.source_url


async def test_playlist_raw_cap_reports_protection_without_extra_pull(monkeypatch):
    pulls = []

    class Worker:
        async def start(self, request):
            pass

        async def send(self, command):
            pulls.append(command)

        async def receive(self, timeout=None):
            return {"type": "item", "info": {"id": "duplicate", "extractor_key": "Test", "formats": [
                {"url": "https://cdn.test/clip.mp4"},
            ]}}

        async def close(self):
            pass

    monkeypatch.setattr("video_scout.adapters.media._Worker", Worker)
    resolver = HybridVideoResolver()
    config = ScanConfig("https://page.test/playlist", max_queue=2)
    try:
        with pytest.raises(ScoutError, match="播放列表候选保护上限"):
            _ = [item async for item in resolver.resolve(VideoCandidate(config.start_url, config.start_url, kind="page"), config, 1)]
        assert len(pulls) == 2
    finally:
        await resolver.close()


async def test_worker_cancellation_reaps_process_group(tmp_path):
    pid_path = tmp_path / "child.pid"
    code = ("import subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(100)']); "
            f"open({str(pid_path)!r},'w').write(str(child.pid)); time.sleep(100)")
    worker = _Worker()
    worker.process = await asyncio.create_subprocess_exec(sys.executable, "-c", code, start_new_session=True)
    process = worker.process
    for _ in range(100):
        if pid_path.exists():
            break
        await asyncio.sleep(0.01)
    child_pid = int(pid_path.read_text())
    await worker.close()
    assert process.returncode is not None
    child_stat = Path(f"/proc/{child_pid}/stat")
    assert not child_stat.exists() or child_stat.read_text().split()[2] == "Z"


async def test_real_ytdlp_download_and_no_overwrite(tmp_path):
    with DemoServer(total=1) as site:
        media = site.base_url + "/media/0.mp4"
        item = VideoItem.create(title="真实视频", source_url=site.url, page_url=site.url,
                                variants=[MediaVariant(media, ext="mp4")])
        directory = tmp_path / "中文 下载"
        task = DownloadTask.create(item, directory, "真实视频-123abc")
        downloader = YtDlpDownloader()
        progress = []
        try:
            path = await downloader.download(task, lambda status, fraction: progress.append((status, fraction)))
            assert path.parent == directory
            assert path.read_bytes() == site.media
            assert path.suffix == ".mp4"
            assert any(status == "downloading" for status, _ in progress)
            with pytest.raises(ScoutError, match="未覆盖"):
                await downloader.download(task, lambda *_: None)
            assert path.read_bytes() == site.media
        finally:
            await downloader.close()


async def test_cancel_download_leaves_no_final_file(tmp_path):
    with DemoServer(total=1, delay=0.5) as site:
        item = VideoItem.create(title="clip", source_url=site.url, page_url=site.url,
                                variants=[MediaVariant(site.base_url + "/media/0.mp4", ext="mp4")])
        downloader = YtDlpDownloader()
        task = asyncio.create_task(downloader.download(DownloadTask.create(item, tmp_path, "clip"), lambda *_: None))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not downloader._workers
        assert not list(tmp_path.glob("*.mp4"))
        await downloader.close()


async def test_browser_disabled_never_initializes_sdk():
    class Fetcher:
        async def fetch(self, task, config):
            return FetchedPage(task.url, b"<html></html>")

        async def close(self):
            pass

    browser = OptionalBrowserFetcher(Fetcher())
    config = ScanConfig("https://test.test")
    page = await browser.fetch(PageTask(config.start_url), config)
    assert page.body == b"<html></html>"
    assert browser._playwright is None
    await browser.close()


def test_ytdlp_redirect_context_domain_isolation_and_scheme_validation():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{self.server.server_port}/target")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            received.append(dict(self.headers))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        contexts = {"127.0.0.1": {"Authorization": "Bearer private", "Cookie": "a=b", "X-Secret": "private"}}
        with safe_youtube_dl(options({}), contexts) as ydl:
            response = ydl.urlopen(f"http://127.0.0.1:{server.server_port}/redirect")
            assert response.read() == b"ok"
            response.close()
            assert received
            headers = {key.lower(): value for key, value in received[0].items()}
            assert not {"authorization", "cookie", "x-secret"} & headers.keys()
            with pytest.raises(ScoutError):
                ydl.urlopen("file:///etc/passwd")
        with safe_youtube_dl(options({}), body_limit=1) as ydl:
            response = ydl.urlopen(f"http://127.0.0.1:{server.server_port}/target")
            with pytest.raises(ValueError, match="大小"):
                response.read()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg 未安装，真实 HLS 集成未执行")
async def test_real_hls_resolve_and_download(tmp_path):
    served = tmp_path / "served"
    served.mkdir()
    result = subprocess.run([
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-stream_loop", "8", "-i", str(Path(__file__).parents[1] / "demo" / "tiny.mp4"),
        "-t", "3", "-c", "copy", "-hls_time", "1", "-hls_list_size", "0", str(served / "stream.m3u8"),
    ], capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode()

    class HotlinkProtectedHandler(SimpleHTTPRequestHandler):
        def do_GET(self):
            expected = f"http://127.0.0.1:{self.server.server_port}/watch"
            if self.headers.get("Referer") != expected:
                self.send_error(403)
                return
            super().do_GET()

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(HotlinkProtectedHandler, directory=str(served)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/stream.m3u8"
    resolver = HybridVideoResolver()
    downloader = YtDlpDownloader()
    try:
        config = ScanConfig(url, limit=1, host_interval=0)
        iterator = resolver.resolve(VideoCandidate(url, url.rsplit("/", 1)[0] + "/watch"), config, 1)
        item = await anext(iterator)
        await iterator.aclose()
        assert item.variants
        assert any("m3u8" in variant.protocol for variant in item.variants)
        task = DownloadTask.create(item, tmp_path / "download", "hls-video")
        path = await downloader.download(task, lambda *_: None)
        assert path.stat().st_size > 0
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
                               capture_output=True, timeout=10)
        assert probe.returncode == 0
        assert b"h264" in probe.stdout
    finally:
        await resolver.close()
        await downloader.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg 未安装，真实 DASH 合并集成未执行")
async def test_real_dash_multiple_formats_and_ffmpeg_merge(tmp_path):
    served = tmp_path / "served"
    served.mkdir()
    result = subprocess.run([
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=size=64x48:rate=5",
        "-f", "lavfi", "-i", "sine=frequency=400:sample_rate=22050",
        "-t", "2", "-c:v", "libx264", "-c:a", "aac", "-seg_duration", "1",
        "-f", "dash", str(served / "manifest.mpd"),
    ], capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode()
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(served)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/manifest.mpd"
    resolver = HybridVideoResolver()
    downloader = YtDlpDownloader()
    try:
        config = ScanConfig(url, limit=1, host_interval=0)
        iterator = resolver.resolve(VideoCandidate(url, url), config, 1)
        item = await anext(iterator)
        await iterator.aclose()
        assert len(item.variants) >= 2
        assert any(variant.vcodec == "none" for variant in item.variants)
        assert any(variant.acodec == "none" for variant in item.variants)
        progress = []
        task = DownloadTask.create(item, tmp_path / "download", "dash-video")
        path = await downloader.download(task, lambda status, fraction: progress.append(status))
        assert path.stat().st_size > 0
        assert "merging" in progress
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
                                "-of", "csv=p=0", str(path)], capture_output=True, timeout=10)
        assert probe.returncode == 0
        assert b"video" in probe.stdout and b"audio" in probe.stdout
    finally:
        await resolver.close()
        await downloader.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


async def test_missing_browser_warns_and_keeps_basic_mode(monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright.async_api", None)

    class Fetcher:
        async def fetch(self, task, config):
            return FetchedPage(task.url, b"<video src='/clip.mp4'></video>")

        async def close(self):
            pass

    warnings = []
    browser = OptionalBrowserFetcher(Fetcher(), warnings.append)
    config = ScanConfig("https://test.test", browser=True)
    try:
        page = await browser.fetch(PageTask(config.start_url), config)
        assert b"clip.mp4" in page.body
        await browser.fetch(PageTask(config.start_url), config)
        assert len(warnings) == 1
        assert "基础 HTTP 模式" in warnings[0]
    finally:
        await browser.close()
