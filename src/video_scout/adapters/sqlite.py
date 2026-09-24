"""One SQLite connection on one dedicated executor: durable I/O never blocks the TUI."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

from video_scout.domain.models import (
    DownloadTask,
    ScanConfig,
    VideoCandidate,
    VideoItem,
    download_from_dict,
    video_dict,
    video_from_dict,
)
from video_scout.domain.urls import redact


class SQLiteRepository:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scout-db")
        self._connection: sqlite3.Connection | None = None
        self._closed = False

    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path)
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                db.close()
                raise RuntimeError("数据库版本高于本程序支持版本，拒绝写入")
            db.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, started TEXT DEFAULT CURRENT_TIMESTAMP,
                    config TEXT NOT NULL, reason TEXT NOT NULL DEFAULT 'running');
                CREATE TABLE IF NOT EXISTS videos (
                    session_id TEXT NOT NULL REFERENCES sessions(id), id TEXT NOT NULL,
                    data TEXT NOT NULL, PRIMARY KEY(session_id,id));
                CREATE TABLE IF NOT EXISTS candidate_errors (
                    session_id TEXT NOT NULL REFERENCES sessions(id), url TEXT, reason TEXT);
                CREATE TABLE IF NOT EXISTS downloads (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                PRAGMA user_version=1;
            """)
            # A previous process can have exited before recording a terminal state.
            db.execute("UPDATE sessions SET reason='上次进程中断' WHERE reason='running'")
            for key, raw in db.execute("SELECT id,data FROM downloads").fetchall():
                data = json.loads(raw)
                if data["status"] in {"queued", "downloading", "merging"}:
                    data.update(status="cancelled", error="上次进程中断，可重试续传")
                    db.execute("UPDATE downloads SET data=? WHERE id=?", (json.dumps(data, ensure_ascii=False), key))
            db.commit()
            self._connection = db
        return self._connection

    async def _run(self, operation):
        if self._closed:
            raise RuntimeError("数据库已关闭")
        return await asyncio.get_running_loop().run_in_executor(self._executor, lambda: operation(self._db()))

    async def start_session(self, session_id: str, config: ScanConfig) -> None:
        def save(db):
            with db:
                db.execute("INSERT INTO sessions(id,config) VALUES (?,?)",
                           (session_id, json.dumps(asdict(config), ensure_ascii=False)))
        await self._run(save)

    async def finish_session(self, session_id: str, reason: str) -> None:
        def save(db):
            with db:
                db.execute("UPDATE sessions SET reason=? WHERE id=?", (redact(reason), session_id))
        await self._run(save)

    async def save_video(self, session_id: str, video: VideoItem) -> None:
        def save(db):
            with db:
                db.execute("INSERT INTO videos(session_id,id,data) VALUES (?,?,?) "
                           "ON CONFLICT(session_id,id) DO UPDATE SET data=excluded.data",
                           (session_id, video.id, json.dumps(video_dict(video, include_context=True), ensure_ascii=False)))
        await self._run(save)

    async def save_candidate_error(self, session_id: str, candidate: VideoCandidate, reason: str) -> None:
        def save(db):
            with db:
                db.execute("INSERT INTO candidate_errors VALUES (?,?,?)", (session_id, redact(candidate.url), redact(reason)))
        await self._run(save)

    async def list_sessions(self) -> list[dict]:
        return await self._run(lambda db: [dict(id=row[0], started=row[1], config=json.loads(row[2]), reason=row[3])
                                          for row in db.execute("SELECT id,started,config,reason FROM sessions ORDER BY rowid DESC")])

    async def list_videos(self, session_id: str | None = None) -> list[VideoItem]:
        def read(db):
            key = session_id
            if key is None:
                row = db.execute("SELECT id FROM sessions ORDER BY rowid DESC LIMIT 1").fetchone()
                key = row[0] if row else ""
            return [video_from_dict(json.loads(row[0])) for row in db.execute(
                "SELECT data FROM videos WHERE session_id=? ORDER BY rowid", (key,))]
        return await self._run(read)

    async def save_download(self, task: DownloadTask) -> None:
        # Snapshot before crossing the thread boundary.
        raw = json.dumps(asdict(task), ensure_ascii=False)
        def save(db):
            with db:
                db.execute("INSERT INTO downloads VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data", (task.id, raw))
        await self._run(save)

    async def list_downloads(self) -> list[DownloadTask]:
        return await self._run(lambda db: [download_from_dict(json.loads(row[0])) for row in db.execute("SELECT data FROM downloads ORDER BY rowid")])

    async def close(self) -> None:
        if self._closed:
            return
        if self._connection is not None:
            await self._run(lambda db: db.close())
        self._closed = True
        self._executor.shutdown(wait=True)
