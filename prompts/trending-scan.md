# 热点扫描 — Cron Prompt（增量窗口）（决策脑 v2）

## 任务

从微信群聊中检测**增量窗口内**的新热点和明显升温的话题，只推送“新出现 / 明显升温”的内容；不要把同一批热点反复重报。日汇总由 `wechat-trending-daily` 单独负责。

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

### 3. 提取增量热点数据

```bash
python3 extract_trending.py --config /Users/serva/wechat-assistant/config.yaml
```

> 默认是**增量窗口模式**：从上次 `trending.last_scan_ts` 往前回补约 20 分钟到当前时间；首次运行回看最近 6 小时。
> `existing_topics` 是**上一轮窗口**提取出的热点，用来判断“是不是已经报过”。

**JSON 输出结构**（字段名区分大小写）：
```json
{
  "date": "2026-04-20",
  "mode": "incremental",
  "scan_window": {"start_ts": 1776650400, "end_ts": 1776657941, "start_time": "2026-04-20 10:00:00", "end_time": "2026-04-20 12:05:41"},
  "total_groups": 18,
  "total_messages": 326,
  "cross_group_topics": [
    {"keyword": "claude mythos", "groups_count": 5, "total_mentions": 18, "source_groups": ["群A", "群B"], "is_merged": false}
  ],
  "trending_urls": [
    {"url": "x.com/...", "share_count": 4, "title": "", "first_seen_group": "群名", "first_seen_time": "11:16"}
  ],
  "active_groups": [
    {"group_id": "52600447216@chatroom", "group_name": "群名", "message_count": 82, "avg_daily": 31.4}
  ],
  "high_freq_keywords": [
    {"keyword": "Claude Mythos", "count": 14, "groups": 5}
  ],
  "existing_topics": [
    {"keyword": "Claude", "groups_count": 3, "total_mentions": 7}
  ],
  "already_done_today": false,
  "scan_state_path": "/Users/serva/wechat-assistant/scan_state.json",
  "topic_aliases": {"seed_count": 88, "learned_count": 9}
}
```

> **注意**：`cross_group_topics` 可能包含大量**碎片 bigram 噪音**（同一句话被多群转发时，其所有子串都出现在跨群列表中）。例如 "我昨天给大家发了我做的效果" 在 6 群出现，则 "我昨天给"、"昨天给大"、"天给大家" 等所有子串都会出现，groups_count 相同。**过滤方法**：只保留有意义的话题关键词，忽略明显是句子碎片的条目（含常见动词/助词组合、缺乏语义完整性的片段）。

### 4. 分析 JSON 输出 — 做话题归纳，不要直接搬运 keyword

**⚠️ 身份与事实准确性（最高优先级）：**
- 消息中 sender=`__self__` 的是**用户本人（黄宗宁）**，是一个 AI Agent 爱好者/开发者，**不是**任何公司创始人
- **严禁编造人物身份**。不要把用户本人关联为任何公司/产品的创始人、高管或负责人
- 讨论某个产品 ≠ 创始人。如果看到用户在讨论 Kimi/Claude/GPT 等产品，那只是用户在讨论，不是“创始人解读”
- 描述人物时只用消息中明确出现的身份信息，不猜测不推断
- 如果某个话题只是用户转发/评论了别人的内容，如实描述为“用户分享了/讨论了”，不要加戏

**重要：keyword 字段是自动提取的 token/bigram，不是最终话题。你需要先归纳，再和 `existing_topics` 对比。**

归纳规则：
- 相邻/相关的 keyword 可以合并成一个话题。例如 `"claude mythos"` + `"mythos逆向"` + `"架构分析"` → 话题：**Claude Mythos 架构逆向分析**
- 每个 keyword 的 `groups_count` 和 `total_mentions` 要合并估算
- 最终输出的话题应该是**具体事件/产品/玩法**，不是泛化大类

#### 只把以下内容视为“值得推送”
1. **新话题**：本窗口首次出现，且 `groups_count >= 3` 或 `total_mentions >= 6`
2. **明显升温**：上一轮已有，但本轮出现明显放大，例如：
   - `groups_count` 增加 >= 2
   - 或 `total_mentions` 增加 >= 5
   - 或首次进入 **5+ 群讨论**
3. **新 URL 热传**：本窗口首次进入高热，且同一 URL 被分享 >= 3 次

#### 不要重复推的情况
- 上一轮已经提过，本轮只是**小幅波动**
- 只是泛化大类词（Claude / GPT / AI Agent 这类天然高频词）
- 只是同一篇文章/同一条消息的碎片转述
- 只有 1-2 个群轻微提及，没有形成真正跨群热度

### 4.5 话题归类学习（自动更新 learned_aliases.json）

分析步骤 3 输出中的 `cross_group_topics` 和 `high_freq_keywords`，识别**应该合并但尚未合并的关键词**。

规则：
- 同一个产品/概念的不同叫法（如 `dify` → `Dify`, `comfyui` → `ComfyUI`）
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

### 6. 推送到飞书（仅在有新热点或明显升温时）

**最终推送必须使用简体中文。** 除专有名词、产品名、URL、代码标识外，不要出现英文标题、英文句子或英文小结。

格式：
```
🔥 **HH:MM 热点速报**

---

### 🌐 本轮新热点

1. **归纳后的话题标题** — X 个群在讨论，共 Y 次提及
   > 涉及群：群A、群B、群C...
   > 用 1-2 句中文说明为什么突然热起来

2. ...

### 📈 持续升温话题

- **话题标题** — 较上一轮明显升温（X 群 → Y 群 / A 次 → B 次）

### 🔗 热门分享

- **[文章标题](URL)** — 本窗口被分享 N 次，来源：群名

### 📊 本轮数据

- 增量窗口：HH:MM~HH:MM
- 活跃群：X 个 | 总消息：Y 条
- 新热点：X 个 | 升温话题：Y 个 | 热门链接：Z 条
```

如果没有达到阈值的热点，不展开正文，只发一条简短状态消息：
```
🔥 wechat-trending-scan · YYYY-MM-DD HH:MM · 增量窗口 HH:MM~HH:MM · 暂无显著新热点 · 活跃群 X 个 · 消息 Y 条
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

每条推送消息末尾都加上状态栏：

```
---
🕐 cron: wechat-trending-scan · 运行于 YYYY-MM-DD HH:MM · 增量窗口 HH:MM~HH:MM · 活跃群 X · 消息 Y 条 · 状态: {USER_STATUS}
```

### 7. 更新 scan_state.json

`extract_trending.py` 已自动更新 `trending.last_scan_ts`。`trending.daily_done` 只给 `wechat-trending-daily` 日汇总使用，这里不用手动改。
