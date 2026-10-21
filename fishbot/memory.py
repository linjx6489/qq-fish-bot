"""会话记忆与 token 账本（SQLite / aiosqlite）。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aiosqlite

from .config import MemoryConfig
from .textutil import clean_text

log = logging.getLogger("fishbot.memory")

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    session   TEXT NOT NULL,
    role      TEXT NOT NULL,
    name      TEXT DEFAULT '',
    content   TEXT NOT NULL,
    ts        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session, id);

CREATE TABLE IF NOT EXISTS usage_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    session           TEXT NOT NULL,
    user_id           TEXT DEFAULT '',
    prompt_tokens     INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens      INTEGER DEFAULT 0,
    estimated         INTEGER DEFAULT 0,
    ts                REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_session ON usage_log(session, ts);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    ts    REAL NOT NULL
);
"""


class Memory:
    def __init__(self, cfg: MemoryConfig):
        self.cfg = cfg
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    # -------------------------------------------------------------- 生命周期
    async def open(self) -> None:
        if self._db is not None:
            return
        path = self.cfg.resolve_db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        log.info("记忆库就绪：%s", path)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Memory 尚未 open()")
        return self._db

    # ------------------------------------------------------------------ 历史
    async def add(self, session: str, role: str, content: str, name: str = "") -> None:
        content = clean_text(content or "")
        if not content:
            return
        async with self._lock:
            await self.db.execute(
                "INSERT INTO messages(session, role, name, content, ts) VALUES (?,?,?,?,?)",
                (clean_text(session), role, clean_text(name), content, time.time()),
            )
            await self.db.commit()

    async def history(self, session: str, limit: int | None = None, max_chars: int | None = None) -> list[dict]:
        """取最近若干条消息，返回 OpenAI messages 片段（时间正序）。"""
        limit = limit or self.cfg.max_messages
        max_chars = max_chars if max_chars is not None else self.cfg.max_context_chars
        async with self._lock:
            cur = await self.db.execute(
                "SELECT role, name, content, ts FROM messages WHERE session=? ORDER BY id DESC LIMIT ?",
                (session, limit),
            )
            rows = list(await cur.fetchall())
        rows.reverse()

        cutoff = time.time() - self.cfg.ttl_hours * 3600
        rows = [r for r in rows if r["ts"] >= cutoff]

        picked: list[dict] = []
        used = 0
        for row in reversed(rows):  # 从最近往前塞，保证最新内容一定在
            content = row["content"]
            if used + len(content) > max_chars and picked:
                break
            used += len(content)
            # 不返回 name 字段：OpenAI 规范要求 name 只能含 [a-zA-Z0-9_-]，
            # 中文昵称会被严格实现的服务端拒绝，因此昵称直接写进正文。
            picked.append({"role": row["role"], "content": content})
        picked.reverse()
        return picked

    async def last_role_and_age(self, session: str) -> tuple[str, float] | None:
        """最近一条消息的 (role, 距今秒数)；没有就返回 None。"""
        async with self._lock:
            cur = await self.db.execute(
                "SELECT role, ts FROM messages WHERE session=? ORDER BY id DESC LIMIT 1",
                (session,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return str(row["role"]), max(0.0, time.time() - float(row["ts"]))

    async def rows(self, session: str, limit: int = 200) -> list[dict]:
        """取最近若干条原始行（带 id / role / name / content / ts），按时间正序。"""
        async with self._lock:
            cur = await self.db.execute(
                "SELECT id, role, name, content, ts FROM messages WHERE session=? ORDER BY id DESC LIMIT ?",
                (session, max(1, int(limit))),
            )
            found = list(await cur.fetchall())
        found.reverse()
        return [
            {
                "id": int(r["id"]),
                "role": str(r["role"]),
                "name": str(r["name"] or ""),
                "content": str(r["content"] or ""),
                "ts": float(r["ts"] or 0.0),
            }
            for r in found
        ]

    async def delete_before(self, session: str, before_id: int) -> int:
        """删掉 id 小于 before_id 的行，返回删了几条。"""
        async with self._lock:
            cur = await self.db.execute(
                "DELETE FROM messages WHERE session=? AND id<?",
                (session, int(before_id)),
            )
            await self.db.commit()
            return int(cur.rowcount or 0)

    async def count(self, session: str, after_id: int = 0) -> int:
        """这个会话有多少条消息；给了 after_id 就只数比它新的。"""
        async with self._lock:
            cur = await self.db.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE session=? AND id>?",
                (session, int(after_id)),
            )
            row = await cur.fetchone()
        return int(row["n"]) if row else 0

    async def clear(self, session: str) -> int:
        async with self._lock:
            cur = await self.db.execute("DELETE FROM messages WHERE session=?", (session,))
            await self.db.commit()
            return cur.rowcount or 0

    async def session_count(self) -> int:
        async with self._lock:
            cur = await self.db.execute("SELECT COUNT(DISTINCT session) AS n FROM messages")
            row = await cur.fetchone()
        return int(row["n"] if row else 0)

    # ------------------------------------------------------------- token 账本
    async def add_usage(
        self,
        session: str,
        user_id: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        estimated: bool = False,
    ) -> None:
        async with self._lock:
            await self.db.execute(
                "INSERT INTO usage_log(session, user_id, prompt_tokens, completion_tokens,"
                " total_tokens, estimated, ts) VALUES (?,?,?,?,?,?,?)",
                (
                    session,
                    str(user_id),
                    int(prompt_tokens),
                    int(completion_tokens),
                    int(total_tokens),
                    int(bool(estimated)),
                    time.time(),
                ),
            )
            await self.db.commit()

    async def usage_summary(self, session: str | None = None, since: float | None = None) -> dict:
        sql = "SELECT COUNT(*) AS n, SUM(total_tokens) AS total, SUM(prompt_tokens) AS prompt,"
        sql += " SUM(completion_tokens) AS completion FROM usage_log WHERE 1=1"
        params: list[Any] = []
        if session:
            sql += " AND session=?"
            params.append(session)
        if since:
            sql += " AND ts>=?"
            params.append(since)
        async with self._lock:
            cur = await self.db.execute(sql, params)
            row = await cur.fetchone()
        return {
            "calls": int(row["n"] or 0),
            "total_tokens": int(row["total"] or 0),
            "prompt_tokens": int(row["prompt"] or 0),
            "completion_tokens": int(row["completion"] or 0),
        }

    async def top_eaters(self, since: float | None = None, limit: int = 5) -> list[dict]:
        sql = "SELECT user_id, SUM(total_tokens) AS total FROM usage_log"
        params: list[Any] = []
        if since:
            sql += " WHERE ts>=?"
            params.append(since)
        sql += " GROUP BY user_id ORDER BY total DESC LIMIT ?"
        params.append(limit)
        async with self._lock:
            cur = await self.db.execute(sql, params)
            rows = await cur.fetchall()
        return [{"user_id": r["user_id"], "total": int(r["total"] or 0)} for r in rows]

    # -------------------------------------------------------------- 键值状态
    async def kv_get(self, key: str, default: Any = None) -> Any:
        async with self._lock:
            cur = await self.db.execute("SELECT value FROM kv WHERE key=?", (key,))
            row = await cur.fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (ValueError, TypeError):
            return default

    async def kv_set(self, key: str, value: Any) -> None:
        payload = clean_text(json.dumps(value, ensure_ascii=False))
        async with self._lock:
            await self.db.execute(
                "INSERT INTO kv(key, value, ts) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, ts=excluded.ts",
                (key, payload, time.time()),
            )
            await self.db.commit()

    async def kv_delete(self, key: str) -> None:
        async with self._lock:
            await self.db.execute("DELETE FROM kv WHERE key=?", (key,))
            await self.db.commit()
