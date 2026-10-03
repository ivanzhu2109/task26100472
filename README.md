# mergechat：会话合并与历史阅读库

面向客服产品的 Python 库，直接以类调用，无接口层；数据全部持久化在 SQLite。

## 能力

- **创建会话**：`create_conversation(contact_id)`，带联系人编号，返回会话编号。
- **追加消息**：`append_message(conversation_id, timestamp_us, body)`，返回全局唯一
  整数消息编号。消息不可修改、不可删除；`conversation_id` 可以是旧编号，消息立即
  归属当前合并组，但原始来源编号（`source_conversation_id`）始终保留写入时的编号。
- **合并会话**：`merge_conversations(target_id, source_ids)`，目标与来源都可用旧
  编号，按当前归属解析；已在同组的输入是空操作；合并组可继续参与合并。返回合并
  后组的当前目标编号。
- **分页阅读**：`read_page(conversation_id, cursor=None, page_size=50)`，返回
  `Page(messages, cursor)`。结果按 `(timestamp_us, message_id)` 升序；追加消息的
  时间戳可以早于已有消息。

## 阅读语义

- 首次取页（`cursor=None`）固定本次阅读的消息集合：该编号当时所属合并组内已存在
  的全部消息，固化为快照存入数据库。
- 后续页凭游标严格延续同一集合与顺序；之后追加的消息（即使时间戳更早）、之后
  合并进来的会话、以及本组被并入更大组，都不会影响进行中的阅读。
- 游标可重复使用（返回相同内容），正常 `close()` 后重新打开同一数据库仍有效。
- 新发起的阅读总是反映当时最新归属与消息。

## 用法

```python
from mergechat import ConversationStore

with ConversationStore("service.db") as store:
    a = store.create_conversation(contact_id=7)
    b = store.create_conversation(contact_id=7)

    store.append_message(a, 1_000_000, "你好")
    store.append_message(b, 2_000_000, "还是之前的问题")
    store.merge_conversations(a, [b])          # b 并入 a；旧编号 b 仍可用

    page = store.read_page(b, page_size=20)    # 访问旧编号等价于访问当前目标
    while page.cursor is not None:
        page = store.read_page(b, cursor=page.cursor, page_size=20)
```

`Page.messages` 中的每条 `Message` 含 `id`、`timestamp_us`、`body`、
`source_conversation_id` 四个字段。

## 运行测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖场景：

1. 旧编号反复参与合并，多个阅读进度跨越不同合并时刻，各自保持首次取页时的
   消息集合；重开数据库后凭游标继续阅读。
2. 分页期间穿插追加同时间戳与时间戳更早的消息，验证阅读得到的编号、顺序、
   正文、原始来源与首次取页时独立收集排序的结果一致。

## 环境要求

仅依赖 Python 3.9+ 标准库（`sqlite3`），无需 Docker、数据库服务或网络。
