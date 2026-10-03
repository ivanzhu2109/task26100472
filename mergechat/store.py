"""会话合并与历史阅读库（SQLite 持久化）。

数据模型：
- conversations: 每个历史会话编号一行，group_id 指向当前所属合并组的目标编号。
- messages: 消息不可变，source_conversation_id 记录写入时的原始会话编号。
- read_sessions / read_session_messages: 首次取页时固化的消息快照，
  保证后续页严格延续同一集合与顺序，且游标在重开数据库后仍有效。
"""
from __future__ import annotations

import sqlite3
from typing import Iterable, List, Optional

from .types import Message, Page

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id INTEGER PRIMARY KEY,
    contact_id INTEGER NOT NULL,
    group_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_us INTEGER NOT NULL,
    body TEXT NOT NULL,
    source_conversation_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS read_sessions (
    session_id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    created_at_us INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS read_session_messages (
    session_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    PRIMARY KEY (session_id, seq)
);
"""

_CURSOR_PREFIX = "mc1"


class ConversationStore:
    """上层客服产品直接调用的入口对象。"""

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()

    def __enter__(self) -> "ConversationStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- 会话 ---------------------------------------------------------------

    def create_conversation(self, contact_id: int) -> int:
        """创建会话，返回新会话编号。新会话自成一个合并组。"""
        cur = self._conn.execute(
            "INSERT INTO conversations (contact_id, group_id) VALUES (?, 0)",
            (contact_id,),
        )
        conversation_id = cur.lastrowid
        self._conn.execute(
            "UPDATE conversations SET group_id = ? WHERE conversation_id = ?",
            (conversation_id, conversation_id),
        )
        self._conn.commit()
        return conversation_id

    def _group_of(self, conversation_id: int) -> int:
        row = self._conn.execute(
            "SELECT group_id FROM conversations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown conversation_id: {conversation_id}")
        return row[0]

    # -- 以下方法随后逐个填充 -------------------------------------------------

    def append_message(
        self, conversation_id: int, timestamp_us: int, body: str
    ) -> int:
        """向会话（可为旧编号）追加消息，返回全局唯一消息编号。

        消息立即归属该编号当前所在的合并组；source_conversation_id
        始终记录写入时使用的编号，合并不改写。
        """
        self._group_of(conversation_id)  # 校验编号存在
        cur = self._conn.execute(
            "INSERT INTO messages (timestamp_us, body, source_conversation_id)"
            " VALUES (?, ?, ?)",
            (timestamp_us, body, conversation_id),
        )
        self._conn.commit()
        return cur.lastrowid

    def merge_conversations(
        self, target_conversation_id: int, source_conversation_ids: Iterable[int]
    ) -> int:
        """把来源会话并入目标会话，返回合并后组的当前目标编号。

        目标与来源均可为旧编号，按当前归属解析；已在同一组的输入为
        空操作；合并组可继续参与合并。
        """
        target_group = self._group_of(target_conversation_id)
        source_groups = {
            self._group_of(sid) for sid in source_conversation_ids
        }
        source_groups.discard(target_group)
        for group in source_groups:
            self._conn.execute(
                "UPDATE conversations SET group_id = ? WHERE group_id = ?",
                (target_group, group),
            )
        self._conn.commit()
        return target_group

    def read_page(
        self,
        conversation_id: int,
        cursor: Optional[str] = None,
        page_size: int = 50,
    ) -> Page:
        """分页阅读。首次取页（cursor=None）固定本次阅读的消息集合。

        快照包含该编号当时所属合并组内已存在的全部消息，按
        (timestamp_us, message_id) 升序；之后的追加与合并不影响本次
        阅读。游标持久化于数据库，重开后仍有效；同一游标重复读取
        返回相同内容。
        """
        if page_size < 1:
            raise ValueError("page_size must be >= 1")
        if cursor is None:
            session_id, offset = self._open_read_session(conversation_id)
        else:
            session_id, offset = self._parse_cursor(cursor)

        rows = self._conn.execute(
            "SELECT r.message_id, m.timestamp_us, m.body,"
            "       m.source_conversation_id"
            " FROM read_session_messages r"
            " JOIN messages m ON m.message_id = r.message_id"
            " WHERE r.session_id = ? AND r.seq >= ?"
            " ORDER BY r.seq LIMIT ?",
            (session_id, offset, page_size),
        ).fetchall()
        messages = [
            Message(
                id=row[0],
                timestamp_us=row[1],
                body=row[2],
                source_conversation_id=row[3],
            )
            for row in rows
        ]
        total = self._conn.execute(
            "SELECT COUNT(*) FROM read_session_messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
        next_offset = offset + len(messages)
        next_cursor = (
            f"{_CURSOR_PREFIX}:{session_id}:{next_offset}"
            if next_offset < total
            else None
        )
        return Page(messages=messages, cursor=next_cursor)

    def _open_read_session(self, conversation_id: int) -> tuple[int, int]:
        group = self._group_of(conversation_id)
        cur = self._conn.execute(
            "INSERT INTO read_sessions (conversation_id, created_at_us)"
            " VALUES (?, 0)",
            (conversation_id,),
        )
        session_id = cur.lastrowid
        self._conn.execute(
            "INSERT INTO read_session_messages (session_id, seq, message_id)"
            " SELECT ?, ROW_NUMBER() OVER"
            "        (ORDER BY m.timestamp_us, m.message_id) - 1, m.message_id"
            " FROM messages m"
            " JOIN conversations c"
            "   ON c.conversation_id = m.source_conversation_id"
            " WHERE c.group_id = ?",
            (session_id, group),
        )
        self._conn.commit()
        return session_id, 0

    def _parse_cursor(self, cursor: str) -> tuple[int, int]:
        parts = cursor.split(":")
        if len(parts) != 3 or parts[0] != _CURSOR_PREFIX:
            raise ValueError(f"invalid cursor: {cursor!r}")
        session_id, offset = int(parts[1]), int(parts[2])
        exists = self._conn.execute(
            "SELECT 1 FROM read_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if exists is None:
            raise ValueError(f"unknown read session in cursor: {cursor!r}")
        return session_id, offset
