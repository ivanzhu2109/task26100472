"""会话合并与历史阅读的核心实现（骨架）。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional, Sequence

from .errors import (
    UnknownConversationError,
    ContactMismatchError,
    InvalidCursorError,
    ChatMergeError,
)

_CURSOR_PREFIX = "cmr1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conv_group (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS conversation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER NOT NULL,
    group_id INTEGER NOT NULL REFERENCES conv_group(id)
);
CREATE TABLE IF NOT EXISTS message (
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    body TEXT NOT NULL,
    source_conversation_id INTEGER NOT NULL REFERENCES conversation(id)
);
CREATE TABLE IF NOT EXISTS read_session (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS read_session_message (
    session_id INTEGER NOT NULL REFERENCES read_session(id),
    seq INTEGER NOT NULL,
    message_id INTEGER NOT NULL REFERENCES message(id),
    PRIMARY KEY (session_id, seq)
);
"""


@dataclass(frozen=True)
class Message:
    """一条消息。编号全局唯一，内容不可修改、不可删除。"""

    id: int
    ts: int  # 整数微秒时间戳
    body: str
    source_conversation_id: int  # 原始会话编号，合并不改写


@dataclass(frozen=True)
class Page:
    """一页阅读结果。"""

    messages: tuple
    next_cursor: Optional[str]  # 为 None 表示本次阅读已读完


class ConversationStore:
    """会话合并与历史阅读库的主入口。"""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.commit()
            self._conn.close()
            self._conn = None

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise ChatMergeError("store is closed")
        return self._conn

    def _resolve(self, conversation_id: int) -> tuple:
        """把（可能是旧编号的）会话编号解析为 (contact_id, group_id)。"""
        conn = self._require_conn()
        row = conn.execute(
            "SELECT contact_id, group_id FROM conversation WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        if row is None:
            raise UnknownConversationError(f"unknown conversation id: {conversation_id}")
        return row[0], row[1]

    def __enter__(self) -> "ConversationStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def create_conversation(self, contact_id: int) -> int:
        """创建会话，返回新会话编号。"""
        conn = self._require_conn()
        with conn:
            cur = conn.execute(
                "INSERT INTO conv_group (contact_id) VALUES (?)", (contact_id,)
            )
            group_id = cur.lastrowid
            cur = conn.execute(
                "INSERT INTO conversation (contact_id, group_id) VALUES (?, ?)",
                (contact_id, group_id),
            )
        return cur.lastrowid

    def append_message(
        self,
        conversation_id: int,
        ts: int,
        body: str,
        message_id: Optional[int] = None,
    ) -> int:
        """向会话（可为旧编号）追加消息，返回消息编号。"""
        conn = self._require_conn()
        self._resolve(conversation_id)  # 确认会话存在；旧编号同样有效
        with conn:
            if message_id is None:
                cur = conn.execute(
                    "INSERT INTO message (ts, body, source_conversation_id)"
                    " VALUES (?, ?, ?)",
                    (ts, body, conversation_id),
                )
            else:
                try:
                    cur = conn.execute(
                        "INSERT INTO message (id, ts, body, source_conversation_id)"
                        " VALUES (?, ?, ?, ?)",
                        (message_id, ts, body, conversation_id),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ChatMergeError(f"duplicate message id: {message_id}") from exc
        return cur.lastrowid

    def merge(self, target_conversation_id: int, source_conversation_ids: Sequence[int]) -> int:
        """把来源会话并入目标会话所在组合，返回当前目标会话编号。"""
        conn = self._require_conn()
        target_contact, target_group = self._resolve(target_conversation_id)
        with conn:
            for source_id in source_conversation_ids:
                source_contact, source_group = self._resolve(source_id)
                if source_contact != target_contact:
                    raise ContactMismatchError(
                        f"conversation {source_id} belongs to contact {source_contact},"
                        f" target belongs to contact {target_contact}"
                    )
                if source_group == target_group:
                    continue  # 已在同一组，不产生重复内容
                conn.execute(
                    "UPDATE conversation SET group_id = ? WHERE group_id = ?",
                    (target_group, source_group),
                )
                conn.execute("DELETE FROM conv_group WHERE id = ?", (source_group,))
        return target_conversation_id

    def read_page(
        self,
        conversation_id: Optional[int] = None,
        cursor: Optional[str] = None,
        page_size: int = 50,
    ) -> Page:
        """分页阅读。首次调用传会话编号，后续传上一页返回的游标。"""
        conn = self._require_conn()
        if page_size < 1:
            raise ValueError("page_size must be >= 1")
        if cursor is not None:
            session_id, offset = self._parse_cursor(cursor)
        elif conversation_id is not None:
            session_id = self._open_session(conversation_id)
            offset = 0
        else:
            raise ValueError("either conversation_id or cursor is required")

        rows = conn.execute(
            "SELECT m.id, m.ts, m.body, m.source_conversation_id"
            " FROM read_session_message r"
            " JOIN message m ON m.id = r.message_id"
            " WHERE r.session_id = ? AND r.seq >= ?"
            " ORDER BY r.seq LIMIT ?",
            (session_id, offset, page_size + 1),
        ).fetchall()
        has_more = len(rows) > page_size
        messages = tuple(
            Message(id=r[0], ts=r[1], body=r[2], source_conversation_id=r[3])
            for r in rows[:page_size]
        )
        next_cursor = (
            f"{_CURSOR_PREFIX}:{session_id}:{offset + page_size}" if has_more else None
        )
        return Page(messages=messages, next_cursor=next_cursor)

    def _open_session(self, conversation_id: int) -> int:
        """首次取页：固定该编号当前所属合并组的全部消息集合与顺序。"""
        conn = self._require_conn()
        _, group_id = self._resolve(conversation_id)
        with conn:
            cur = conn.execute(
                "INSERT INTO read_session (group_id) VALUES (?)", (group_id,)
            )
            session_id = cur.lastrowid
            ids = [
                row[0]
                for row in conn.execute(
                    "SELECT m.id FROM message m"
                    " JOIN conversation c ON c.id = m.source_conversation_id"
                    " WHERE c.group_id = ?"
                    " ORDER BY m.ts, m.id",
                    (group_id,),
                )
            ]
            conn.executemany(
                "INSERT INTO read_session_message (session_id, seq, message_id)"
                " VALUES (?, ?, ?)",
                [(session_id, seq, mid) for seq, mid in enumerate(ids)],
            )
        return session_id

    def _parse_cursor(self, cursor: str) -> tuple:
        conn = self._require_conn()
        parts = cursor.split(":")
        if len(parts) != 3 or parts[0] != _CURSOR_PREFIX:
            raise InvalidCursorError(f"unrecognized cursor: {cursor!r}")
        try:
            session_id, offset = int(parts[1]), int(parts[2])
        except ValueError as exc:
            raise InvalidCursorError(f"unrecognized cursor: {cursor!r}") from exc
        row = conn.execute(
            "SELECT 1 FROM read_session WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None or offset < 0:
            raise InvalidCursorError(f"unrecognized cursor: {cursor!r}")
        return session_id, offset
