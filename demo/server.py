"""Run ``python -m demo.server`` to serve an intentionally small crawling playground."""
from __future__ import annotations

import argparse
import html
import json
import re
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


class DemoServer:
    """A real HTTP server with unique media URLs, deep details and cyclic pagination.

    Each media URL serves the same valid, tiny, generated MP4. They deliberately
    have distinct identities; guessing sameness from title/content is inappropriate.
    ``requests`` records both GET and HEAD, keyed by the unmodified request path.
    """

    def __init__(self, total: int = 137, per_page: int = 10, *, port: int = 0,
                 delay: float = 0) -> None:
        if total < 0 or per_page < 1:
            raise ValueError("total >= 0 and per_page >= 1 are required")
        self.total = total
        self.per_page = per_page
        self.delay = delay
        self.requests: Counter[str] = Counter()
        self.methods: Counter[tuple[str, str]] = Counter()
        self._lock = threading.Lock()
        self.media = Path(__file__).with_name("tiny.mp4").read_bytes()
        site = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_HEAD(self) -> None:
                self.respond(head=True)

            def do_GET(self) -> None:
                self.respond(head=False)

            def send(self, body: bytes, mime: str = "text/html; charset=utf-8",
                     status: int = 200, *, head: bool = False,
                     headers: dict[str, str] | None = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(body)))
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                if not head:
                    try:
                        self.wfile.write(body)
                    except (BrokenPipeError, ConnectionResetError):
                        pass  # A bounded probe/cancel is allowed to disconnect.

            def respond(self, *, head: bool) -> None:
                with site._lock:
                    site.requests[self.path] += 1
                    site.methods[(self.command, self.path)] += 1
                    count = site.requests[self.path]
                if site.delay:
                    time.sleep(site.delay)
                parsed = urlsplit(self.path)
                path = parsed.path
                if path == "/stats":
                    self.send(json.dumps(dict(site.requests)).encode(),
                              "application/json", head=head)
                    return
                if path == "/outside":
                    self.send(b"", status=302, head=head,
                              headers={"Location": site.base_url.replace("127.0.0.1", "localhost") + "/forbidden"})
                    return
                if path == "/forbidden":
                    self.send(b'<video src="/media/9999.mp4"></video>', head=head)
                    return
                if path == "/flaky" and count < 2:
                    self.send(b"temporary", status=503, head=head)
                    return
                if path == "/limited":
                    self.send(b"rate limited", status=429, head=head,
                              headers={"Retry-After": "0"})
                    return
                if path == "/restricted":
                    self.send(b"access restricted", status=403, head=head)
                    return
                if path == "/oversized":
                    self.send(b"x" * 100_000, head=head)
                    return
                if path == "/not-video.mp4":
                    self.send(b"<html>This is HTML, not media</html>", head=head)
                    return
                match = re.fullmatch(r"/media/(\d+)\.mp4", path)
                if match:
                    data = site.media
                    headers = {"Accept-Ranges": "bytes"}
                    range_match = re.fullmatch(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
                    status = 200
                    if range_match:
                        begin = int(range_match[1])
                        end = min(int(range_match[2]) if range_match[2] else len(data) - 1,
                                  len(data) - 1)
                        if begin >= len(data):
                            self.send(b"", status=416, head=head,
                                      headers={"Content-Range": f"bytes */{len(data)}"})
                            return
                        headers["Content-Range"] = f"bytes {begin}-{end}/{len(data)}"
                        data = data[begin:end + 1]
                        status = 206
                    self.send(data, "video/mp4", status=status, head=head, headers=headers)
                    return
                if path in ("/", "/catalog", "/flaky"):
                    page = max(1, int(parse_qs(parsed.query).get("p", ["1"])[0]))
                    first = (page - 1) * site.per_page
                    last = min(first + site.per_page, site.total)
                    body = [f"<h1>本地视频目录 {page}</h1>"]
                    for number in range(first, last):
                        body.append(f'<a class="video detail" href="/detail/{number}">视频 {number}</a>')
                    if first < site.total:
                        body.append(f'<a class="video detail" href="/detail/{first}">重复视频</a>')
                    if last < site.total:
                        body.append(f'<a rel="next" href="?p={page + 1}">下一页</a>')
                    body.extend(['<a href="/catalog?p=1">循环回起点</a>',
                                 '<a href="/outside">外部重定向</a>'])
                    self.send(self.document("\n".join(body)), head=head)
                    return
                match = re.fullmatch(r"/(detail|watch)/(\d+)", path)
                if match:
                    kind, number = match.groups()
                    if kind == "detail":
                        body = f'<a class="video detail" href="../watch/{number}">播放</a>'
                    else:
                        body = (f'<h1>演示视频 {number}</h1><video title="演示视频 {number}">'
                                f'<source src="../media/{number}.mp4" type="video/mp4">'
                                f'<source src="../media/{number}.mp4" type="video/mp4">'
                                '</video>'
                                f'<a href="../media/{number}.mp4">重复媒体地址</a>')
                    body += '<a href="/catalog?p=1">返回目录</a>'
                    self.send(self.document(body), head=head)
                    return
                self.send(b"not found", status=404, head=head)

            @staticmethod
            def document(body: str) -> bytes:
                return ("<!doctype html><html lang=zh><head><meta charset=utf-8>"
                        f"<title>{html.escape('video-scout 本地测试站点')}</title>"
                        f"</head><body>{body}</body></html>").encode()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self.httpd.server_port}"
        self.url = self.base_url + "/catalog?p=1"
        self.thread: threading.Thread | None = None

    def __enter__(self) -> DemoServer:
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self.thread:
            self.thread.join(timeout=3)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--total", type=int, default=137)
    parser.add_argument("--per-page", type=int, default=10)
    args = parser.parse_args()
    with DemoServer(args.total, args.per_page, port=args.port) as site:
        print(f"video-scout 演示网址: {site.url}", flush=True)
        print("所有视频由 FFmpeg 生成；按 Ctrl+C 退出。", flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
