# 待办扫描 — Cron Prompt

## 任务

从微信私聊中提取待办事项，有变化则推送到飞书。

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

### 2. 提取私聊数据

```bash
python3 extract_todos.py --config /Users/serva/wechat-assistant/config.yaml
```

> 输出 JSON 到 stdout，包含 `conversations`、`existing_todos`（来自 scan_state.json）和 `scan_state_path`。

### 3. 轻量偏好归档

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
    # 用 msg_time 去重
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

### 4. 分析 JSON 输出

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

### 5. 更新 scan_state.json

读取 `scan_state_path`（`/Users/serva/wechat-assistant/scan_state.json`），用 Python 脚本更新：

```bash
python3 -c "
import json, sys, time
state_path = '/Users/serva/wechat-assistant/scan_state.json'
with open(state_path) as f:
    state = json.load(f)

# 读取、更新、写回
# 新增的待办：追加到 items，status='open'
# 已解决的：标记 status='done'，加 resolved 时间戳
# last_scan_ts 更新为当前时间戳

state['todos']['last_scan_ts'] = int(time.time())

with open(state_path, 'w') as f:
    json.dump(state, f, ensure_ascii=False, indent=2)
"
```

更新规则：
- 新增的待办：`status: "open"`，含 `id`, `contact`, `summary`, `urgent`, `created`
- 已解决的：`status: "done"`，加 `resolved` 时间戳

### 6. 静默时间

**23:00 ~ 08:00 不推送飞书**。但状态照常更新。
- 当前时间在此范围内 → 跳过推送步骤，只更新 state

### 7. 推送到飞书

**只在有变化时**（新增或完成）推送：

格式：
```
📋 **YYYY-MM-DD HH:MM 微信待办更新**

🔴 **紧急**
1. **联系人** — 待办描述

🟡 **需跟进**
1. **联系人** — 待办描述（创建日期）

✅ **已完成**
- ~~联系人 — 待办描述~~

📊 N 新增 · N 完成 · N 待处理
```

如果没有变化，不发消息。

### 8.5 写入 assistant.db

将本次扫描结果写入 SQLite 数据库（用于跨 cron 查询和历史追踪）：

```bash
# 写入 todos
python3 /Users/serva/.hermes/skills/social-media/wechat-assistant/scripts/db_writer.py --db ~/wechat-assistant/assistant.db --table todos --data '[{items JSON array, 每个item含 id,contact,summary,status,created_ts,created_date,context}]'

# 写入 scan_log
python3 /Users/serva/.hermes/skills/social-media/wechat-assistant/scripts/db_writer.py --db ~/wechat-assistant/assistant.db --scan-log "todo:ok:N新增 M完成"
```

### 9. 状态栏

每条推送消息末尾必须加上状态栏（紧跟在正文最后），格式：

```
---
🕐 cron: wechat-todo-scan · 运行于 YYYY-MM-DD HH:MM · 扫描窗口 HH:MM~HH:MM · 结果：N新增 N完成
```

即使没有变化也发一条简短的状态消息到飞书：
```
📋 wechat-todo-scan · YYYY-MM-DD HH:MM · 无变化 · 待处理: N
```
这样用户能确认 cron 在正常运行。
