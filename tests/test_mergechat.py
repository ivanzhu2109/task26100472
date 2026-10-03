"""会话合并与历史阅读库的自动化测试。"""
import os
import tempfile
import unittest

from mergechat import ConversationStore, Message


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        os.unlink(self.db_path)
        self.store = ConversationStore(self.db_path)

    def tearDown(self):
        self.store.close()
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def reopen(self):
        self.store.close()
        self.store = ConversationStore(self.db_path)

    def read_all(self, conversation_id, cursor=None, page_size=2):
        """从游标（或新阅读）读完全部页面，返回消息列表。"""
        messages = []
        while True:
            page = self.store.read_page(
                conversation_id, cursor=cursor, page_size=page_size
            )
            messages.extend(page.messages)
            if page.cursor is None:
                return messages
            cursor = page.cursor


class MergeAcrossReadsTest(StoreTestCase):
    """旧编号反复合并，多个阅读跨越不同合并时刻。"""

    def test_reads_keep_their_own_snapshots_across_reopen(self):
        s = self.store
        a = s.create_conversation(contact_id=7)
        b = s.create_conversation(contact_id=7)
        c = s.create_conversation(contact_id=7)
        d = s.create_conversation(contact_id=7)

        # A、B 各自有消息，随后 B 并入 A。
        ma1 = s.append_message(a, 1000, "a1")
        mb1 = s.append_message(b, 2000, "b1")
        s.merge_conversations(a, [b])
        snap_r1 = [ma1, mb1]  # (1000, ma1) < (2000, mb1)

        # R1：合并发生后开始阅读旧编号 b，只取第一页。
        r1 = s.read_page(b, page_size=1)
        self.assertEqual([m.id for m in r1.messages], snap_r1[:1])
        r1_cursor = r1.cursor
        self.assertIsNotNone(r1_cursor)

        # 之后追加与新一轮合并不应影响 R1。
        ma2 = s.append_message(a, 3000, "a2")
        mc1 = s.append_message(c, 1500, "c1")
        s.merge_conversations(b, [c])  # 用旧编号 b 作目标，c 并入大组
        # 按 (timestamp_us, id) 升序：1000/ma1, 1500/mc1, 2000/mb1, 3000/ma2
        snap_r2 = [ma1, mc1, mb1, ma2]

        # R2：此刻开始阅读旧编号 c。
        r2 = s.read_page(c, page_size=3)
        r2_cursor = r2.cursor

        # 组合并组：A/B/C 组并入 D；旧编号继续可用。
        md1 = s.append_message(d, 500, "d1")
        s.merge_conversations(d, [a, b])  # a、b 已在同组，不产生重复
        self.assertEqual(s.merge_conversations(a, [c]), d)  # 全部已在 d 组

        # R1 完成剩余页：只看到首次取页时的集合。
        r1_rest = self.read_all(b, cursor=r1_cursor, page_size=1)
        self.assertEqual([m.id for m in r1.messages] + [m.id for m in r1_rest],
                         snap_r1)

        # 重开数据库后，R2 的游标仍有效，集合不受后续合并影响。
        self.reopen()
        r2_first = [m.id for m in r2.messages]
        r2_rest = self.read_all(c, cursor=r2_cursor, page_size=2)
        self.assertEqual(r2_first + [m.id for m in r2_rest], snap_r2)

        # 同一游标重复读取返回相同内容。
        again = self.store.read_page(c, cursor=r2_cursor, page_size=2)
        self.assertEqual([m.id for m in again.messages],
                         [m.id for m in r2_rest[:2]])

        # 新阅读反映最新归属与消息：旧编号 a 等价于当前目标 d。
        ma3 = self.store.append_message(a, 2500, "a3")
        fresh = self.read_all(a, page_size=3)
        self.assertEqual([m.id for m in fresh],
                         [md1, ma1, mc1, mb1, ma3, ma2])
        self.assertEqual(
            [m.id for m in self.read_all(d, page_size=4)],
            [m.id for m in fresh],
        )


class PagedReadStabilityTest(StoreTestCase):
    """分页期间穿插同时间戳与更早时间戳的追加。"""

    def test_pages_match_independent_snapshot(self):
        s = self.store
        a = s.create_conversation(contact_id=9)
        b = s.create_conversation(contact_id=9)

        # 首次取页前写入的消息：含同时间戳与乱序时间戳。
        seed = [
            (a, 1000, "a-1000-x"),
            (a, 1000, "a-1000-y"),
            (b, 500, "b-500"),
            (a, 3000, "a-3000"),
            (b, 2000, "b-2000"),
        ]
        seed_ids = [s.append_message(cid, ts, body) for cid, ts, body in seed]
        s.merge_conversations(a, [b])

        # 独立收集并排序，作为基准期望。
        expected = list(zip(seed_ids, seed))
        expected.sort(key=lambda item: (item[1][1], item[0]))

        # 开始阅读，分页期间穿插追加同时间戳与更早时间戳的消息。
        collected = []
        page = s.read_page(a, page_size=2)
        collected.extend(page.messages)
        s.append_message(a, 1000, "late-same-ts")   # 同时间戳
        s.append_message(b, 100, "late-earlier")    # 更早时间戳
        s.append_message(a, 9999, "late-later")     # 更晚时间戳

        while page.cursor is not None:
            page = s.read_page(a, cursor=page.cursor, page_size=2)
            collected.extend(page.messages)

        # 消息编号、顺序、正文、原始来源与基准完全一致。
        self.assertEqual([m.id for m in collected],
                         [item[0] for item in expected])
        self.assertEqual([m.body for m in collected],
                         [item[1][2] for item in expected])
        self.assertEqual([m.source_conversation_id for m in collected],
                         [item[1][0] for item in expected])
        self.assertEqual([m.timestamp_us for m in collected],
                         [item[1][1] for item in expected])

        # 新发起的阅读包含后来追加的消息，并按规则排序。
        fresh = self.read_all(a, page_size=3)
        fresh_ids = [m.id for m in fresh]
        self.assertEqual(len(fresh_ids), len(seed_ids) + 3)
        ordered = sorted(fresh, key=lambda m: (m.timestamp_us, m.id))
        self.assertEqual([m.id for m in ordered], fresh_ids)


if __name__ == "__main__":
    unittest.main()
