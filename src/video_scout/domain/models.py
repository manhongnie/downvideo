"""Framework-free values shared across application boundaries."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4


class ScoutError(Exception):
    """An expected, displayable failure; adapters must redact external messages."""


@dataclass(frozen=True)
class ScanConfig:
    start_url: str
    limit: int = 100
    download_dir: str = "downloads"
    max_depth: int = 3
    max_pages: int = 500
    deadline_seconds: float = 1800
    page_concurrency: int = 1
    download_concurrency: int = 2
    allowed_hosts: tuple[str, ...] = ()
    allowed_paths: tuple[str, ...] = ()
    request_timeout: float = 20
    host_interval: float = 0.25
    retries: int = 2
    max_queue: int = 2000
    max_body_bytes: int = 2_000_000
    browser: bool = False
    browser_steps: int = 3
    browser_seconds: float = 10

    def validate(self) -> None:
        from video_scout.domain.urls import validate_url
        validate_url(self.start_url)
        for name in ("limit", "max_pages", "page_concurrency", "download_concurrency", "max_queue", "max_body_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ScoutError(f"{name} 必须是正整数")
        for name in ("max_depth", "retries", "browser_steps"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ScoutError(f"{name} 必须是非负整数")
        import math
        for name in ("deadline_seconds", "request_timeout", "browser_seconds", "host_interval"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0 or (name != "host_interval" and value == 0):
                raise ScoutError(f"{name} 必须是有限正数（限速间隔可为零）")
        if not self.download_dir.strip():
            raise ScoutError("下载目录不能为空")
        if any(not path.startswith("/") for path in self.allowed_paths):
            raise ScoutError("允许路径必须以 / 开头")


@dataclass(frozen=True)
class PageTask:
    url: str
    depth: int = 0
    kind: str = "page"  # detail, pagination, page
    source_url: str = ""


@dataclass(frozen=True)
class FetchedPage:
    url: str
    body: bytes
    content_type: str = "text/html"
    media_urls: tuple[str, ...] = ()


@dataclass(frozen=True)
class VideoCandidate:
    url: str
    source_url: str
    title: str = ""
    kind: str = "media"  # media, embed, page
    group_urls: tuple[str, ...] = ()  # explicit sources of the same HTML video element


@dataclass(frozen=True)
class MediaVariant:
    url: str
    format_id: str = "direct"
    ext: str | None = None
    protocol: str = "https"
    width: int | None = None
    height: int | None = None
    vcodec: str | None = None
    acodec: str | None = None
    # Domain-keyed context; never included in regular exports or logs.
    headers: dict[str, dict[str, str]] = field(default_factory=dict, repr=False)


@dataclass
class VideoItem:
    id: str
    title: str
    source_url: str
    page_url: str
    variants: list[MediaVariant]
    extractor: str = "direct"
    extractor_id: str | None = None
    duration: float | None = None
    status: str = "已解析"
    sources: list[str] = field(default_factory=list)

    @property
    def identity(self) -> str:
        if self.extractor_id and self.extractor != "direct":
            return f"{self.extractor}:{self.extractor_id}"
        return f"media:{self.variants[0].url}" if self.variants else f"invalid:{self.id}"

    @property
    def primary_url(self) -> str:
        return self.variants[0].url if self.variants else ""

    @classmethod
    def create(cls, *, title: str, source_url: str, page_url: str, variants: list[MediaVariant],
               extractor: str = "direct", extractor_id: str | None = None,
               duration: float | None = None) -> VideoItem:
        key = f"{extractor}:{extractor_id}" if extractor_id and extractor != "direct" else f"media:{variants[0].url}"
        return cls(sha256(key.encode()).hexdigest()[:24], title, source_url, page_url,
                   variants, extractor, extractor_id, duration, sources=[source_url])


@dataclass
class DownloadTask:
    id: str
    video: VideoItem
    target_dir: str
    filename: str
    status: str = "queued"  # queued, downloading, merging, completed, failed, cancelled
    progress: float | None = None
    file_path: str | None = None
    error: str = ""
    resumable: bool = True

    @classmethod
    def create(cls, video: VideoItem, target_dir: Path, filename: str) -> DownloadTask:
        return cls(uuid4().hex, video, str(target_dir), filename)


@dataclass(frozen=True)
class Event:
    kind: str  # session, video, candidate_error, state, log, download
    session_id: str = ""
    message: str = ""
    video: VideoItem | None = None
    download: DownloadTask | None = None
    data: dict[str, Any] = field(default_factory=dict)


EventSink = Callable[[Event], None]
ProgressSink = Callable[[str, float | None], None]


def video_from_dict(data: dict[str, Any]) -> VideoItem:
    copied = dict(data)
    copied["variants"] = [MediaVariant(**item) for item in copied["variants"]]
    return VideoItem(**copied)


def download_from_dict(data: dict[str, Any]) -> DownloadTask:
    copied = dict(data)
    copied["video"] = video_from_dict(copied["video"])
    return DownloadTask(**copied)


def video_dict(item: VideoItem, *, include_context: bool = False) -> dict[str, Any]:
    data = asdict(item)
    if not include_context:
        for variant in data["variants"]:
            variant.pop("headers", None)
    return data
