# chatmerge：会话合并与历史阅读库

供上层客服产品直接以库形式调用，无接口层；全部数据保存在 SQLite，
不依赖任何外部服务。

## 能力

- **创建会话**：`create_conversation(contact_id)`，带联系人编号，返回会话编号。
- **追加消息**：`append_message(conversation_id, ts, body, message_id=None)`。
  消息有全局唯一整数编号（默认自增，也可显式指定）、整数微秒时间戳、
  正文和原始会话编号；消息不可修改、不可删除。时间戳可以早于已有消息。
- **合并会话**：`merge(target_id, source_ids)`，把同一联系人下的多个来源会话
  并入目标所在组合。目标与来源都可用旧编号，按当前归属处理；已在同一组的
  输入幂等；合并组可继续参与合并；不同联系人之间合并会抛出
  `ContactMismatchError`。
- **分页阅读**：`read_page(conversation_id=..., page_size=...)` 首次取页，
  之后用上一页返回的 `next_cursor` 继续，直到 `next_cursor is None`。

## 语义保证

- 合并后所有历史会话编号仍可使用，访问旧编号等同于访问当前目标；
  向旧编号追加的消息立即属于当前目标，但原始来源编号不变。
- 阅读结果按 `(时间戳, 消息编号)` 升序；每条消息只出现一次。
- 首次取页即固定本次阅读的消息集合（该编号当时所属合并组的全部消息）。
  之后追加的消息（即使时间戳更早）、之后合并进来的会话，都不会进入
  本次阅读；该组之后被并入更大的组，原阅读仍只看到原集合。
- 游标可重复使用（返回相同内容），正常关闭并重新打开同一数据库后仍有效。
- 新发起的阅读总是反映当时的最新归属与消息，不受未结束的旧阅读影响。

## 快速开始

```python
from chatmerge import ConversationStore

store = ConversationStore("service.db")

c1 = store.create_conversation(contact_id=1001)
c2 = store.create_conversation(contact_id=1001)

store.append_message(c1, ts=1_700_000_000_000_000, body="你好")
store.append_message(c2, ts=1_700_000_001_000_000, body="还是上次的问题")

# 同一客户重复咨询，合并后统一处理；旧编号 c1/c2 仍然可用
store.merge(c1, [c2])

# 分页阅读（首次取页即固定本次阅读的消息集合）
page = store.read_page(conversation_id=c1, page_size=50)
while True:
    for msg in page.messages:
        print(msg.id, msg.ts, msg.body, msg.source_conversation_id)
    if page.next_cursor is None:
        break
    page = store.read_page(cursor=page.next_cursor, page_size=50)

store.close()  # 也支持 with ConversationStore(...) as store: ...
```

## 运行测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖：

- 旧编号反复参与合并、多个阅读进度跨越不同合并时刻，各次阅读保持各自
  首次取页时的消息集合，重开数据库后仍可继续阅读；
- 分页期间穿插追加同时间戳与更早时间戳的消息，每次阅读最终得到的
  消息编号、顺序、正文和原始来源，与首次取页时独立收集并排序的结果一致；
- 合并幂等、跨联系人合并拒绝、未知会话与非法游标的错误处理、
  同时间戳按编号排序、显式消息编号查重。

## 存储结构

单文件 SQLite 数据库，四张表：

- `conv_group`：合并组（含联系人编号）；
- `conversation`：会话编号到当前组的映射（合并只改这里的归属）；
- `message`：消息本体，原始会话编号一经写入不再改动；
- `read_session` / `read_session_message`：每次阅读在首次取页时固化的
  消息快照（有序），游标即「快照编号 + 偏移」，因此可重放、可跨重启。
