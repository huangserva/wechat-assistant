# 洞察分析 — Cron Prompt（每3天）

## 任务

读取最近 3 天的每日干货 digest，分析话题关联性和群画像，推送到飞书。

## 执行步骤

### 1. 收集数据

```bash
cd /Users/serva/.hermes/skills/social-media/wechat-assistant/scripts
python3 insight.py --config /Users/serva/wechat-assistant/config.yaml --days 3
```

> 输出 JSON 到 stdout，包含：
> - `digests`: 每天的干货数据（群名、条目、关键词、链接、分类）
> - `group_summary`: 每个群的条目数、活跃天数、分类分布
> - `existing_profiles`: 现有群画像
> - `existing_threads`: 现有话题线索
> - `digest_count`: digest 天数
> - 各文件路径

> **如果 `digest_count` 为 0 或 `message` 包含 "没有找到"**：直接终止，不发消息。

### 2. 检查是否需要运行

```bash
python3 -c "
import json
with open('/Users/serva/wechat-assistant/scan_state.json') as f:
    state = json.load(f)
print(state.get('insight', {}).get('last_run_date', ''))
"
```

如果 last_run_date 是今天 → 已运行过，终止。

### 3. 分析 — 话题关联

从多天的 digest 中识别跨天话题关联：

#### 关联规则
- **同一话题在不同天出现**（如 Day1 讨论 "Claude 新模型"，Day3 讨论 "Claude API 降价"，可能是同一条线索）
- **技术栈上下游关系**（如 Day1 讨论 Cursor 编辑器，Day2 讨论 AI code review，Day3 讨论 Vercel v0，都围绕 AI 开发工具）
- **因果关系**（如 Day1 讨论某政策，Day3 讨论该政策的影响）
- **同一事件的发展**（如 Day1 发布 → Day2 讨论 → Day3 实测）

#### 不算关联
- 不同领域的话题偶然在同一天出现
- 单纯的时间相近但内容无关联

### 4. 分析 — 群画像

根据每个群的讨论内容，生成/更新群画像：

#### 画像维度
- **话题标签**（如 "AI应用"、"投资理财"、"技术开发"、"行业八卦"、"副业创业"）
- **活跃度**（high/medium/low — 基于 3 天内出现的天数）
- **内容偏好**（偏讨论型 / 偏链接分享型 / 偏实战型）
- **核心话题 Top 3**（出现次数最多的主题）

#### 更新规则
- 如果 `existing_profiles` 中已有该群画像：**合并更新**，不覆盖原有标签，而是累加
- 如果是新增群：创建新画像

### 5. 更新数据文件

更新 group_profiles.json 和 topic_threads.json：

```bash
python3 -c "
import json, datetime

# 更新 group_profiles.json
profiles_path = '/Users/serva/wechat-assistant/group_profiles.json'
# ... 写入分析结果

# 更新 topic_threads.json
threads_path = '/Users/serva/wechat-assistant/topic_threads.json'
# ... 写入话题线索

# 更新 scan_state.json
state_path = '/Users/serva/wechat-assistant/scan_state.json'
with open(state_path) as f:
    state = json.load(f)
state['insight']['last_run_date'] = datetime.date.today().isoformat()
# 记录本次分析的 digest 日期
state['insight']['last_analyzed_dates'] = ['2026-04-17', '2026-04-18', '2026-04-19']  # 实际日期
state['insight']['run_count'] = state['insight'].get('run_count', 0) + 1
with open(state_path, 'w') as f:
    json.dump(state, f, ensure_ascii=False, indent=2)
"
```

### 6. 推送到飞书

格式：
```
🧠 **洞察报告（MM.DD ~ MM.DD）**

---

### 📌 话题线索

**线索标题**（跨 N 天）
> Day1: 简述第一天出现的背景
> Day2: 简述发展
> Day3: 简述最新进展
> 📊 涉及群：群A、群B · 关键词：xxx, yyy

---

**线索标题2**（跨 N 天）
> ...

---

### 🏷️ 群画像更新

**群名** — 🟢 高活跃
> 标签：AI应用 · 技术讨论 · 工具推荐
> 核心话题：Claude、Cursor、开源模型
> 内容风格：偏实战型（60%经验分享 + 30%链接 + 10%讨论）

**群名2** — 🟡 中活跃
> 标签：投资理财
> 核心话题：A股、港股打新、基金经理
> 内容风格：偏讨论型

---

📊 本期覆盖：X 天 · Y 个群 · Z 条干货 · N 条话题线索
```

如果 3 天内没有有意义的关联（所有话题都是独立的），简化报告，只展示群画像更新。

### 6.2 写入 scan_log

记录本次 insight 运行：

```bash
python3 /Users/serva/.hermes/skills/social-media/wechat-assistant/scripts/db_writer.py --db ~/wechat-assistant/assistant.db --scan-log "insight:ok:覆盖X天Y群·N话题线索·M群画像更新"
```

### 6.5 状态栏

每条推送末尾加上状态栏：

```
---
🕐 cron: wechat-insight · 运行于 YYYY-MM-DD HH:MM · 覆盖：MM.DD~MM.DD · 话题线索 N · 群画像更新 M
```
