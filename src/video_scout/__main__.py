from __future__ import annotations

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(description="video-scout — 跨页发现视频、勾选下载与导出")
    parser.add_argument("--database", help="SQLite 历史数据库路径（默认 XDG_DATA_HOME/video-scout）")
    parser.add_argument("--url", default="", help="预填起始 HTTP/HTTPS URL")
    args = parser.parse_args()
    from video_scout.bootstrap import build_runtime
    from video_scout.tui.app import VideoScoutApp
    app = VideoScoutApp(build_runtime(args.database))
    if args.url:
        app.initial_url = args.url
    app.run()


if __name__ == "__main__":
    main()
