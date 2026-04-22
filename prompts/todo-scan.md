# 待办扫描 — Cron Prompt（决策脑 v2）

## 任务

从微信私聊中提取待办事项，结合用户状态推断优先级，推送到飞书。

## 执行步骤

### 1. 刷新解密 + 同步消息

直接本地执行（微信在本机）：

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

在提取待办之前，先推断用户当前状态：

```bash
python3 -c "
import sys
sys.path.insert(0, '/Users/serva/.hermes/skills/social-media/wechat-assistant/scripts')
from state_manager import StateManager
sm = StateManager('/Users/serva/wechat-assistant/scan_state.json')
status, context = sm.infer_user_status()
print(f'USER_STATUS={status}')
print(f'USER_CONTEXT={context}')
# 自动确认超过2小时的旧 todo
acked = sm.auto_ack_old_todos(hours=2)
if acked > 0:
    print(f'AUTO_ACKED={acked}')
"
```

这会输出当前用户状态：`sleeping` / `busy` / `working` / `idle`。
后续步骤据此调整推送策略。

### 2.5 检查手动状态设置

检查 user_state.json 中是否有未过期的手动状态设置：

```bash
python3 -c "
import sys, json, os
from datetime import datetime, timezone, timedelta

tz8 = timezone(timedelta(hours=8))
state_path = '/Users/serva/wechat-assistant/user_state.json'
if os.path.exists(state_path):
    with open(state_path) as f:
        us = json.load(f)
    current = us.get('current', {})
    if current.get('source') == 'user_set':
        manual_at = current.get('manual_set_at', '')
        if manual_at:
            elapsed = (datetime.now(tz=tz8) - datetime.fromisoformat(manual_at)).total_seconds() / 3600
            if elapsed < 4:
                print(f'MANUAL_STATUS={current["status"]}')
                print(f'MANUAL_CONTEXT={current.get("context", "")}')
                print(f'MANUAL_REMAINING={4 - elapsed:.1f}h')
            else:
                print('MANUAL_STATUS=expired')
        else:
            print('MANUAL_STATUS=none')
    else:
        print('MANUAL_STATUS=none')
else:
    print('MANUAL_STATUS=none')
"
```

如果输出 `MANUAL_STATUS=xxx` 且不是 expired/none：
- 用手动设置的状态覆盖 infer_user_status() 的结果
- 推送状态栏中显示 `状态: {MANUAL_STATUS} (手动设置，剩余 Xh)`

### 3. 提取私聊数据

```bash
python3 extract_todos.py --config /Users/serva/wechat-assistant/config.yaml
```

> 输出 JSON 到 stdout，包含 `conversations`、`existing_todos`（来自 scan_state.json）和 `scan_state_path`。

### 4. 轻量偏好归档

每次扫描顺便归档今天的偏好消息（不调 AI，纯关键词匹配）：

```bash
# 提取今日偏好数据，追加到按天归档文件
python3 extract_preferences.py --config /Users/serva/wechat-assistant/config.yaml > /tmp/pref_today.json
python3 -c "
import json, os, datetime
pref_dir = '/Users/serva/wechat-assistant/preferences'
os.makedirs(pref_dir, exist_ok=True)
date_str = datetime.date.today().isoformat()
path = os.path.join(pref_dir, f'{date_str}.json')

# 合并：同一天的多次归档，去重
new_data = json.load(open('/tmp/pref_today.json'))
if os.path.exists(path):
    existing = json.load(open(path))
    seen = {p['msg_time'] for p in existing.get('preferences', [])}
    for p in new_data.get('preferences', []):
        if p['msg_time'] not in seen:
            existing['preferences'].append(p)
            seen.add(p['msg_time'])
    existing['stats'] = new_data['stats']
    existing['scan_time'] = new_data['scan_time']
    with open(path, 'w') as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
else:
    with open(path, 'w') as f:
        json.dump(new_data, f, ensure_ascii=False, indent=2)
"
```

> 这步不需要推送，只是默默归档。preference-scan cron 会读这些归档文件做深度分析。

### 5. 分析 JSON 输出

从 conversations 中识别待办事项。

#### 什么算待办
- 对方**请求我做的事**（明确的 action item）
- **我承诺要做的事**（"好的我去处理"、"我来搞"）
- 涉及**金钱、合同、法律**的事项（urgent=true）
- 有**明确 deadline** 的事项（urgent=true）
- **重大事项**：即使没有明确 action，但涉及金钱交易、付款、收款、投资决定、重要约定、人事变动等，也应标记为待关注（urgent=true）
- **重要承诺**：双方达成一致的约定（不限于我单方面承诺）

#### 什么不算待办
- 纯聊天、寒暄、问好
- 已经当场解决的问题
- 咨询性质的对话（我在回答别人问题）
- 广告、推销、群发消息
- 纯表情、图片消息
- 已在 existing_todos 中且 status=done 的（不重复）

#### 去重规则
- 检查 `existing_todos` 中是否已存在相似待办（同一联系人 + 相似 summary）
- 已存在的不重复添加
- 检查是否有待办在对话中被解决（resolved）
- 对话中有"搞定了"、"已完成"、"不用了" → 标记对应 todo 为 done

### 6. 优先级排序（Layer B）

分析完成后，对每个待办进行优先级评估。考虑因素：
- **urgent 字段**：已标记 urgent 的 → 🔴 高优
- **时效性**：有明确 deadline 的 → 🔴；deadline 临近（<24h）→ 🔴🔴
- **用户当前状态**：如果 `USER_STATUS=sleeping`，所有推送降级；如果 `USER_STATUS=busy`，只推 🔴
- **存续时间**：已 open 超过 3 天且未 acknowledged → 🟡（提醒）
- **acknowledged 状态**：
  - `acknowledged=false` 的新 todo → 用 🔔 标记（未读）
  - `acknowledged=true` 的旧 todo → 正常展示（已读）

优先级标签：
| 标签 | 含义 | 条件 |
|------|------|------|
| 🔴 | 紧急 | urgent=true 或 deadline<24h |
| 🟡 | 需跟进 | 非 urgent 但需要行动 |
| 🟢 | 已确认 | acknowledged=true，等待结果 |
| ⚪ | 可延后 | 非 urgent + 无 deadline + >3天 |

### 7. 更新 scan_state.json

读取 `scan_state_path`（`/Users/serva/wechat-assistant/scan_state.json`），用 Python 脚本更新：

```bash
python3 -c "
import json, sys, time
state_path = '/Users/serva/wechat-assistant/scan_state.json'
with open(state_path) as f:
    state = json.load(f)

# 新增的待办：追加到 items，status='open', acknowledged=false
# 已解决的：标记 status='done'，加 resolved 时间戳
# last_scan_ts 更新为当前时间戳

state['todos']['last_scan_ts'] = int(time.time())

with open(state_path, 'w') as f:
    json.dump(state, f, ensure_ascii=False, indent=2)
"
```

更新规则：
- 新增的待办：`status: "open"`, `acknowledged: false`，含 `id`, `contact`, `summary`, `urgent`, `created`
- 已解决的：`status: "done"`，加 `resolved` 时间戳

### 8. 更新用户状态（Layer A 写回）

每次扫描后更新 user_state.json：

```bash
python3 -c "
import sys, json, os
sys.path.insert(0, '/Users/serva/.hermes/skills/social-media/wechat-assistant/scripts')
from state_manager import StateManager
sm = StateManager('/Users/serva/wechat-assistant/scan_state.json')
state = sm._read()
todos = state.get('todos', {}).get('items', [])
open_todos = [t for t in todos if t.get('status') == 'open']
urgent_open = [t for t in open_todos if t.get('urgent')]
sm.update_user_state({
    'current': {
        'active_todos': len(open_todos),
        'urgent_unresolved': len(urgent_open),
        'last_active': __import__('datetime').datetime.now(__import__('datetime').timezone(__import__('datetime').timedelta(hours=8))).isoformat(),
        'source': 'scan_inferred'
    }
})
print('[OK] user_state updated')
"
```

### 9. 静默时间

**23:00 ~ 08:00 不推送飞书**。但状态照常更新（state + user_state）。
- 当前时间在此范围内 → 跳过推送步骤，只更新 state

### 10. 推送到飞书

**每次都展示全部 open 待办**（包括旧的），让用户一眼看到全景。
新 todo 用 🔔 标记，已确认的用 🟢 标记。

**每次推送都必须列出全部 open 待办**，无论有无变化。

格式：
```
📋 **YYYY-MM-DD HH:MM 微信待办更新**

🔴 **紧急**
1. 🔔 **联系人** — 待办描述
2. 🟢 **联系人** — 待办描述（已确认）

🟡 **需跟进**
1. 🔔 **联系人** — 待办描述
2. 🟢 **联系人** — 待办描述（3天前创建）

🟢 **已确认·等待中**
1. **联系人** — 待办描述

✅ **本次完成**
- ~~联系人 — 待办描述~~

📊 N 新增 · N 完成 · N 待处理 · 🔔 N 未确认
```

**展示规则（重要！）：**
- **每次都列出所有 status=open 的待办**，即使 0 新增 0 完成 — 用户需要一眼看到全景
- 🔔 = acknowledged=false（新 todo，尚未被用户看到）
- 🟢 = acknowledged=true（已读，但还没完成）
- 超过 3 天的已确认待办，在描述后标注 `(X天前)`
- 如果用户回复"这个不用了"，标记为 done
- 无变化时标题改为"无变化"，但 todo 列表照列

> **手动状态设置指令**
> 用户可以通过飞书回复以下指令来设置状态（Hermes 解析后执行 set_user_status）：
> - "我在开会" / "busy" → busy
> - "我在忙" / "勿扰" → unavailable
> - "我在休息" → idle
> - "恢复" / "正常" → 恢复为自动推断

### 11. 写入 assistant.db

将本次扫描结果写入 SQLite 数据库（用于跨 cron 查询和历史追踪）：

```bash
# 写入 todos
python3 /Users/serva/.hermes/skills/social-media/wechat-assistant/scripts/db_writer.py --db ~/wechat-assistant/assistant.db --table todos --data '[{items JSON array}]'

# 写入 push_feedback（记录本次推送的每个 todo）
python3 /Users/serva/.hermes/skills/social-media/wechat-assistant/scripts/db_writer.py --db ~/wechat-assistant/assistant.db --table push_feedback --data '[{push_type:"todo", content_summary:"联系人-描述", priority:"urgent"}]'

# 写入 scan_log
python3 /Users/serva/.hermes/skills/social-media/wechat-assistant/scripts/db_writer.py --db ~/wechat-assistant/assistant.db --scan-log "todo:ok:N新增 M完成"
```

### 12. 状态栏

每条推送消息末尾必须加上状态栏（紧跟在正文最后），格式：

```
---
🕐 cron: wechat-todo-scan · 运行于 YYYY-MM-DD HH:MM · 扫描窗口 HH:MM~HH:MM · 状态: {USER_STATUS} · 结果：N新增 N完成
```

**注意：不发送"无变化"的简短心跳**。每次都发完整的待办列表+状态栏，让用户一眼看到全景。
