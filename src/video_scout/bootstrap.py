"""The production composition root; all concrete adapter choices live here."""
from __future__ import annotations

import os
from pathlib import Path

from video_scout.adapters.browser import OptionalBrowserFetcher
from video_scout.adapters.http import HtmlDiscovery, HttpPageFetcher
from video_scout.adapters.media import HybridVideoResolver, YtDlpDownloader, supports_site
from video_scout.adapters.sqlite import SQLiteRepository
from video_scout.domain.models import Event, EventSink, ScanConfig, ScoutError
from video_scout.services.download import DownloadService
from video_scout.services.scan import ScanService


class Runtime:
    def __init__(self, database: str | Path | None = None, on_event: EventSink = lambda event: None):
        data_root = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
        self.repository = SQLiteRepository(database or data_root / "video-scout/scout.sqlite3")
        self.on_event = on_event
        self._http = HttpPageFetcher()
        self._browser: OptionalBrowserFetcher | None = None
        self._resolver = HybridVideoResolver()
        discovery = HtmlDiscovery(supports_site)
        self.scan = ScanService(self._http, discovery, discovery, self._resolver,
                                self.repository, self._emit)
        self.downloads = DownloadService(YtDlpDownloader(), self.repository, self._emit,
                                        can_download=lambda: not self.scan.active)
        self._closed = False

    def _emit(self, event: Event) -> None:
        self.on_event(event)

    async def configure(self, config: ScanConfig) -> None:
        config.validate()
        if any(task.status in {"queued", "downloading", "merging"} for task in self.downloads.tasks.values()):
            raise ScoutError("请等待下载完成或取消下载后，再开始新扫描")
        if config.browser:
            if self._browser is None:
                self._browser = OptionalBrowserFetcher(self._http,
                    warning=lambda message: self._emit(Event("log", message=message)))
            self.scan.fetcher = self._browser
        else:
            self.scan.fetcher = self._http

    async def close(self) -> None:
        if self._closed:
            return
        closers = [self.scan.close, self.downloads.close, self._resolver.close]
        if self._browser:
            closers.append(self._browser.close)
        closers.extend([self._http.close, self.repository.close])
        failures = []
        for closer in closers:
            try:
                await closer()
            except Exception as exc:
                failures.append(type(exc).__name__)
        self._closed = True
        if failures:
            raise ScoutError("资源清理出现错误：" + ", ".join(failures))


def build_runtime(database: str | Path | None = None, on_event: EventSink = lambda event: None) -> Runtime:
    return Runtime(database, on_event)
