"""Explicit media-only TXT and structured exports without authentication context."""
from __future__ import annotations

import asyncio
import csv
import json
from pathlib import Path

from video_scout.domain.models import ScoutError, VideoItem, video_dict
from video_scout.domain.urls import safe_text


def _write(items: list[VideoItem], path: Path, format: str) -> Path:
    if format not in {"txt", "csv", "json"}:
        raise ScoutError("导出格式必须为 TXT、CSV 或 JSON")
    if not items:
        raise ScoutError("没有可导出的视频")
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="") as stream:
            if format == "json":
                json.dump([video_dict(item) for item in items], stream, ensure_ascii=False, indent=2)
            elif format == "txt":
                for url in dict.fromkeys(variant.url for item in items for variant in item.variants):
                    stream.write(url + "\n")
            else:
                writer = csv.writer(stream)
                writer.writerow(["video_id", "title", "source_page", "play_page", "media_url", "format", "ext", "width", "height", "duration"])
                for item in items:
                    title = safe_text(item.title)
                    if title.startswith(("=", "+", "-", "@")):
                        title = "'" + title
                    for variant in item.variants:
                        writer.writerow([item.id, title, item.source_url, item.page_url, variant.url,
                                         variant.format_id, variant.ext or "未知", variant.width or "未知",
                                         variant.height or "未知", item.duration if item.duration is not None else "未知"])
    except FileExistsError as exc:
        raise ScoutError("导出文件已存在，请选择新文件名") from exc
    return path


async def export_items(items: list[VideoItem], path: str | Path, format: str) -> Path:
    return await asyncio.to_thread(_write, items, Path(path), format.lower())
