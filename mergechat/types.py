"""类型定义：消息与阅读页。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class Message:
    """一条不可变消息。

    id: 全局唯一整数编号
    timestamp_us: 整数微秒时间戳
    body: 正文
    source_conversation_id: 原始会话编号（写入时的编号，合并不改写）
    """

    id: int
    timestamp_us: int
    body: str
    source_conversation_id: int


@dataclass(frozen=True)
class Page:
    """一页阅读结果。

    messages: 本页消息，按 (timestamp_us, id) 升序
    cursor: 可继续阅读的游标；None 表示本次阅读已读完
    """

    messages: List[Message]
    cursor: Optional[str]
