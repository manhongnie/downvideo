from __future__ import annotations

import argparse


def _proxy_url(value: str) -> str:
    from video_scout.domain.models import ScoutError
    from video_scout.domain.urls import validate_proxy_url
    try:
        return validate_proxy_url(value)
    except ScoutError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="video-scout — 跨页发现视频、勾选下载与导出")
    parser.add_argument("--database", help="SQLite 历史数据库路径（默认 XDG_DATA_HOME/video-scout）")
    parser.add_argument("--url", default="", help="预填起始 HTTP/HTTPS URL")
    parser.add_argument("--browser", action="store_true", help="预先启用可选浏览器增强")
    parser.add_argument("--browser-visible", action="store_true",
                        help="预先启用可见 Chromium（同时启用浏览器增强）")
    parser.add_argument("--proxy", type=_proxy_url,
                        help="HTTP/HTTPS 代理地址，例如 http://127.0.0.1:10808")
    args = parser.parse_args()
    from video_scout.bootstrap import build_runtime
    from video_scout.tui.app import VideoScoutApp
    app = VideoScoutApp(build_runtime(args.database, proxy=args.proxy))
    if args.url:
        app.initial_url = args.url
    app.initial_browser = args.browser or args.browser_visible
    app.initial_browser_visible = args.browser_visible
    app.run()


if __name__ == "__main__":
    main()
