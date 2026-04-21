# 微信助手"决策脑"升级方案

## 一句话

给微信助手加三层：**用户状态感知** → **推送优先级排序** → **反馈闭环**

不动现有架构，只加新模块。

---

## 现状问题

1. 所有消息平等对待——客户催款和朋友闲聊用同一个推送通道
2. 没有用户状态——不知道你在忙、在开会、还是在摸鱼
3. 没有反馈——推了你看没看、回没回，系统不知道，下次还一样推

## 三层升级

### Layer A: 用户状态模块 (`user_state`)

**新增文件**: `~/wechat-assistant/user_state.json`
**维护者**: todo-scan cron（每30分钟顺带更新）

```json
{
  "current": {
    "status": "working | idle | sleeping | busy | unavailable",
    "context": "在搞 youmind 文章 / 在开会 / 在路上",
    "last_active": "2026-04-21T08:43:00",
    "active_todos": 5,
    "urgent_unresolved": 1,
    "source": "inferred"
  },
  "schedule": {
    "working_hours": "09:00-23:00",
    "sleep_hours": "23:00-08:00",
    "timezone": "Asia/Shanghai"
  },
  "patterns": {
    "avg_response_time_min": 45,
    "peak_active_hours": ["09:00-12:00", "14:00-18:00", "20:00-23:00"],
    "ignore_rate_last_7d": 0.15,
    "last_updated": "2026-04-21"
  },
  "feedback_stats": {
    "total_pushed": 120,
    "total_acted": 85,
    "total_ignored": 20,
    "total_snoozed": 15,
    "by_type": {
      "todo": {"pushed": 30, "acted": 25, "ignored": 3, "snoozed": 2},
      "calendar": {"pushed": 15, "acted": 14, "ignored": 1, "snoozed": 0},
      "trending": {"pushed": 50, "acted": 30, "ignored": 12, "snoozed": 8},
      "digest": {"pushed": 25, "acted": 16, "ignored": 4, "snoozed": 5}
    }
  }
}
```

**状态推断逻辑**（在 todo-scan prompt 中加一步）：

```
推断规则（按优先级）：
1. 如果现在在 sleep_hours 内 → status=sleeping
2. 如果 scan_state.json 有 urgent=true 且 status=open 的 todo → status=busy
3. 如果最近 30 分钟内有消息活动（collector.db 查 last hour）→ status=working/idle
4. 默认 → status=idle
```

**状态来源**：
- `source: "inferred"` — 系统自动推断
- `source: "user_set"` — 用户手动设置（通过飞书消息 "@Hermes 我在开会"）

---

### Layer B: 推送优先级排序 (`push_prioritizer`)

**改哪里**: 每个 cron prompt 的推送步骤前，加一步 LLM 排序

**流程**：
```
原来的：extract → 分析 → 直接推送
改后的：extract → 分析 → 排优先级 → 分通道推送
```

**优先级分级**：

| 级别 | 条件 | 动作 |
|------|------|------|
| 🔴 **立刻推** | urgent=true / 涉及金钱 / 今天 deadline / @你 | 立刻推飞书，标题加🔴 |
| 🟡 **正常推** | 普通待办 / 新日程 / 有价值的跨群热点 | 正常推飞书 |
| 🟢 **攒着** | 非紧急干货 / 普通技术讨论 / 弱热点 | 攒到日汇总时一起推 |
| ⚪ **不推** | 纯噪音 / 已处理 / 广告 | 不推，只在状态栏计数 |

**排序 prompt 片段**（插入到各 cron prompt 的推送步骤前）：

```markdown
### 排优先级（推送前必做）

读取 ~/wechat-assistant/user_state.json 获取当前用户状态。

根据以下规则给每条待推送内容打标签：

**立刻推**：
- urgent=true 的待办
- 涉及金钱/合同/付款
- 今天截止的日程
- 群里被 @ 且有具体问题

**正常推**：
- 普通 open 待办（非 urgent）
- 新日程（非今天）
- 跨群热点（3+ 群，具体事件）
- 有深度的技术讨论

**攒着**：
- 群干货（不紧急）
- 弱热点（2-3 个群，信息量一般）
- 普通技术讨论（非突破性）

**不推**：
- 已回复/已处理
- 广告/推销
- 纯寒暄

**用户状态影响**：
- sleeping → 只推 🔴立刻推 的内容，其余攒着
- busy → 🔴立刻推 + 🟡正常推，🟢攒着
- working/idle → 全部正常推送
```

---

### Layer C: 反馈闭环 (`feedback_loop`)

**新增表**: `assistant.db` 加 `push_feedback` 表

```sql
CREATE TABLE IF NOT EXISTS push_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    push_time TEXT NOT NULL,          -- 推送时间
    push_type TEXT NOT NULL,          -- todo/calendar/trending/digest
    content_summary TEXT NOT NULL,    -- 推送内容摘要
    priority TEXT NOT NULL,           -- urgent/normal/deferred
    user_action TEXT DEFAULT NULL,    -- acted/ignored/snoozed（NULL=未判断）
    action_time TEXT DEFAULT NULL,    -- 用户行动时间
    inferred_from TEXT DEFAULT NULL   -- 推断来源（todo_resolved/feishu_read/等）
);
```

**反馈采集方式**（不需要用户主动反馈）：

1. **Todo 被 resolve** → 说明用户 act 了对应的推送 → `user_action='acted'`
2. **Calendar 被 confirm** → 说明用户看了日程推送 → `user_action='acted'`
3. **推送后 2 小时内 scan_state 无变化** → `user_action='ignored'`
4. **用户在飞书说"先别推了"/"等会"** → `user_action='snoozed'`

**反馈怎么用**：

在 preference-scan（每天23点）中加入一步：
```markdown
### 反馈分析

读取今天的 push_feedback 数据，分析：
1. 哪类推送被忽略最多 → 降低该类推送频率
2. 哪些联系人/群的推送总被忽略 → 降低优先级
3. 用户在什么时间段更容易忽略 → 调整推送时间窗口

将分析结果更新到 user_state.json 的 patterns 字段。
```

---

## 改动清单

| 改什么 | 怎么改 | 影响范围 |
|--------|--------|----------|
| `state_manager.py` | 加 `get_user_state()` / `update_user_state()` | 新增方法 |
| `db_writer.py` | 加 `push_feedback` 表 + `write_push_feedback()` | 新增表 |
| `scan_state.json` | 加 `user_state` section（或独立文件） | 新增字段 |
| `todo-scan.md` | 加状态推断 + 优先级排序步骤 | prompt 改动 |
| `calendar-scan.md` | 加优先级排序步骤 | prompt 改动 |
| `trending-scan.md` | 加优先级排序 + 弱热点攒着逻辑 | prompt 改动 |
| `digest.md` | 加优先级排序（干货默认 🟢攒着） | prompt 改动 |
| `tech-scan.md` | 加优先级排序 | prompt 改动 |
| `preference-scan.md` | 加反馈分析步骤 | prompt 改动 |
| 新增 `user_state.json` | 用户状态文件 | 新文件 |
| 新增 cron（可选） | 反馈采集 cron（每天22:30），或合入 preference-scan | 新/改 prompt |

## 实施顺序

**Phase 1**（先做，最小可用）：
1. 新增 `user_state.json` + `state_manager.py` 加读写方法
2. `db_writer.py` 加 `push_feedback` 表
3. 改 `todo-scan.md`：加状态推断 + 优先级排序
4. 改 `calendar-scan.md`：加优先级排序

**Phase 2**（一周后）：
5. 改 `trending-scan.md` / `digest.md` / `tech-scan.md`：加优先级排序
6. 改 `preference-scan.md`：加反馈分析
7. 积累一周 push_feedback 数据后调优排序规则

**Phase 3**（可选）：
8. 加用户手动设置状态功能（飞书发 "@Hermes 我在开会"）
9. 基于反馈数据自动调整各类型推送频率

---

## 不改什么

- 不改 extract_*.py 脚本（纯数据提取，不动）
- 不改 collector.py / refresh_decrypt.py（数据采集层不动）
- 不改 scan_state.json 去重逻辑（状态管理不变）
- 不加新依赖（纯 prompt + JSON + SQLite）
- 不引入外部框架（不用 SDEP/Spice）
