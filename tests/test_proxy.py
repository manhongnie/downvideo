"""An explicit proxy reaches every HTTP layer without relaxing URL safeguards."""
from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import pytest

from video_scout.bootstrap import build_runtime
from video_scout.domain.models import PageTask, ScanConfig, ScoutError
from video_scout.domain.urls import validate_proxy_url


@contextmanager
def forwarding_proxy():
    paths: list[str] = []

    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, newurl):
            return None

    direct = build_opener(ProxyHandler({}), NoRedirect())

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_GET(self):
            paths.append(self.path)
            headers = {key: value for key, value in self.headers.items()
                       if key.lower() in {"range", "referer", "user-agent"}}
            try:
                response = direct.open(Request(self.path, headers=headers), timeout=5)
            except HTTPError as exc:
                response = exc
            with response:
                body = response.read()
                self.send_response(response.status)
                for key in ("Content-Type", "Content-Range", "Accept-Ranges", "Location"):
                    if response.headers.get(key):
                        self.send_header(key, response.headers[key])
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", paths
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("url", [
    "socks://127.0.0.1:10808", "http://user:secret@127.0.0.1:10808",
    "http://127.0.0.1:10808/path", "http://127.0.0.1:10808?token=secret",
])
def test_proxy_url_requires_plain_http_endpoint(url):
    with pytest.raises(ScoutError):
        validate_proxy_url(url)


async def test_explicit_proxy_covers_scan_probe_download_and_keeps_scope(demo_site, tmp_path):
    site = demo_site(total=1)
    with forwarding_proxy() as (proxy, paths):
        runtime = build_runtime(tmp_path / "proxy.sqlite", proxy=proxy)
        config = ScanConfig(site.url, limit=1, retries=0, host_interval=0)
        try:
            await runtime.configure(config)
            await asyncio.wait_for(runtime.scan.run(config), 15)
            [item] = runtime.scan.items.values()
            assert item.primary_url == site.base_url + "/media/0.mp4"
            assert any(path.endswith("/catalog?p=1") for path in paths)
            assert any(path.endswith("/media/0.mp4") for path in paths)

            [task] = await runtime.downloads.enqueue([item], str(tmp_path / "videos"))
            await asyncio.wait_for(runtime.downloads.wait(), 20)
            assert task.status == "completed", task.error
            assert task.file_path and task.file_path.endswith(".mp4")
            assert len([path for path in paths if path.endswith("/media/0.mp4")]) >= 2
            assert len(paths) == sum(site.requests.values())

            with pytest.raises(ScoutError, match="范围"):
                await runtime._http.fetch(PageTask(site.base_url + "/outside"), config)
            assert not any(path.endswith("/forbidden") for path in paths)
        finally:
            await runtime.close()
