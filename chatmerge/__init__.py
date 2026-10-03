"""会话合并与历史阅读库。

上层客服产品直接以库的形式调用，数据保存在 SQLite。
"""

from .store import ConversationStore, Message, Page
from .errors import (
    ChatMergeError,
    UnknownConversationError,
    ContactMismatchError,
    InvalidCursorError,
)

__all__ = [
    "ConversationStore",
    "Message",
    "Page",
    "ChatMergeError",
    "UnknownConversationError",
    "ContactMismatchError",
    "InvalidCursorError",
]
