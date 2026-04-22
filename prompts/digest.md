# 干货收集 — Cron Prompt（决策脑 v2）

## 任务

从微信监控群中提炼昨天的干货内容，推送到飞书并归档到 Obsidian。


**⚠️ 身份与事实准确性：**
- 消息中 sender=`__self__` 的是**用户本人（黄宗宁）**，是 AI Agent 爱好者/开发者，**不是**任何公司创始人
- **严禁编造人物身份**，讨论某产品 ≠ 创始人
- 描述人物只用消息中明确出现的身份，不猜测不推断

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

在分析内容之前，先感知用户当前状态，以便后续输出能适应用户情境：

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

> 将 `USER_STATUS` 和 `USER_CONTEXT` 记录下来，用于状态栏展示和后续步骤的上下文感知。

### 3. 提取群聊数据

```bash
python3 extract_digest.py --config /Users/serva/wechat-assistant/config.yaml --date yesterday
```

> 输出 JSON 到 stdout，包含每个群的消息列表。
> **如果 `already_done` 为 true** — 昨天的干货已收集过，直接终止不重复推送。

### 4. 分析 JSON 输出

从每个群的消息中提炼干货。

#### 什么是干货
- **工具 / 产品推荐**（含链接或具体名称）
- **技术方案 / 经验分享**（代码、架构、方法论）
- **行业洞察 / 趋势分析**
- **有价值的资源链接**（教程、文档、开源项目）
- **实战案例 / 踩坑记录**
- **重要新闻 / 政策变化**

#### 什么是噪音
- 日常闲聊、灌水
- 广告、推销
- 重复的接龙、回复
- 纯表情、贴图
- 已被大量转发的陈旧信息
- 拉票、投票、砍价类

### 5. 推送到飞书

**只在有干货时**推送。

格式：
```
📰 **YYYY-MM-DD 微信群干货日报**

---

### 🔥 精选 Top 3

1. **标题/主题** — 一句话总结
   > 关键内容摘录（50-100字）
   📌 来源：群名 · 发送者 · HH:MM
   🔗 相关链接（如有）

2. ...

3. ...

---

### 📂 按群分组

#### 群名1（N 条有效 / M 条总计）
- 🔹 **主题** — 摘要（发送者 HH:MM）
- 🔹 **主题** — 摘要（发送者 HH:MM）

#### 群名2（N / M）
- ...

---
📊 统计：X 个群 · Y 条干货 · Z 条过滤
```

如果所有群都没有干货，发一条简短的"昨天群里没什么干货"。

### 6.5 状态栏

每条推送消息末尾必须加上状态栏，格式：

```
---
🕐 cron: wechat-digest · 运行于 YYYY-MM-DD HH:MM · 覆盖：昨天全天 · 结果：X群 Y条干货 · 状态: {USER_STATUS}
```

### 7. 标记已完成

推送成功后，更新 scan_state.json：

```bash
python3 -c "
import json
state_path = '/Users/serva/wechat-assistant/scan_state.json'
with open(state_path) as f:
    state = json.load(f)
yesterday = '<昨天的日期 YYYY-MM-DD>'
state['digest']['daily_done'] = yesterday
with open(state_path, 'w') as f:
    json.dump(state, f, ensure_ascii=False, indent=2)
"
```

### 8. Obsidian 归档

将干货内容写入 Obsidian vault（如果用户配置了 vault 路径）：

路径：`~/obsidian-vault/微信干货/YYYY-MM-DD.md`

> 如果用户没有 Obsidian 或未配置 vault 路径，跳过此步骤。

格式：同飞书推送内容（Markdown 格式），顶部加 frontmatter：

```yaml
---
date: YYYY-MM-DD
tags: [wechat-digest, auto-generated]
groups: [群名1, 群名2]
---
```

### 9. 保存结构化 JSON

将分析结果保存到本地，供后续 insight 分析使用：

```bash
python3 -c "
import json, os, datetime
digest_dir = '/Users/serva/wechat-assistant/digests'
os.makedirs(digest_dir, exist_ok=True)

yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()

# 构建 digest 数据
digest = {
    'date': yesterday,
    'groups': [
        {
            'name': '群名',
            'total_messages': 0,
            'useful_messages': 0,
            'items': [
                {
                    'topic': '主题',
                    'summary': '摘要',
                    'sender': '发送者',
                    'time': 'HH:MM',
                    'links': ['url1'],
                    'category': '工具推荐/技术方案/行业洞察/资源链接/实战案例/新闻政策'
                }
            ]
        }
    ],
    'top3': [
        {'topic': '主题', 'summary': '摘要', 'source_group': '群名', 'sender': '发送者', 'links': []}
    ],
    'stats': {'total_groups': 0, 'total_digest': 0, 'total_filtered': 0}
}

path = os.path.join(digest_dir, f'{yesterday}.json')
with open(path, 'w') as f:
    json.dump(digest, f, ensure_ascii=False, indent=2)
print(f'Saved: {path}')
"
```

### 9.5 写入 assistant.db

将昨日摘要写入 SQLite 数据库：

```bash
python3 /Users/serva/.hermes/skills/social-media/wechat-assistant/scripts/db_writer.py --db ~/wechat-assistant/assistant.db --table digests --data '[{"date":"YYYY-MM-DD","content":{digest数据}}]'
python3 /Users/serva/.hermes/skills/social-media/wechat-assistant/scripts/db_writer.py --db ~/wechat-assistant/assistant.db --scan-log "digest:ok:昨日X群Y条精华"
```

> ⚠️ 上面的 digest 数据结构是模板，必须填入步骤3实际分析出的内容，不能留空。
