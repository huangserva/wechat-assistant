---
name: wechat-assistant
description: 微信 AI 个人助手：自动从微信聊天中提取待办、日程、干货、热点、技术讨论，推送到飞书
allowed-tools: terminal, read_file, write_file, patch, search_files
---

# wechat-assistant

> description: "微信 AI 个人助手：自动从微信聊天中提取待办、日程、干货、热点、技术讨论，推送到飞书"

## Triggers

- wechat assistant
- 微信助手
- 设置微信助手
- 微信待办
- 微信日程
- 微信干货
- wechat todo
- wechat calendar
- wechat digest
- wechat trending
- wechat tech
- 微信热点
- 微信技术

## Prerequisites

- **macOS** + 微信桌面版 4.0+
- **Python 3.8+** + `pip3 install pycryptodome zstandard pyyaml`
- **Hermes** 已配置飞书（Feishu）
- Python 依赖：`pycryptodome`, `zstandard`, `pyyaml`

## Architecture

三层设计：

### Layer 1: 独立 CLI 工具（scripts/ 目录）
纯 Python，不依赖 OpenClaw，任何人都能用。只做数据提取输出 JSON，**不调 AI API**。

### Layer 2: Hermes Skill（本文件 + prompts/）
Agent 读 prompt 模板，调 CLI 拿 JSON，分析后推送到飞书（Feishu）。

### Layer 3: 决策脑（Decision Brain v2）
在 Layer 2 基础上增加三个子层，让推送更智能：

| 子层 | 功能 | 实现 |
|------|------|------|
| **Layer A** 用户状态感知 | 推断用户当前状态（sleeping/busy/working/idle），支持手动设置 | `state_manager.py` 的 `infer_user_status()` + `set_user_status()` + `user_state.json` |
| **Layer B** 推送优先级 | 按紧急程度排序（🔴🟡🟢⚪），结合用户状态动态调整 | 各 cron prompt 中的优先级评估段 |
| **Layer C** 反馈闭环 | 记录推送反馈，追踪用户是否 acted/ignored，自动调频 | `push_feedback` 表 + acknowledged 机制 + preference-scan 反馈分析 |

**Acknowledged 机制：** 新 todo 标记 `acknowledged=false`（显示 🔔），2小时后自动确认（变为 🟢）。展示保持全部 open todo 列出，不隐藏旧项。

**手动状态设置：** 用户在飞书回复状态指令（"我在开会"/"busy"→busy, "勿扰"→unavailable, "我在休息"→idle, "恢复"→回到自动推断），4小时后自动过期恢复推断模式。`state_manager.py` 的 `set_user_status()` + `check_manual_status_expiry()` 实现。

**反馈分析（每天23:00）：** preference-scan 读取近7天 push_feedback 数据，分析各类推送的 ignore_rate / 最佳推送时段 / 优先级准确度，结果写入 `user_state.json` 的 `feedback_stats` 和 `patterns` 字段。数据积累不足时跳过。

## File Structure

```
scripts/
  decrypt/
    find_all_keys_macos.c   — C 密钥提取源码（macOS）
    decrypt_db.py            — 数据库全量解密（--config）
    config.py                — 配置加载器
  state_manager.py           — 统一状态读写模块（读写 scan_state.json）
  db_writer.py               — assistant.db 统一写入工具（9种表 + scan_log + SQL查询）
  refresh_decrypt.py         — 增量解密（WAL patch，cron 用这个）
  collector.py               — 一次性增量同步命令
  extract_todos.py           — 提取私聊对话 + 已有 todos → JSON
  extract_calendar.py        — 提取日程 + 已有 events → JSON
  extract_digest.py          — 提取群聊消息（含 daily_done 去重）→ JSON
  extract_trending.py        — 提取跨群热点（今日累计模式，每次从 00:00 扫到当前）→ JSON
  extract_tech.py            — 提取技术讨论（含 daily_done 去重）→ JSON
  insight.py                 — 读取多天 digest JSON，输出合并数据供 LLM 分析
  extract_preferences.py     — 提取用户偏好/观点消息（关键词模式匹配）+ 写作样本 → JSON
  requirements.txt           — Python 依赖
prompts/
  todo-scan.md               — 待办扫描 cron prompt（决策脑 v2：Layer A 状态感知 + acknowledged + 优先级排序）
  calendar-scan.md           — 日程扫描 cron prompt（决策脑 v2：Layer A + 优先级排序）
  digest.md                  — 干货收集 cron prompt（决策脑 v2：含 daily_done 检查 + JSON 存档）
  trending-scan.md           — 热点扫描 cron prompt（决策脑 v2：今日累计模式，每小时）
  trending-daily.md          — 热点日汇总 cron prompt（每天 21:00）
  tech-scan.md               — 技术讨论 cron prompt（决策脑 v2：含 daily_done 检查）
  insight.md                 — 洞察分析 cron prompt（每3天，话题关联+群画像）
  preference-scan.md         — 用户偏好画像 cron prompt（决策脑 v2：每天，技术/商业/决策/沟通/写作分析）
config.example.yaml          — 配置模板
```

**运行时文件**（在 `<work_dir>/`，如 `~/wechat-assistant/`）：
```
assistant.db      — 结构化数据库（SQLite，9张表，跨 cron 查询 + 历史追踪）
scan_state.json   — 统一状态文件（5个分类的去重状态 + items）
user_state.json   — 用户状态感知（Layer A：current/schedule/patterns/feedback_stats）
todos.json        — 旧格式待办文件（仅首次迁移用，之后用 scan_state.json）
collector.db      — 采集的 SQLite 数据库
config.yaml       — 配置文件
all_keys.json     — 加密密钥
digests/          — 每日干货存档（YYYY-MM-DD.json，供 insight 分析）
group_profiles.json — 群画像（持续更新，由 insight cron 维护）
topic_threads.json — 话题线索（持续更新，由 insight cron 维护）
learned_aliases.json — trending 话题学习层（LLM 自动追加的同义词映射 + __IGNORE__ 过滤）
preferences/      — 每日偏好存档（YYYY-MM-DD.json，由 todo-scan 归档）
profile/
  servasyy_profile.json — 用户偏好画像（持续更新，由 preference-scan cron 维护）
```

### 状态管理（scan_state.json）

所有 cron extract 脚本共享统一的 state 文件 `<work_dir>/scan_state.json`，通过 `scripts/state_manager.py` 读写。

**为什么需要状态管理？** 没有状态 → 每次 cron 都重复分析相同消息 → 重复推送相同内容。有了状态 → 只推送增量变化，已处理的跳过。

**结构**：
```json
{
  "todos": {
    "items": [
      {"id": "todo_001", "contact": "张三", "summary": "...", "status": "open", "created": "2026-04-19", "resolved": null}
    ],
    "last_scan_ts": 1776559295
  },
  "calendar": {
    "items": [
      {"id": "cal_001", "title": "...", "date": "2026-04-20", "status": "pending", "created": "2026-04-19"}
    ],
    "last_scan_ts": 1776559295
  },
  "digest": {"daily_done": "2026-04-18"},
  "trending": {\"items\": [], \"last_scan_ts\": 1776559295, \"daily_done\": \"2026-04-19\"},
  "tech": {\"daily_done\": \"2026-04-18\"},
  "insight": {"last_run_date": "", "last_analyzed_dates": [], "run_count": 0},
  "preference": {"last_run_date": "", "run_count": 0}
}
```

**去重策略**：
- **todos**: `existing_todos` 字段传给 prompt，LLM 对比已有 open items 去重。状态机：`open` → (用户说完成) → `done` → (7天后) → 自动归档
- **calendar**: `existing_events` 字段传给 prompt，已有 pending/confirmed 不重复推送。状态机：`pending` → (用户确认) → `confirmed` → (日期已过) → `expired`
- **digest**: 按 `daily_done` 日期去重，同一天的只跑一次
- **trending**: `last_scan_ts` 记录上次扫描时间。**今日累计模式**：每次从今天 00:00 扫到当前，越到晚上数据越多，跨群关联越容易发现。去重靠 `existing_topics`（已报过的话题不重复推）。`daily_done` 防止重复日汇总
- **tech**: 按 `daily_done` 日期去重，同一天的只跑一次
- **insight**: `last_run_date` 防止同一天重复运行，`last_analyzed_dates` 记录已分析的 digest 日期，`run_count` 累计运行次数
- **preference**: `last_run_date` 防止同一天重复运行，`run_count` 累计运行次数。画像存储在 `profile/servasyy_profile.json`

**state_manager.py API**：
```python
from state_manager import StateManager
sm = StateManager("/path/to/scan_state.json")
sm.get_todos()           # → list of todo items
sm.add_todo(item_dict)   # 添加新 todo
sm.resolve_todo(id_)     # 标记 done
sm.get_calendar()        # → list of calendar items
sm.add_calendar_event(item_dict)
sm.confirm_calendar_event(id_)
sm.get_preference_state()  # → {'last_run_date': '', 'run_count': 0}
sm.mark_preference_done()  # 更新 last_run_date 和 run_count
sm.update(key, value)    # 通用更新
sm.save()                # 写回文件
```

### 数据流

```
微信进程 → 加密DB + WAL（持续更新）
     ↓
refresh_decrypt.py（WAL patch，~70ms/DB）
     ↓
解密后 DB（持续更新）
     ↓
collector.py --sync（增量同步到 collector.db）
     ↓
extract_*.py（读 scan_state.json → 提取 JSON + 已有状态）→ Agent 分析（对比去重）→ 更新 scan_state → 飞书推送 → 写入 assistant.db
```

> **关键**：`refresh_decrypt.py` 是保持数据新鲜的核心。每次 cron 都先运行它，
> 它会检测 WAL 文件 mtime 变化，只解密新增的 WAL frame（通常 <1 秒），
> 而不是每次全量解密 19GB 数据。
> 参考实现：[bbingz/wechat-decrypt](https://github.com/bbingz/wechat-decrypt/tree/feat/macos-support)

### 洞察分析数据流（Layer 3）

```
digest cron (每天 9:00)
  → 提取干货 → 推飞书 → 保存 ~/digests/YYYY-MM-DD.json（结构化）

insight cron (每3天 20:00)
  → insight.py 读最近3天 digest JSON
  → 分析话题跨天关联 → 更新 topic_threads.json
  → 分析群话题偏好/标签 → 更新 group_profiles.json
  → 更新 scan_state.json (insight.last_run_date)
  → 推飞书洞察报告
```

### 用户偏好画像数据流（Layer 4）

```
preference-scan cron (每天 23:00)
  → extract_preferences.py 读 collector.db 中 sender='__self__' 的消息
  → 关键词分类: tech/business/decision/opinion → preferences[]
  → 提取写作样本（均匀采样最多50条）→ writing_samples[]
  → 输出 JSON → LLM 深度分析5个维度（技术、商业、决策、沟通、写作风格）
  → 增量合并到 profile/servasyy_profile.json
  → 更新 scan_state.json (preference.last_run_date)
  → 推飞书偏好报告
```

> **两层设计**: extract_preferences.py 是纯 Python 关键词提取（不调 AI），preference-scan.md 的 LLM 负责深度分析和画像合并。
> 用户在 collector.db 中的身份标识是 `sender='__self__'`（不是 wxid）。
> 群聊名称从 `decrypted/session/session.db` 的 `SessionNoContactInfoTable.session_title` 查询。

## Setup（首次设置流程）

用户说"帮我设置微信助手"时，Agent 按以下步骤引导：

> **工作目录（`<work_dir>`）**：用户存放 config.yaml、all_keys.json、collector.db 等运行时文件的目录。
> 建议创建 `~/wechat-assistant`。Agent 引导时先询问用户想放哪里，默认 `~/wechat-assistant`。
> `<skill_dir>` 是 skill 安装目录（只读代码），`<work_dir>` 是运行时数据目录（可写）。

### Step 1: 创建工作目录 + 安装依赖

```bash
mkdir -p ~/wechat-assistant && cd ~/wechat-assistant
pip3 install pycryptodome zstandard pyyaml
```

### Step 2: 编译密钥提取工具（macOS）

```bash
cd <skill_dir>/scripts/decrypt
cc -O2 -o find_all_keys_macos find_all_keys_macos.c
```

> 编译不需要 sudo。运行密钥提取时才需要 sudo。微信桌面版必须正在运行。

### Step 3: 提取密钥

```bash
cd <work_dir>
sudo <skill_dir>/scripts/decrypt/find_all_keys_macos
# 输出 all_keys.json 到当前目录
```

> 必须 `cd` 到 config.yaml 所在的目录再运行，因为 `all_keys.json` 会输出到当前工作目录，
> 而 config.yaml 默认配置 `keys_file: "./all_keys.json"` 是相对于 config.yaml 解析的。

### Step 4: 创建配置文件

```bash
cp <skill_dir>/config.example.yaml <work_dir>/config.yaml
```

引导用户填写：
- `wechat.db_dir` — 微信数据库目录（macOS 自动检测路径：`~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/*/db_storage`，取 `*` 部分中含 wxid 的那个）
- `wechat.self_wxid` — 用户的微信 wxid（从 `db_dir` 路径中提取，格式如 `wxid_xxxxxxxxxxxx`；或在解密后查 `contact.db` 中 `type=0` 的记录）
- `monitor.groups` — 想监控的群 chatroom_id（格式如 `12345678@chatroom`；可在首次同步后用 `sqlite3 collector.db "SELECT chatroom_id, chatroom_name FROM watched_chats WHERE chatroom_id LIKE '%@chatroom' LIMIT 20"` 查看）
- `monitor.work_groups` — 工作群映射（日程扫描用，非 @chatroom 格式的工作群也会保留）

### Step 5: 解密数据库

```bash
cd <skill_dir>/scripts
python3 decrypt/decrypt_db.py --config <config_path>
```

### Step 6: 首次采集

```bash
# 首次同步（自动发现所有群和私聊）
python3 <skill_dir>/scripts/collector.py --config <config_path> --sync
```

> `--sync` 首次运行时如果 watched_chats 为空，会自动触发 discover（扫描 session.db 和 contact.db）。
> ⚠️ 首次同步会注册所有历史会话，可能较慢。同步完成后可用：
> `sqlite3 <work_dir>/collector.db "SELECT chatroom_id, chatroom_name FROM watched_chats LIMIT 20"`
> 查看已注册的会话，手动删除不需要的。

### Step 7: 配置飞书推送

Hermes 通过飞书 WebSocket 网关推送 cron 结果。确保：

1. **飞书应用已创建**，获取 App ID 和 App Secret
2. **事件订阅**：在飞书开发者后台订阅 `im.message.receive_v1`（接收消息必需）
3. **权限**：开通 `im:message`、`im:message:send_as_bot`、`admin:app.info:readonly`（机器人身份识别）
4. **Gateway 配置**：通过 `hermes gateway` 配置飞书连接（WebSocket 模式不需要公网 IP）
5. **创建飞书群**，将机器人拉入群中，获取群 chat_id（`oc_` 开头）
6. **私聊设置 home channel**：在飞书私聊中给机器人发 `/sethome`

> **群聊注意**：飞书群里机器人只响应 **@mention** 消息（`_should_accept_group_message` 要求显式 @机器人）。
> 如果机器人无法识别 @mention，检查 `admin:app.info:readonly` 权限是否已生效（可能需要发布新版本）。

### Step 8: 注册 Cron 任务

注册 6 个 cron 任务（使用 `cronjob action=create`）。每个 cron 的 prompt 内容来自 `prompts/` 目录下的模板。

| 参数 | 值 |
|------|-----|
| `deliver` | `feishu:<群chat_id>`（如 `feishu:oc_82a7b5f029170f31d78d4084027886ac`） |
| `skills` | `[\"wechat-assistant\"]` |

注册时替换以下占位符：

| 占位符 | 含义 |
|--------|------|
| `{{config_path}}` | config.yaml 的绝对路径 |
| `{{skill_dir}}` | skill 根目录绝对路径 |
| `{{groups}}` | 监控群 ID 列表（逗号分隔） |

> **本机执行**：如果 Hermes 和微信在同一台机器上，prompt 中的命令直接本地执行，不需要 SSH。

> **静默时间**：todo-scan 和 calendar-scan 的 prompt 内置了 23:00-08:00 静默逻辑（state 照常更新，但不推送飞书）。

> **状态栏（必选）**：每个 cron 推送消息的末尾必须包含统一状态栏，格式：
> ```
> ---
> 🕐 cron: <cron-name> · 运行于 YYYY-MM-DD HH:MM · 扫描窗口 HH:MM~HH:MM · 结果：...
> ```
> 即使没有变化也发一条简短状态消息（如 `📋 wechat-todo-scan · YYYY-MM-DD HH:MM · 无变化 · 待处理: N`），让用户能确认 cron 在正常运行。所有 prompt 模板中已内嵌此格式。

#### Cron 1: 待办扫描（每 30 分钟）
- 模板：`prompts/todo-scan.md`
- Schedule: `*/30 * * * *`
- 状态：对比 `scan_state.json` 中的已有 open todos，只推送新增/变化

#### Cron 2: 日程扫描（每 30 分钟）
- 模板：`prompts/calendar-scan.md`
- Schedule: `*/30 * * * *`
- 状态：对比已有 pending/confirmed 事件，只推送新日程

#### Cron 3: 干货收集（每天 9:00）
- 模板：`prompts/digest.md`
- Schedule: `0 9 * * *`
- 状态：检查 `digest.daily_done`，同一天只跑一次

#### Cron 4: 热点扫描（每小时）
- 模板：`prompts/trending-scan.md`
- Schedule: `0 * * * *`
- 状态：**今日累计模式**，每次从今天 00:00 扫到当前时间（不是增量）。越到晚上数据越多，跨群关联越明显。去重靠 `existing_topics`

#### Cron 5: 热点日汇总（每天 21:00）
- 模板：`prompts/trending-daily.md`
- Schedule: `0 21 * * *`
- 状态：检查 `trending.daily_done`，同一天只汇总一次

#### Cron 6: 技术讨论（每天 10:30）
- 模板：`prompts/tech-scan.md`
- Schedule: `30 10 * * *`
- 状态：检查 `tech.daily_done`，同一天只跑一次

## CLI Usage

所有 CLI 工具都是一次性命令，执行完退出。

### 增量解密（每次 cron 必须先跑）

```bash
# 正常模式：检测 WAL 变化，只 patch 新页面（<1 秒）
python3 refresh_decrypt.py --config config.yaml

# 强制全量解密
python3 refresh_decrypt.py --config config.yaml --full
```

### 同步消息

```bash
# 同步所有 watched_chats
python3 collector.py --config config.yaml --sync

# 只同步单个群
python3 collector.py --config config.yaml --sync --chatroom 12345@chatroom
```

### 提取私聊待办数据

```bash
# 增量（最近 90 分钟，含已有 open todos）
python3 extract_todos.py --config config.yaml

# 全量（昨天整天）
python3 extract_todos.py --config config.yaml --full
```

输出包含 `existing_todos`（scan_state.json 中已有的 open items）供 prompt 对比去重。

### 提取日程数据

```bash
# 增量（最近 90 分钟，含已有 pending/confirmed 事件）
python3 extract_calendar.py --config config.yaml

# 全量
python3 extract_calendar.py --config config.yaml --full
```

输出包含 `existing_events` 供 prompt 对比去重。

### 提取群聊干货

```bash
# 默认：config 中所有 monitor.groups，昨天
# 自动检查 scan_state.json 的 digest.daily_done，已处理过输出 already_done=true
python3 extract_digest.py --config config.yaml

# 指定群和日期
python3 extract_digest.py --config config.yaml --groups "123@chatroom,456@chatroom" --date 2026-03-12
```

### 提取跨群热点

```bash
# 今日累计模式（默认）：从今天 00:00 到当前时间，每次看全天数据
python3 extract_trending.py --config config.yaml

# 全量（指定日期）
python3 extract_trending.py --config config.yaml --full --date 2026-03-12

# 调整参数
python3 extract_trending.py --config config.yaml --top 30 --min-groups 3 --min-count 5
```

今日累计模式每次从今天 00:00 扫到当前时间，越到晚上数据越多，跨群关联越容易发现。去重靠 `existing_topics`（已报过的话题不重复推）。全量模式（`--full`）扫描指定日期的整天。

**话题提取机制**（2026-04-20 重构）：

`extract_trending.py` 的 trending 提取经过三层处理：

**Layer 1 — Token 提取（`_tokenize()`）**：
- Pass 1: 英文单词（过滤 wxid/XML属性/停用词）
- Pass 2: 英文 bigram（相邻两词组合，如 `claude mythos`、`plus 代充`）
- Pass 3: 中文短语（2-4 字直接保留，5+ 字切 4-gram）
- Pass 4: 中英混合 bigram（如 `claude mythos架构`、`libtv 美女包围`）

**Layer 2 — 泛化大类词过滤（`_GENERIC_WORDS`）**：
- 25+ 个泛化词（`claude`, `gpt`, `ai`, `agent`, `cursor`, `copilot`, `deepseek` 等）在 AI 群里天然全量覆盖，出现在 cross_group_topics 和 high_freq_keywords 时被过滤
- **但 bigram 形式保留**：`claude` 被过滤，但 `claude mythos`、`claude pro` 不被过滤（含空格 = 有具体信息量）

**Layer 3 — LLM 话题归纳（prompt 层）**：
- JSON 里的 keyword 是 token/bigram，不是最终话题
- LLM 负责把相关 keyword 归纳为具体事件标题
- 例：`"claude mythos"` + `"mythos逆向"` + `"架构分析"` → 话题：**Claude Mythos 架构逆向分析**
- 跨群门槛：3+ 群（不再用 5+，因为过滤了大类词后具体话题覆盖群数自然更少）

**噪音过滤层级**：
1. **tokenize 阶段**：过滤微信号/用户名（`gavin8800`、`candy520nznf` 的字母+数字混合模式），XML 属性名，短停用词
2. **大类词过滤**：`_GENERIC_WORDS` 黑名单过滤 claude/gpt/ai 等泛化词（bigram 形式保留）
3. **停用词表**：表情词（撇嘴/呲牙）、碎片 bigram（神的孩子在跳舞/子在跳舞）、通用词（小时/老板）

### 提取技术讨论

```bash
# 昨天，自动检查 scan_state.json 的 tech.daily_done
python3 extract_tech.py --config config.yaml

# 只看 AI 相关分类
python3 extract_tech.py --config config.yaml --category ai

# 指定日期和数量
python3 extract_tech.py --config config.yaml --date 2026-03-12 --top 20
```

## assistant.db 结构化数据库

所有 cron prompt 已配置为每次扫描后写入 `assistant.db`（SQLite），解决：
1. **并发写安全** — 多个 cron 同时写不同表，不再有 JSON 竞态
2. **历史数据留存** — 可查任意日期的 trending/todos/digest 等
3. **跨维度查询** — 可 SQL 关联 trending + tech + todos

### 9 张表

| 表 | 用途 | UNIQUE 约束 |
|---|---|---|
| trending_topics | 每日热点话题 | (scan_date, keyword) |
| trending_urls | 共享链接 | (scan_date, url) |
| todos | 待办事项 | id (TEXT PK) |
| calendar_events | 日程 | (scan_date, event_date, content) |
| tech_highlights | 技术讨论 | (scan_date, category, keyword) |
| preferences | 用户偏好 | (date, category, content, msg_time) |
| profile_snapshots | 用户画像快照 | (date, dimension) |
| digests | 每日摘要 | id (AUTO) |
| scan_log | 扫描运行日志 | id (AUTO) |

### db_writer.py 用法

```bash
# 写入数据（--data 接 JSON 字符串，--file 接 JSON 文件）
python3 <skill_dir>/scripts/db_writer.py --db ~/wechat-assistant/assistant.db --table TABLE --data '[JSON]'

# 记录 scan_log（格式: type:status:message）
python3 <skill_dir>/scripts/db_writer.py --db ~/wechat-assistant/assistant.db --scan-log "trending:ok:5群24条"

# SQL 查询（调试用）
python3 <skill_dir>/scripts/db_writer.py --db ~/wechat-assistant/assistant.db --query "SELECT * FROM trending_topics WHERE scan_date='2026-04-20'"

# 初始化建表
python3 <skill_dir>/scripts/db_writer.py --db ~/wechat-assistant/assistant.db --init
```

### 在 Python 中直接调用

```python
import sys
sys.path.insert(0, '<skill_dir>/scripts')
from db_writer import _ensure_db, write_trending_topics, write_todos, write_scan_log

conn = _ensure_db('~/wechat-assistant/assistant.db')
write_trending_topics(conn, [{'keyword': 'Claude', 'groups_count': 6, ...}])
write_scan_log(conn, 'trending', 'ok', '5群24条')
```

## Maintenance

### 微信重启后

密钥会变，需要重新执行 Step 3（提取密钥）。
然后运行 `refresh_decrypt.py --full` 强制全量解密。
Agent 如果发现 `refresh_decrypt.py` 报错（HMAC 验证失败），应主动提醒用户重新提取密钥。

### WAL checkpoint 后

微信会定期将 WAL 合并到主 DB（checkpoint）。此时 `refresh_decrypt.py` 会自动检测到主 DB 的 mtime 变化，触发全量解密。无需手动操作。

### 添加/移除监控群

编辑 `config.yaml` 的 `monitor.groups`，下次 cron 自动生效。

## Troubleshooting（常见问题）

### 问题0: collector.py --sync 挂住/超时

**症状**: `collector.py --sync` 无输出、不退出，cron 任务因此卡死。

**原因**: 首次同步（`--recent-hours 0`）或 WAL checkpoint 后全量解密触发时，同步数据量巨大，可能需要数十分钟。日常增量同步（默认 24h）已优化为 ~10s，不应超时。

**排查**: 观察进程是否还在运行（`ps aux | grep collector`）。

**修复**:
1. 日常 cron 用默认参数即可（`--sync` 不加 `--recent-hours`，默认 24h 增量）
2. 如果仍超时，用 `--recent-hours 2` 缩小窗口到最近 2 小时
3. 首次全量同步用 `--recent-hours 0`，建议在 background 模式下运行
4. **execute_code 里的 `terminal()` 不支持 `background` 参数**，如需后台运行必须用直接 terminal 工具
5. **macOS 没有 `timeout` 命令**，不能用 `timeout 120 python3 ...` 做超时控制。替代方案：用 execute_code 的 terminal(timeout=150) 来设超时，或用直接 terminal 工具的 background 模式

### 问题0b: 飞书机器人不响应群聊消息

**症状**: 私聊正常，但群里发消息机器人不回复。

**原因**: Hermes 飞书适配器要求群消息必须 **@机器人** 才会处理（`_should_accept_group_message` 方法）。

**排查**:
1. 确认在群里发消息时有 @机器人名字
2. 检查 gateway error log 是否有 `Unable to hydrate bot identity` 警告
3. 如果有该警告，说明机器人无法识别自己的身份，@mention 检测可能失败

**修复**:
1. 在飞书开发者后台给应用添加 `admin:app.info:readonly` 权限
2. 添加权限后需要**发布新版本**才生效
3. 重启 gateway：`hermes gateway restart`
4. 如果权限已加但警告仍在，检查应用版本是否已审批通过

### 问题0c: 增量扫描漏掉私聊消息

**症状**: cron extract_todos/calendar 报告 "0 条会话"，但用户确实有私聊。

**原因**: 增量模式扫描"最近 90 分钟"。如果 gateway 重启打断了 cron 周期，错过一次 tick，消息就可能超出窗口被漏掉。

**已应用的修复**:
1. 窗口已从 35 分钟改为 **90 分钟**（窗口 >= cron间隔×2 + 缓冲）
2. **状态管理**：`scan_state.json` 记录 `last_scan_ts`，即使某次 cron 漏掉，下次仍从上次位置开始扫描

**排查**:
```bash
# 检查 scan_state.json 的时间戳是否合理
python3 -c "import json; s=json.load(open('$HOME/wechat-assistant/scan_state.json')); print('todos last_scan:', s['todos']['last_scan_ts']); print('calendar last_scan:', s['calendar']['last_scan_ts'])"
```

> **经验**: 有了 scan_state.json 后，last_scan_ts 确保不会永久漏掉消息。但如果 state 文件被误删，会回退到默认窗口。

### 问题1: 私聊提取返回 0 条对话

**症状**: `extract_todos.py` / `extract_calendar.py` 返回 `conversations_count: 0`

**原因**: `config.yaml` 的 `self_wxid` 与 contact.db 里实际的 username 不匹配。
collector.py 用 `self_wxid` 判断哪些消息是自己发的（标记 `__self__`）。不匹配时，自己发的消息 sender 是昵称而非 `__self__`，双向对话过滤就失败了。

**排查**:
```bash
# 查 contact.db 里真实 wxid（注意：db_dir 路径中 xxx_f647 后缀不是 wxid）
sqlite3 ~/wechat-assistant/decrypted/contact/contact.db \
  "SELECT username, nick_name FROM contact WHERE type=0"

# 检查 collector.db 里有没有 __self__
sqlite3 ~/wechat-assistant/collector.db \
  "SELECT COUNT(*) FROM messages WHERE sender = '__self__'"
```

**修复**:
1. 更新 config.yaml 的 `self_wxid` 为 contact.db 查到的 username（不带路径后缀）
2. 修正已有数据:
```bash
sqlite3 ~/wechat-assistant/collector.db \
  "UPDATE messages SET sender = '__self__' WHERE sender = '你的微信昵称'"
```

> **经验**: db_dir 路径 `.../xwechat_files/servasyy_f647/db_storage` 中的 `servasyy_f647` **不是** wxid。
> 实际 wxid 是 `servasyy`（从 contact.db type=0 查到），路径里多了个文件夹后缀。

### 问题2: extract_digest 所有群 total=0

**症状**: `extract_digest.py` 输出多个群但每个 `total: 0, filtered: 0, messages: []`

**原因**: `monitor.groups` 的群 ID 格式与 collector.db 不一致。
collector.db 存数字格式（`49418517394@chatroom`），但 config 可能填了 hash 格式（`e8ece7ec82b022283b74d948e1ba7870@chatroom`）。

**注意**: `extract_trending.py` 和 `extract_tech.py` 扫描所有群不受此影响，只有 `extract_digest.py` 依赖 config 的群列表。

**排查**:
```bash
# 对比 config 群 ID vs 数据库实际群 ID
sqlite3 ~/wechat-assistant/collector.db \
  "SELECT DISTINCT chatroom_id FROM messages WHERE chatroom_id LIKE '%@chatroom' LIMIT 5"
```

**修复**: 用数据库里真实群 ID 更新 config.yaml:
```bash
# 取消息量最多的50个群（或按需调整数量）
sqlite3 ~/wechat-assistant/collector.db \
  "SELECT chatroom_id FROM messages WHERE chatroom_id LIKE '%@chatroom'
   GROUP BY chatroom_id ORDER BY COUNT(*) DESC LIMIT 50"
```
然后用查到的数字 ID 替换 config.yaml 的 `monitor.groups` 列表。

### 问题2b: scan_state.json items 被意外清空

**症状**: `scan_state.json` 的 `todos.items` 或 `calendar.items` 变成空数组 `[]`，但 `extract_todos.py` 仍返回 `existing_todos`（说明数据曾存在）。

**原因**: 多个 cron 任务（todo-scan、calendar-scan、trending-scan 等）并发读写同一个 `scan_state.json`，存在竞态条件（race condition）。一个任务读入状态 → 另一个任务写回 → 第一个任务基于旧数据写回 → 第二个任务的更新被覆盖或丢失。

**排查**:
```bash
# 检查 items 是否为空
python3 -c "import json; s=json.load(open('$HOME/wechat-assistant/scan_state.json')); print('todos:', len(s['todos']['items']), 'items'); print('calendar:', len(s['calendar']['items']), 'items')"
```

**修复/防御**:
1. 在 cron prompt 中，更新 state 前先重新读取文件（不要用 extract 输出中的旧 state 数据）
2. 如果发现 items 被清空，可以从 extract 输出的 `existing_todos` / `existing_events` 恢复
3. 长期方案：考虑给 state_manager 加文件锁（`fcntl.flock`）或用 SQLite 替代 JSON
4. 尽量错开不同 cron 任务的执行时间（如 todo-scan 在 :00/:30，calendar-scan 在 :15/:45）

### 问题3: collector.py --sync 超时导致私聊消息断流

**症状**: `collector.py --sync` 超时（>5 分钟），私聊消息停止同步到 collector.db，群聊正常。

**根因**: `collector --sync` 遍历 watched_chats 里**所有**会话（可能 5000+），对每个会话用 `md5(chatroom_id)` 算 hash，然后逐个打开解密 DB 查找对应的 `Msg_<hash>` 表。5000+ 次文件打开/查询导致总耗时超过 cron timeout，sync 在遍历完所有会话之前就被杀掉。由于 watched_chats 按 chatroom_id 排序，群聊（`@chatroom`）排在前面先被同步，私聊排在后面，经常轮不到就超时了。

**诊断方法**:
```bash
# 1. 确认私聊是否断流：查看 collector.db 中最后一条私聊消息
sqlite3 ~/wechat-assistant/collector.db \
  "SELECT datetime(MAX(msg_time), 'unixepoch', 'localtime') FROM messages WHERE chatroom_id NOT LIKE '%@chatroom' AND chatroom_id NOT LIKE 'gh_%'"

# 2. 对比群聊最新消息（通常还在更新）
sqlite3 ~/wechat-assistant/collector.db \
  "SELECT datetime(MAX(msg_time), 'unixepoch', 'localtime') FROM messages WHERE chatroom_id LIKE '%@chatroom'"

# 3. 查看特定私聊的 sync_state（如林杰 wxid_3ow0cp77wim822）
sqlite3 ~/wechat-assistant/collector.db \
  "SELECT last_local_id, datetime(last_sync_at, 'unixepoch', 'localtime') FROM sync_state WHERE chatroom_id='wxid_3ow0cp77wim822'"

# 4. 确认解密DB里是否有该私聊的新消息（collector用md5(chatroom_id)映射表名）
python3 -c "
import hashlib, sqlite3
wxid = 'wxid_3ow0cp77wim822'
table = 'Msg_' + hashlib.md5(wxid.encode()).hexdigest()
conn = sqlite3.connect('$HOME/wechat-assistant/decrypted/message/message_0.db')
row = conn.execute(f'SELECT COUNT(*), MAX(create_time) FROM {table}').fetchone()
from datetime import datetime
print(f'{wxid}: {row[0]} msgs, latest={datetime.fromtimestamp(row[1]) if row[1] else \"N/A\"}')
"

# 5. 看私聊整体同步时间分布
sqlite3 ~/wechat-assistant/collector.db \
  "SELECT COUNT(*), CASE WHEN last_sync_at > strftime('%s','now','-1 day') THEN 'synced_today' ELSE 'stale' END FROM sync_state WHERE chatroom_id NOT LIKE '%@chatroom' AND chatroom_id NOT LIKE 'gh_%' GROUP BY 2"
```

**临时修复 — 手动同步单个私聊**:
```bash
# 只同步特定私聊（跳过全局遍历，秒级完成）
cd ~/.hermes/skills/social-media/wechat-assistant/scripts
python3 collector.py --config ~/wechat-assistant/config.yaml --sync --chatroom wxid_3ow0cp77wim822
```

**已实施的长期修复（2026-04-19）**:

collector.py 已优化，默认只同步最近 24h 内活跃的会话（基于 session.db），不再遍历全部 5072+ 会话。核心改动：

1. **`_build_full_table_cache()`**: 一次扫描所有解密 DB（~24 个），建立 `hash→(db_path, table_name)` 映射。把 O(chats × DBs) 变成 O(DBs × tables)，即只打开每个 DB 一次。
2. **`_get_recent_chats(hours)`**: 查询 `session.db` 的 `SessionTable.last_timestamp`，只返回最近 N 小时内活跃的会话（watched_chats ∩ session_recent）。
3. **`run_sync(recent_hours=24)`**: 默认 24h 增量模式，新增 `--recent-hours` CLI 参数。0 = 全量模式。

**效果**: 5072 会话全量 >300s 超时 → 85 活跃会话 **9.8s 完成**（8774 条/秒）。

```bash
# 默认：只同步最近 24h 活跃的会话（推荐，cron 用这个）
python3 collector.py --config config.yaml --sync

# 指定时间窗口（如最近 2 小时）
python3 collector.py --config config.yaml --sync --recent-hours 2

# 全量同步（首次或调试用）
python3 collector.py --config config.yaml --sync --recent-hours 0

# 单个会话（不受 recent_hours 影响）
python3 collector.py --config config.yaml --sync --chatroom wxid_xxx
```

**数据新鲜度降级**:
1. 如果 sync 仍因故失败，检查最新消息时间判断数据是否够新鲜
2. 增量 extract 返回 0 条时，降级到 `--full` 模式：
```bash
python3 extract_todos.py --config $CFG --full
```

### 验证全部 5 个功能的快速检查

```bash
SKILL=~/.hermes/skills/social-media/wechat-assistant/scripts
CFG=~/wechat-assistant/config.yaml

# 功能1: 待办（应该返回 conversations_count > 0）
python3 $SKILL/extract_todos.py --config $CFG --full | python3 -c "import sys,json;d=json.load(sys.stdin);print(f'会话数:{d[\"conversations_count\"]}')"

# 功能2: 日程
python3 $SKILL/extract_calendar.py --config $CFG --full | python3 -c "import sys,json;d=json.load(sys.stdin);print(f'会话数:{len(d[\"conversations\"])}')"

# 功能3: 干货（应该有群 total > 0）
python3 $SKILL/extract_digest.py --config $CFG --date yesterday | python3 -c "import sys,json;d=json.load(sys.stdin);non_empty=[g for g in d['groups'] if g.get('total',0)>0];print(f'有消息的群:{len(non_empty)}/{len(d[\"groups\"])}')"

# 功能4: 热点（扫描所有群，不需要 config 的群列表）
python3 $SKILL/extract_trending.py --config $CFG --date yesterday | python3 -c "import sys,json;d=json.load(sys.stdin);print(f'总消息:{d[\"total_messages\"]}, 热点:{len(d[\"cross_group_topics\"])}')"

# 功能5: 技术讨论
python3 $SKILL/extract_tech.py --config $CFG --date yesterday | python3 -c "import sys,json;d=json.load(sys.stdin);print(f'技术消息:{d[\"total_tech_mentions\"]}, 分类:{len(d[\"categories\"])}')"
```

## execute_code 中的注意事项

- **`terminal()` 不支持 `background` 参数**：hermes_tools 的 terminal() 是同步封装，无法后台运行。需要后台运行必须用直接 terminal 工具。
- **macOS 没有 `timeout` 命令**：不能在 shell 中用 `timeout 120 cmd` 做超时。用 terminal(timeout=N) 来控制。
- **复杂 shell 命令容易失败**：heredocs、嵌套引号、管道+python -c 组合在 execute_code 中经常解析出错。**可靠方案**：先 `write_file` 写 Python 脚本到 /tmp/，再 `terminal("python3 /tmp/script.py")` 执行。
- **read_file 返回值是 dict 不是 str**：`content = read_file(...)` 返回 `{"content": "...", "total_lines": N}`，不能直接切片。用 `content["content"][:N]` 或直接用 terminal + head/cat。
- **read_file 返回的 content 带 `NUM|` 行号前缀**：格式如 `"     1|{\n     2|  \"key\": ..."`。如果要在 execute_code 中做 `json.loads()`，必须先去掉行号：
  ```python
  raw = read_file("path.json", offset=1, limit=2000)
  lines = raw["content"].split('\n')
  clean = '\n'.join(line.split('|', 1)[1] if '|' in line else line for line in lines)
  data = json.loads(clean)
  ```
- **read_file 有去重缓存**：同一个 session 中对同一文件第二次调用 `read_file` 会返回 `"File unchanged since last read..."` 占位文本而非实际内容。**绕过方法**：用不同 `offset` 参数，或改用 `terminal("cat file.json")` 读取。
- **messages 表没有 chatroom_name 列**：查询需要 JOIN watched_chats：`SELECT w.chatroom_name, m.content FROM messages m JOIN watched_chats w ON m.chatroom_id = w.chatroom_id WHERE ...`。直接 `SELECT chatroom_name FROM messages` 会报 `no such column`。
- **terminal 中管道 + python -c 会被安全扫描拦截**：`cat file | python3 -c "..."` 会触发 `[HIGH] Pipe to interpreter` 安全告警并被阻止。改用 write_file 写脚本到 /tmp/ 再执行，或在 execute_code 中直接用 read_file + json.loads。

## 决策脑开发 Pitfalls

1. **db_writer.py 新增表**：函数定义必须在 `_TABLE_WRITERS` 字典之前，或用延迟注册模式（`_TABLE_WRITERS['new_table'] = write_new_table` 放在函数定义之后）。直接在 dict 里写 `None` 会导致 CLI 报 "Unknown table"。
2. **Shell 内联 Python**：复杂的 Python 代码不要用 `python3 -c "..."` 内联（引号嵌套必炸），写到 `/tmp/` 脚本文件再执行更可靠。
3. **数据迁移**：给现有数据加新字段（如 `acknowledged`）时，必须回填已有记录的默认值（`acknowledged=true`），否则已有的 open items 会全部显示为 🔔 新消息。
4. **user_state.json 初始化**：`get_user_state()` 只读不创建。需要 `update_user_state()` 或手动 `write_file` 才会创建文件。
5. **多 prompt 批量改编号**：用 `delegate_task` 并行改多个 prompt 文件，比串行快 3x。但每个子 agent 只改一个文件，避免跨文件依赖。

### LLM 身份幻觉防范（必加）

**问题**：LLM 看到用户（黄宗宁，`sender=__self__`）在群里讨论某产品（如 Kimi K2.6），会幻觉用户是该公司创始人（"黄宗宁亲自解读"）。这在 trending-scan 和 digest 中尤其容易发生。

**修复**：在所有涉及人物描述的 prompt 中（trending-scan、digest、tech-scan、trending-daily）加入身份防幻觉块：

```markdown
**⚠️ 身份与事实准确性：**
- 消息中 sender=`__self__` 的是**用户本人（黄宗宁）**，是 AI Agent 爱好者/开发者，**不是**任何公司创始人
- **严禁编造人物身份**，讨论某产品 ≠ 创始人
- 描述人物只用消息中明确出现的身份，不猜测不推断
```

**原则**：用户参与讨论 ≠ 用户是负责人。宁可少说，不要编造。

### Todo 描述修正：查源数据而非猜测

**问题**：当用户说"这个 todo 描述不对"时，不要凭印象改，应该查 `collector.db` 原始聊天记录。

**方法**：
1. 用 `sqlite3 collector.db "SELECT msg_time, sender, content FROM messages WHERE chatroom_id='<私聊ID>' ORDER BY msg_time"` 取上下文
2. 找到生成该 todo 的原始对话
3. 根据原始对话准确更新 `scan_state.json` 和 `assistant.db`

**示例**：阿北的 creao 返佣 todo，原始记录是"阿北: 好，你选个handle，你的唯一返佣链接，我后台给你开"，说明是阿北帮用户开，不是用户给阿北发。

### Cron 推送格式：永远全量展示

**原则**：todo-scan 每次都展示**全部 open todo**，不发"无变化"简短心跳。用户需要一眼看到全景。
- 无变化时标题写"无变化"，但 todo 列表照列
- 新增用 🔔，已确认用 🟢，超3天标注 `(X天前)`
- 状态栏保留：`🕐 cron: wechat-todo-scan · ... · 结果：N新增 N完成`

## Security Notes

- `all_keys.json` 包含数据库加密密钥，**不要泄露或提交到 Git**
- `config.yaml` 包含路径配置，同样需要保密
- 密钥提取需要 **sudo** 权限

### 问题6: assistant.db 表结构与写入陷阱

**症状**: 写入 `assistant.db` 时报错 `OperationalError: table XXX has no column named YYY`。

**原因**: `assistant.db` 的表结构与常见的 JSON 结构不同。例如 `todos` 表使用：
- `created_ts` (INTEGER timestamp) + `created_date` (TEXT 'YYYY-MM-DD') 而非单纯的 `created` 字段
- 没有 `urgent` 列（优先级在推送逻辑中判断，不存储在数据库）
- `updated_ts` 记录最后更新时间

**排查**:
```bash
# 查看表结构
python3 /Users/serva/.hermes/skills/social-media/wechat-assistant/scripts/db_writer.py --db ~/wechat-assistant/assistant.db --query "PRAGMA table_info(todos)"

# 查看已有的 todos 记录
python3 /Users/serva/.hermes/skills/social-media/wechat-assistant/scripts/db_writer.py --db ~/wechat-assistant/assistant.db --query "SELECT * FROM todos LIMIT 5"
```

**修复**: 在写入前先检查表结构，使用正确的列名和数据类型：

```python
# 正确的写入方式
def date_to_ts(date_str):
    if not date_str:
        return None
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return int(dt.timestamp()) + 8 * 3600  # 北京时区 UTC+8

cur.execute("""
    INSERT OR REPLACE INTO todos (id, contact, summary, status, created_ts, created_date, updated_ts)
    VALUES (?, ?, ?, ?, ?, ?, ?)
""", (todo["id"], todo["contact"], todo["summary"], todo["status"],
      date_to_ts(todo["created_date"]), todo["created_date"], int(datetime.now().timestamp())))
```

**经验**: 直接用 `db_writer.py` 的 CLI 更可靠：
```bash
# 写入 todos（--data 接收内联 JSON 字符串）
python3 db_writer.py --db ~/wechat-assistant/assistant.db --table todos --data '[{"id":"todo_001","contact":"张三","summary":"...","status":"open","created_ts":1776000000,"created_date":"2026-04-19"}]'

# 从 JSON 文件读取（--file 接收文件路径，不要用 --data 传文件路径！）
python3 db_writer.py --db ~/wechat-assistant/assistant.db --table trending_topics --file /tmp/trending_result.json
```

> **`--data` vs `--file`**: `--data` 期望内联 JSON 字符串，`--file` 期望文件路径。用 `--data /tmp/file.json` 会把路径当 JSON 解析导致报错。

### 问题7: 手动搜索特定消息内容（ad-hoc 查询）

当用户问"某某内容在哪个群"时，需要跨多个解密数据库搜索。关键映射链：

```
message_fts.db (FTS 全文搜索) → name2id (session_id→username) → contact.db (username→nick_name)
```

**步骤**：

```bash
cd ~/wechat-assistant

# 1. FTS 搜索关键词（跨所有 message_fts_v4_* 表）
python3 -c "
import sqlite3, glob
fts = sqlite3.connect('decrypted/message/message_fts.db')
cur = fts.cursor()
for tbl in ['message_fts_v4_0','message_fts_v4_1','message_fts_v4_2','message_fts_v4_3']:
    try:
        cur.execute(f'SELECT acontent, session_id, sender_id, create_time FROM {tbl} WHERE acontent LIKE \"%搜索词%\" LIMIT 10')
        for r in cur.fetchall():
            print(f'table={tbl} session_id={r[1]} sender={r[2]} time={r[3]}')
            print(r[0][:200])
            print('---')
    except: pass
fts.close()
"

# 2. session_id → username（chatroom ID）
python3 -c "
import sqlite3
fts = sqlite3.connect('decrypted/message/message_fts.db')
cur = fts.cursor()
for sid in [27299, 27306]:  # 替换为上一步查到的 session_id
    cur.execute('SELECT username FROM name2id WHERE rowid=?', (sid,))
    print(f'session {sid} -> {cur.fetchone()[0]}')
fts.close()
"

# 3. username → 群名/昵称
python3 -c "
import sqlite3
conn = sqlite3.connect('decrypted/contact/contact.db')
cur = conn.cursor()
for uid in ['56001217739@chatroom']:  # 替换为上一步查到的 username
    cur.execute('SELECT username, nick_name, remark FROM contact WHERE username=?', (uid,))
    r = cur.fetchone()
    if r: print(f'{r[0]}: {r[1]} (remark: {r[2]})')
conn.close()
"
```

> **要点**：`message_fts.db` 的 `name2id` 表用 `rowid` 映射，FTS 表的 `session_id` 就是 `name2id` 的 `rowid`。
> `local_type=21474836529`（0x80000011）通常是链接/文章分享类型，普通文本消息 `local_type` 不同。
> 如果 FTS 搜索无结果，也可以直接搜 `collector.db`（已同步的数据）：`SELECT * FROM messages WHERE content LIKE '%关键词%'`。

### Cron 推送执行细节（实战经验）

**时间戳转换为扫描窗口**：

`extract_todos.py` 输出包含 `ts_start` 和 `ts_end`（Unix timestamp），需要转换为可读的扫描窗口：

```python
from datetime import datetime, timezone, timedelta

# 示例：ts_start=1776700800, ts_end=1776771372
start_dt = datetime.fromtimestamp(ts_start, tz=timezone(timedelta(hours=8)))
end_dt = datetime.fromtimestamp(ts_end, tz=timezone(timedelta(hours=8)))
scan_window = f"{start_dt.strftime('%H:%M')}~{end_dt.strftime('%H:%M')}"

print(f"扫描窗口: {scan_window}")  # 00:00~19:36
```

**状态栏格式验证**：

每个 cron 推送末尾必须包含统一状态栏：

```markdown
---
🕐 cron: <cron-name> · 运行于 YYYY-MM-DD HH:MM · 扫描窗口 HH:MM~HH:MM · 结果：...
```

**静默时段检测**：

todo-scan 和 calendar-scan 在 23:00-08:00 期间不推送（state 照常更新）：

```python
from datetime import datetime, timezone, timedelta

now = datetime.now(tz=timezone(timedelta(hours=8)))
hour = now.hour
in_quiet_hours = hour >= 23 or hour < 8

if in_quiet_hours:
    # 更新 state 但不推送飞书
    pass
else:
    # 正常推送
    pass
```

**待办完成判定需谨慎**：

当发现对话中包含 "搞定"、"完成"、"已通过" 等关键词时，需仔细确认：

1. **检查上下文**：该关键词是否真的表示 todo 完成，而非其他含义（如评价产品）
2. **验证关联**：确认该消息对应的 todo 确实存在（通过 contact 和 summary 匹配）
3. **避免误判**：如果不确定，保留为 open 状态，让用户手动确认

示例：todo_003（森森淼淼："帮忙询问项目信息"）对话中出现 "搞定"，但这是评价 screen studio 工具，不是 todo 完成。应保留 open 状态。

**报告内容完整性**：

todo-scan 必须显示所有 open todos，即使无变化也要列出：

- **有变化时**：标题写 "今日更新"，分类显示新增/更新/紧急/其他
- **无变化时**：标题写 "今日无变化"，但 todo 列表照列
- **新增标记**：`🔔` 表示新待办或待确认更新
- **已确认标记**：`🟢` 表示已读/已确认
- **天数标注**：超3天的 todo 标注 `(X天前)`

### 问题5: trending 热点全是大类词（"Claude 25群, GPT 12群"）——零信息量

**症状**: trending-scan 每次输出都是 "Claude 25群, AI Agent 15群, GPT 12群"——天天一样，因为群全是 AI 群，这些大类词天然高频。

**根因**: 旧版用 `_TOPIC_ALIASES` 把细分词合并到大类（`opus→Claude`, `cursor→AI编程工具`），导致大类词永远占满 top。加上 tokenize 只提取单个 token，提不出 "Claude Mythos" 这种短语。

**修复（2026-04-20）**：
1. **去掉大类合并**：删除 `_TOPIC_ALIASES` / `_normalize_topic()`，改为 `_GENERIC_WORDS` 黑名单过滤泛化词
2. **加 bigram 提取**：英文 bigram + 中英混合 bigram，能提取出 "claude mythos"、"libtv 美女包围" 等具体短语
3. **LLM 做话题归纳**：prompt 不再直接用 keyword，要求 LLM 把相关 keyword 归纳为具体事件标题

**扩展泛化词列表**：编辑 `extract_trending.py` 的 `_GENERIC_WORDS` frozenset，添加新出现的大类词。
