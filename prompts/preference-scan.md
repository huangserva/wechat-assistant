# 用户偏好画像分析 — Cron Prompt（每天 1 次）

## 任务

运行 extract_preferences.py 提取最近 7 天的偏好消息，结合 AI 深度分析，更新用户画像，推送到飞书。

## 执行步骤

### 1. 读取归档数据

```bash
python3 -c "
import json, os, glob, datetime

pref_dir = '/Users/serva/wechat-assistant/preferences'
today = datetime.date.today()
files = []
for i in range(7):
    d = (today - datetime.timedelta(days=i)).isoformat()
    p = os.path.join(pref_dir, f'{d}.json')
    if os.path.exists(p):
        files.append(p)

if not files:
    print(json.dumps({'stats': {'preference_count': 0, 'writing_samples_count': 0}}))
else:
    # 合并所有天
    all_prefs = []
    total_samples = 0
    for f in sorted(files):
        with open(f) as fh:
            data = json.load(fh)
        all_prefs.extend(data.get('preferences', []))
        total_samples = max(total_samples, data.get('stats', {}).get('writing_samples_count', 0))
    print(json.dumps({
        'stats': {'preference_count': len(all_prefs), 'writing_samples_count': total_samples},
        'preferences': all_prefs,
        'files': files
    }, ensure_ascii=False))
"
```

> 输出 JSON：最近 7 天的归档偏好数据。
> **如果 preference_count 为 0**：直接终止，不发消息。

### 2. 检查是否需要运行

```bash
python3 -c "
import json
with open('/Users/serva/wechat-assistant/scan_state.json') as f:
    state = json.load(f)
print(state.get('preference', {}).get('last_run_date', ''))
"
```

如果 last_run_date 是今天 → 已运行过，终止。

### 3. 读取现有画像

```bash
python3 -c "
import json, os
path = '/Users/serva/wechat-assistant/profile/servasyy_profile.json'
if os.path.exists(path):
    with open(path) as f:
        print(json.dumps(json.load(f), ensure_ascii=False))
else:
    print('{}')
"
```

### 4. AI 深度分析

基于提取的 preferences 和 writing_samples，结合现有画像，进行以下分析：

#### 4.1 技术偏好分析
- 编程语言/框架偏好（倾向什么、回避什么）
- 工具链选择倾向（IDE、CLI、云服务）
- AI/LLM 使用偏好（模型选择、prompt 风格、Agent 工作流）
- 架构偏好（微服务 vs 单体、云 vs 本地）

#### 4.2 商业见解分析
- 对市场的判断和预测
- 商业模式偏好
- 竞争策略观点
- 成本/效率考量

#### 4.3 决策模式分析
- 决策风格（果断/谨慎/数据驱动/直觉）
- 优先级排序模式
- 风险偏好

#### 4.4 人际沟通风格
- 表达方式（直接/委婉/幽默）
- 常用句式和口头禅
- 情绪模式（在什么情境下表达什么情绪）

#### 4.5 写作风格
- 从 writing_samples 分析：
  - 平均句长
  - 用词偏好（口语化程度、专业术语密度）
  - 标点使用习惯
  - emoji 使用频率
  - 表达结构（总分/列举/叙述）

### 5. 更新画像

分析完成后，将结果合并到画像文件。**合并规则**：
- 已有的画像维度：增量更新（追加新发现，不删除旧结论）
- 新发现的维度：新增
- 矛盾的信息：保留最新的，标注时间戳
- 每个结论附带 confidence（high/medium/low）和 source_count（来自多少条消息）

```bash
python3 -c "
import json, datetime, os

profile_path = '/Users/serva/wechat-assistant/profile/servasyy_profile.json'
os.makedirs(os.path.dirname(profile_path), exist_ok=True)

# 读取现有画像（如果存在）
existing = {}
if os.path.exists(profile_path):
    with open(profile_path) as f:
        existing = json.load(f)

# 合并新分析结果（具体逻辑由 AI 决定）
# updated = { ... AI 分析结果 ... }
# with open(profile_path, 'w') as f:
#     json.dump(updated, f, ensure_ascii=False, indent=2)

# 更新 scan_state.json
state_path = '/Users/serva/wechat-assistant/scan_state.json'
with open(state_path) as f:
    state = json.load(f)
if 'preference' not in state:
    state['preference'] = {}
state['preference']['last_run_date'] = datetime.date.today().isoformat()
state['preference']['run_count'] = state['preference'].get('run_count', 0) + 1
with open(state_path, 'w') as f:
    json.dump(state, f, ensure_ascii=False, indent=2)
"
```

### 6. 推送到飞书

格式：
```
🧑 **用户画像更新（MM.DD）**

---

### 🔧 技术偏好
- **新增**：偏好描述（confidence: high, 来源: N 条消息）
- **更新**：原有偏好描述 → 新的偏好描述

### 💼 商业见解
- ...

### ⚡ 决策模式
- ...

### 💬 沟通风格
- ...

### ✍️ 写作风格
- ...

---

📊 本期分析：偏好消息 N 条 · 写作样本 M 条 · 覆盖 7 天
```

如果画像没有实质性变化（只是确认已有结论），简化报告为：
```
🧑 **用户画像确认（MM.DD）**
无新增偏好发现。现有画像 N 个维度，M 个结论保持不变。

📊 本期分析：偏好消息 N 条 · 写作样本 M 条
```

### 6.2 写入 assistant.db

将用户画像更新写入 SQLite 数据库：

```bash
python3 /Users/serva/.hermes/skills/social-media/wechat-assistant/scripts/db_writer.py --db ~/wechat-assistant/assistant.db --table profile_snapshots --data '[{每个维度: dimension, conclusions(JSON array)}]'
python3 /Users/serva/.hermes/skills/social-media/wechat-assistant/scripts/db_writer.py --db ~/wechat-assistant/assistant.db --scan-log "preference:ok:N维度·M结论"
```

### 6.5 状态栏

每条推送末尾加上状态栏：

```
---
🕐 cron: wechat-preference-scan · 运行于 YYYY-MM-DD HH:MM · 偏好消息 N · 写作样本 M · 画像维度 D
```
