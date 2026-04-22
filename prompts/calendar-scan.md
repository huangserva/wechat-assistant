# 日程扫描 — Cron Prompt（决策脑 v2）

## 任务

从微信私聊和工作群中扫描日程信息，结合用户状态判断推送优先级，创建 Apple Calendar 事件，推送到飞书。

## 执行步骤

### 1. 刷新解密 + 同步消息

直接本地执行：

```bash
cd /Users/serva/.hermes/skills/social-media/wechat-assistant/scripts

# 增量解密（WAL patch，通常 <1 秒）
# 如果退出码=2，表示密钥过期（微信重启过），发告警后终止
python3 refresh_decrypt.py --config /Users/serva/wechat-assistant/config.yaml

# 同步到 collector.db
python3 collector.py --config /Users/serva/wechat-assistant/config.yaml --sync
```

> **如果 `refresh_decrypt.py` 输出包含 "HMAC 验证失败" 或退出码为 2：**
> 发飞书告警：`⚠️ 微信密钥已过期，需要重新提取。请运行 sudo find_all_keys_macos`
> 然后**终止本次任务**，不继续后续步骤。

### 2. 感知用户状态（Layer A）

```bash
python3 -c "
import sys
sys.path.insert(0, '/Users/serva/.hermes/skills/social-media/wechat-assistant/scripts')
from state_manager import StateManager
sm = StateManager('/Users/serva/wechat-assistant/scan_state.json')
status, context = sm.infer_user_status()
print(f'USER_STATUS={status}')
print(f'USER_CONTEXT={context}')
"
```

### 3. 提取日程数据

```bash
python3 extract_calendar.py --config /Users/serva/wechat-assistant/config.yaml
```

> 输出 JSON 到 stdout，包含按对话分组的消息、`existing_events`（来自 scan_state.json）和 `scan_state_path`。

### 4. 分析 JSON 输出

从 conversations 中识别日程事件。

#### 什么算日程
- **明确的时间 + 地点/事件**（"周五下午3点开会"、"明天10点到公司"）
- **约见面 / 约饭 / 约会议**（含具体时间）
- **截止日期提醒**（"月底前交材料"）
- **航班 / 高铁 / 出行安排**

#### 什么不算日程
- 模糊的"改天聊"、"有空见"
- 过去的事件（已经发生的）
- 别人的日程（跟黄宗宁无关的）
- 纯讨论未确认的计划

#### 需要确认参与的
- 聚餐邀约、活动邀请 → 标注"待确认"
- 工作群里安排的会议 → 直接标记

#### 去重规则
- **必须**检查 `existing_events`，已存在的日程不重复添加
- 只推送 existing_events 中没有的**新发现**日程
- 已过期（已过去）的日程标记为 `expired`

#### 优先级评估（Layer B）
| 标签 | 条件 |
|------|------|
| 🔴 | 24小时内即将发生的日程 |
| 🟡 | 3天内的日程 |
| 🟢 | 3天以上的日程 |
| ⚪ | 待确认/未确认的邀约 |

如果 `USER_STATUS=sleeping`，只推送 🔴 日程。

### 5. 更新 scan_state.json

```bash
python3 -c "
import json, time
state_path = '/Users/serva/wechat-assistant/scan_state.json'
with open(state_path) as f:
    state = json.load(f)

# 更新 calendar items
# 保留 existing 中未过期的 + 新发现的
# 过期的标记 status='expired'
state['calendar']['last_scan_ts'] = int(time.time())

with open(state_path, 'w') as f:
    json.dump(state, f, ensure_ascii=False, indent=2)
"
```

### 6. 创建日历事件

对每个确认的日程，用 osascript 创建 Apple Calendar 事件：

```bash
osascript -e '
tell application "Calendar"
  tell calendar "日历"
    make new event with properties {summary:"事件标题", start date:date "2026-03-15 15:00:00", end date:date "2026-03-15 16:00:00", description:"来源：微信 - 联系人名"}
  end tell
end tell'
```

> ⚠️ **osascript 需要 macOS GUI 会话**。本机有 GUI 可直接执行。

### 7. 静默时间

**23:00 ~ 08:00 不推送飞书**。但状态照常更新。

### 8. 推送到飞书

**只在有新日程时**推送。

格式：
```
📅 **YYYY-MM-DD HH:MM 日程扫描**

🔴 **即将到来**
1. 📌 **事件标题** — 今天 15:00-16:00
   来源：联系人名 · 已添加到 Apple Calendar

🟡 **近期日程**
1. 📌 **事件标题** — 3月15日 15:00-16:00
   来源：联系人名

⏳ **待确认**
1. 🤔 **聚餐邀约** — 周六晚上
   来源：张三 · 需要你确认是否参加

📊 本周共 N 个日程 · 🔴 N 紧急
```

如果没有日程，不发消息。

### 9. 写入 assistant.db

将本次扫描的日程写入 SQLite 数据库：

```bash
python3 /Users/serva/.hermes/skills/social-media/wechat-assistant/scripts/db_writer.py --db ~/wechat-assistant/assistant.db --table calendar_events --data '[{items JSON array}]'

# 记录推送反馈
python3 /Users/serva/.hermes/skills/social-media/wechat-assistant/scripts/db_writer.py --db ~/wechat-assistant/assistant.db --table push_feedback --data '[{push_type:"calendar", content_summary:"事件标题", priority:"urgent"}]'

python3 /Users/serva/.hermes/skills/social-media/wechat-assistant/scripts/db_writer.py --db ~/wechat-assistant/assistant.db --scan-log "calendar:ok:N新日程"
```

### 10. 状态栏

每条推送消息末尾必须加上状态栏，格式：

```
---
🕐 cron: wechat-calendar-scan · 运行于 YYYY-MM-DD HH:MM · 扫描窗口 HH:MM~HH:MM · 状态: {USER_STATUS} · 结果：N新日程
```

即使没有新日程也发一条简短的状态消息：
```
📅 wechat-calendar-scan · YYYY-MM-DD HH:MM · 无新日程 · 待确认: N · 状态: {USER_STATUS}
```
