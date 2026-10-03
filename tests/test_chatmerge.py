"""chatmerge 库的自动化测试。

运行方式：python3 -m unittest discover -s tests -v
"""

import os
import tempfile
import unittest

from chatmerge import (
    ConversationStore,
    ContactMismatchError,
    InvalidCursorError,
    UnknownConversationError,
)


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self._dir, "test.db")
        self.store = ConversationStore(self.db_path)

    def tearDown(self):
        self.store.close()

    def reopen(self):
        self.store.close()
        self.store = ConversationStore(self.db_path)

    def read_all(self, conversation_id=None, cursor=None, page_size=2):
        """从指定会话或游标开始读完全部页面，返回消息列表。"""
        messages = []
        page = self.store.read_page(
            conversation_id=conversation_id, cursor=cursor, page_size=page_size
        )
        messages.extend(page.messages)
        while page.next_cursor is not None:
            page = self.store.read_page(cursor=page.next_cursor, page_size=page_size)
            messages.extend(page.messages)
        return messages


class TestMergeAcrossReadings(StoreTestCase):
    """场景一：旧编号反复参与合并，多个阅读进度跨越不同合并时刻。

    各次阅读保持各自首次取页时的消息集合，重开数据库后仍可继续阅读。
    """

    def test_readings_pinned_at_different_merge_moments(self):
        s = self.store
        c1 = s.create_conversation(contact_id=7)
        c2 = s.create_conversation(contact_id=7)
        c3 = s.create_conversation(contact_id=7)
        c4 = s.create_conversation(contact_id=7)

        m1 = s.append_message(c1, ts=1000, body="c1-a")
        m2 = s.append_message(c2, ts=2000, body="c2-a")

        # 阅读 R1：此时 c1 单独成组，只看到 m1
        r1 = s.read_page(conversation_id=c1, page_size=1)
        self.assertEqual([m.id for m in r1.messages], [m1])
        self.assertIsNone(r1.next_cursor)

        s.merge(c1, [c2])
        m3 = s.append_message(c2, ts=3000, body="c2-b")  # 向旧编号 c2 追加

        # 阅读 R2：c1、c2 已同组，看到 m1、m2、m3
        r2 = s.read_page(conversation_id=c2, page_size=2)
        self.assertEqual([m.id for m in r2.messages], [m1, m2])
        self.assertIsNotNone(r2.next_cursor)

        # 旧编号反复参与合并：c3 经旧编号并入，再把组合并进 c4
        s.merge(c2, [c3])
        m4 = s.append_message(c3, ts=500, body="c3-a")
        s.merge(c3, [c4])  # 用旧编号 c3 把整组并入 c4
        m5 = s.append_message(c4, ts=4000, body="c4-a")

        # 阅读 R3：四个会话同组，看到全部五条消息
        r3_first = s.read_page(conversation_id=c4, page_size=3)
        self.assertEqual([m.id for m in r3_first.messages], [m4, m1, m2])

        # R1 已读完，集合不变；R2 继续读，仍只看到首次取页时的三条消息
        r2_rest = self.read_all(cursor=r2.next_cursor, page_size=2)
        self.assertEqual([m.id for m in r2_rest], [m3])

        # 重开数据库后，R3 的游标仍有效，且只看到原集合的剩余部分
        self.reopen()
        s = self.store
        r3_rest = self.read_all(cursor=r3_first.next_cursor, page_size=2)
        self.assertEqual([m.id for m in r3_rest], [m3, m5])

        # 重开后重复用同一游标，返回相同内容
        again = self.store.read_page(cursor=r3_first.next_cursor, page_size=2)
        self.assertEqual([m.id for m in again.messages], [m3, m5])

        # 组被并入更大的组后，原阅读仍只看到原集合
        c5 = s.create_conversation(contact_id=7)
        m6 = s.append_message(c5, ts=6000, body="c5-a")
        s.merge(c1, [c5])  # 旧编号 c1 代表整组参与合并
        r3_again = self.read_all(cursor=r3_first.next_cursor, page_size=1)
        self.assertEqual([m.id for m in r3_again], [m3, m5])

        # 新发起的阅读反映最新归属与消息，不受旧阅读影响
        fresh = self.read_all(conversation_id=c3, page_size=2)
        self.assertEqual([m.id for m in fresh], [m4, m1, m2, m3, m5, m6])
        self.assertEqual(
            [m.source_conversation_id for m in fresh], [c3, c1, c2, c2, c4, c5]
        )

    def test_merge_same_group_is_idempotent(self):
        s = self.store
        c1 = s.create_conversation(contact_id=1)
        c2 = s.create_conversation(contact_id=1)
        s.append_message(c1, ts=10, body="x")
        s.append_message(c2, ts=20, body="y")
        s.merge(c1, [c2])
        s.merge(c1, [c2])
        s.merge(c2, [c1, c2, c1])
        fresh = self.read_all(conversation_id=c1, page_size=10)
        self.assertEqual(len(fresh), 2)  # 每条消息只出现一次

    def test_merge_different_contacts_rejected(self):
        s = self.store
        c1 = s.create_conversation(contact_id=1)
        c2 = s.create_conversation(contact_id=2)
        with self.assertRaises(ContactMismatchError):
            s.merge(c1, [c2])

    def test_unknown_conversation_rejected(self):
        with self.assertRaises(UnknownConversationError):
            self.store.append_message(999, ts=1, body="x")
        with self.assertRaises(UnknownConversationError):
            self.store.read_page(conversation_id=999)

    def test_invalid_cursor_rejected(self):
        with self.assertRaises(InvalidCursorError):
            self.store.read_page(cursor="not-a-cursor")
        with self.assertRaises(InvalidCursorError):
            self.store.read_page(cursor="cmr1:999:0")


class TestInterleavedAppends(StoreTestCase):
    """场景二：分页期间穿插追加同时间戳和更早时间戳的消息。

    每次阅读最终得到的消息编号、顺序、正文和原始来源，
    与首次取页时独立收集并排序的结果一致。
    """

    def test_reading_isolated_from_later_appends(self):
        s = self.store
        c1 = s.create_conversation(contact_id=42)
        c2 = s.create_conversation(contact_id=42)
        c3 = s.create_conversation(contact_id=42)
        s.merge(c1, [c2])

        # 独立收集：测试侧自行记录追加的消息
        appended = []  # (id, ts, body, source_conversation_id)

        def append(conv, ts, body):
            mid = s.append_message(conv, ts=ts, body=body)
            appended.append((mid, ts, body, conv))
            return mid

        append(c1, ts=1000, body="m1")
        append(c2, ts=1000, body="m2")  # 与 m1 同时间戳
        append(c1, ts=5000, body="m3")

        # 首次取页时刻的期望集合：独立收集并按 (ts, id) 排序
        expected = sorted(appended, key=lambda r: (r[1], r[0]))

        page = s.read_page(conversation_id=c2, page_size=1)
        cursor = page.next_cursor
        collected = list(page.messages)

        # 分页期间穿插追加：同时间戳、更早时间戳、以及新合并进来的会话
        append(c1, ts=1000, body="late-same-ts")
        append(c2, ts=1, body="late-earlier-ts")
        append(c3, ts=2000, body="other-conv")
        s.merge(c1, [c3])  # 分页期间发生合并

        while cursor is not None:
            page = s.read_page(cursor=cursor, page_size=1)
            collected.extend(page.messages)
            cursor = page.next_cursor
            append(c1, ts=10, body="late-between-pages")  # 页间继续追加

        self.assertEqual([m.id for m in collected], [r[0] for r in expected])
        self.assertEqual([m.body for m in collected], [r[2] for r in expected])
        self.assertEqual(
            [m.source_conversation_id for m in collected], [r[3] for r in expected]
        )
        self.assertEqual([m.ts for m in collected], [r[1] for r in expected])

        # 新阅读则包含全部消息，按 (ts, id) 升序
        all_expected = sorted(appended, key=lambda r: (r[1], r[0]))
        fresh = self.read_all(conversation_id=c1, page_size=3)
        self.assertEqual([m.id for m in fresh], [r[0] for r in all_expected])

    def test_same_timestamp_ordering_by_id(self):
        s = self.store
        c1 = s.create_conversation(contact_id=5)
        ids = [s.append_message(c1, ts=777, body=f"b{i}") for i in range(5)]
        fresh = self.read_all(conversation_id=c1, page_size=2)
        self.assertEqual([m.id for m in fresh], ids)

    def test_explicit_message_ids_and_duplicate_rejected(self):
        s = self.store
        c1 = s.create_conversation(contact_id=5)
        s.append_message(c1, ts=1, body="a", message_id=100)
        with self.assertRaises(Exception):
            s.append_message(c1, ts=2, body="b", message_id=100)
        # 显式编号不影响后续自增编号的唯一性
        mid = s.append_message(c1, ts=3, body="c")
        self.assertNotEqual(mid, 100)


if __name__ == "__main__":
    unittest.main()
