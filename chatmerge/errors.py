"""库内异常类型定义。"""


class ChatMergeError(Exception):
    """本库所有异常的基类。"""


class UnknownConversationError(ChatMergeError):
    """访问了不存在的会话编号。"""


class ContactMismatchError(ChatMergeError):
    """合并的会话不属于同一联系人。"""


class InvalidCursorError(ChatMergeError):
    """阅读游标无法识别。"""
