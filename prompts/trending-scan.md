# 热点扫描 — Cron Prompt（每小时，今日累计）（决策脑 v2）

## 任务

从微信群聊中检测跨群热点话题，每次扫描今天 00:00 到现在的全部数据（今日累计模式），发现被多个群讨论的事件和趋势，推送到飞书。

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

### 3. 提取今日热点数据

```bash
python3 extract_trending.py --config /Users/serva/wechat-assistant/config.yaml
```

> 注意：现在是"今日累计模式"，每次从今天 00:00 扫到当前时间。越到晚上数据越多，跨群关联越明显。

**JSON 输出结构**（字段名区分大小写）：
```json
{
  "date": "2026-04-20",
  "mode": "today_cumulative",
  "scan_window": {"start_ts": 1776614400, "end_ts": 1776657941, "start_time": "2026-04-20 00:00:00", "end_time": "2026-04-20 12:05:41"},
  "total_groups": 35,
  "total_messages": 1413,
  "cross_group_topics": [
    {"keyword": "claude mythos", "groups_count": 5, "total_mentions": 18, "source_groups": ["群A", "群B"], "is_merged": false}
  ],
  "trending_urls": [
    {"url": "x.com/...", "share_count": 7, "title": "", "first_seen_group": "群名", "first_seen_time": "11:16"}
  ],
  "active_groups": [
    {"group_id": "52600447216@chatroom", "group_name": "群名", "message_count": 347, "avg_daily": 40.4}
  ],
  "high_freq_keywords": [
    {"keyword": "Claude", "count": 53, "groups": 15}
  ],
  "existing_topics": [],
  "already_done_today": true,
  "scan_state_path": "/Users/serva/wechat-assistant/scan_state.json",
  "topic_aliases": {"seed_count": 88, "learned_count": 9, "learned": {...}}
}
```

> **注意**：`cross_group_topics` 可能包含大量**碎片 bigram 噪音**（同一句话被多群转发时，其所有子串都出现在跨群列表中）。例如 "我昨天给大家发了我做的效果" 在 6 群出现，则 "我昨天给"、"昨天给大"、"天给大家" 等所有子串都会出现，groups_count 相同。**过滤方法**：只保留有意义的话题关键词，忽略明显是句子碎片的条目（含常见动词/助词组合、缺乏语义完整性的片段）。

### 4. 分析 JSON 输出 — 做话题归纳，不要直接搬运 keyword

**⚠️ 身份与事实准确性（最高优先级）：**
- 消息中 sender=`__self__` 的是**用户本人（黄宗宁）**，是一个 AI Agent 爱好者/开发者，**不是**任何公司创始人
- **严禁编造人物身份**。不要把用户本人关联为任何公司/产品的创始人、高管或负责人
- 讨论某个产品 ≠ 创始人。如果看到用户在讨论 Kimi/Claude/GPT 等产品，那只是用户在讨论，不是"创始人解读"
- 描述人物时只用消息中明确出现的身份信息，不猜测不推断
- 如果某个话题只是用户转发/评论了别人的内容，如实描述为"用户分享了/讨论了"，不要加戏

**重要：keyword 字段是自动提取的 token/bigram，不是最终话题。你需要归纳。**

归纳规则：
- 相邻/相关的 keyword 可以合并成一个话题。例如 `"claude mythos"` + `"mythos逆向"` + `"架构分析"` → 话题：**Claude Mythos 架构逆向分析**
- 每个 keyword 的 groups_count 和 total_mentions 要合并计算
- 最终输出的话题应该是**具体事件/产品/玩法**，不是泛化大类

#### 什么算热点
- **具体事件**（"Claude Mythos 架构被逆向"、"LibTV Vibe 玩法"、"GPT Plus 代充技术"）
- **跨群讨论的事件**（被 3+ 个群同时讨论即可）
- **被多次转发的文章/链接**（同一 URL 被分享 3+ 次）
- **突然爆火的话题**（某个关键词在多个群同时出现）
- **行业重大新闻**（融资、收购、发布、政策等）
- **产品发布/更新**（新模型、新工具、新版本）

#### 什么是噪音（需过滤）
- 系统消息（"请升级至最新版本"等）
- 纯表情名称
- 日常用语（"工作"、"大家"、"直接"）
- 重复的红包链接
- 广告、推销、拉票
- **碎片 bigram**：同一句话被多群转发产生的子串（如 "发布了很猛" 产生 "发布了很"、"布了很猛" 等），表现为多个条目 groups_count 完全相同、关键词是较长条目的子串

### 4.5 话题归类学习（自动更新 learned_aliases.json）

分析步骤 3 输出中的 `cross_group_topics` 和 `high_freq_keywords`，识别**应该合并但尚未合并的关键词**。

规则：
- 同一个产品/概念的不同叫法（如 "dify" → "Dify", "comfyui" → "ComfyUI"）
- 新出现的 AI 工具/模型名需要归到已有父话题或创建新话题
- **人名/用户名/群昵称**（如 heimuking, gavin8800, liur, 白水, 小零件）标记为 `"__IGNORE__"`，这样它们会被完全过滤
- 不要重复种子映射中已有的 alias（`topic_aliases.seed_count` 显示已有数量）

操作：如果发现需要合并的新 alias，写一个 JSON 文件到 `/tmp/new_aliases.json`：

```json
{"aliases": {"dify": "Dify", "comfyui": "ComfyUI", "suno": "AI音乐", "heimuking": "__IGNORE__", "liur": "__IGNORE__"}}
```

然后执行：
```bash
python3 extract_trending.py --config /Users/serva/wechat-assistant/config.yaml --update-aliases /tmp/new_aliases.json
```

> **不需要每次都学习**。只有在发现明显应该合并的关键词时才操作。如果没有新发现，跳过此步骤。

### 5. 静默时间

**23:00 ~ 08:00 不推送飞书**。但 state 照常更新。

### 6. 推送到飞书（仅在有新热点时）

格式：
```
🔥 **HH:MM 热点速报**

---

### 🌐 跨群热议 Top 5

1. **归纳后的话题标题** — X 个群在讨论，共 Y 次提及
   > 涉及群：群A、群B、群C...
   > 简要概括话题内容（1-2 句）

2. ...

---

### 🔗 热门分享

- **[文章标题](URL)** — 被分享 N 次，来源：群名
- **[文章标题](URL)** — 被分享 N 次，来源：群名

---

### 📊 本轮数据

- 活跃群：X 个 | 总消息：Y 条
- 最活跃群：群名（N 条消息）
- 跨群话题：X 个 | 热门链接：Y 条
```

如果没有有价值的跨群热点（被 5+ 个群讨论的话题 < 3 个），不发消息。

改为：即使没有达到阈值的热点，也发一条简短状态消息：
```
🔥 wechat-trending-scan · YYYY-MM-DD HH:MM · 今日累计 00:00~HH:MM · 无显著热点 · 活跃群: X · 消息: Y
```

### 6.2 写入 assistant.db

将本次热点扫描结果写入 SQLite 数据库：

```bash
# 写入 trending_topics
python3 /Users/serva/.hermes/skills/social-media/wechat-assistant/scripts/db_writer.py --db ~/wechat-assistant/assistant.db --table trending_topics --data '[{items: keyword, groups_count, total_mentions, source_groups, is_merged}]'

# 如果有共享链接，写入 trending_urls
python3 /Users/serva/.hermes/skills/social-media/wechat-assistant/scripts/db_writer.py --db ~/wechat-assistant/assistant.db --table trending_urls --data '[{items: url, title, share_count, first_seen_group, first_seen_time}]'

# scan_log
python3 /Users/serva/.hermes/skills/social-media/wechat-assistant/scripts/db_writer.py --db ~/wechat-assistant/assistant.db --scan-log "trending:ok:X群Y条|top话题摘要"
```

### 6.5 状态栏

有热点时，在推送正文末尾加上状态栏：

```
---
🕐 cron: wechat-trending-scan · 运行于 YYYY-MM-DD HH:MM · 今日累计 00:00~HH:MM · 活跃群 X · 消息 Y 条 · 状态: {USER_STATUS}
```

### 7. 更新 scan_state.json

extract_trending.py 已自动更新 `trending.last_scan_ts`，无需额外操作。
