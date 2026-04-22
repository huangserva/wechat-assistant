# 技术讨论扫描（决策脑 v2） — Cron Prompt

## 任务

从微信群聊中提取技术讨论内容，按分类汇总重点技术话题，推送到飞书。


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

> 将 `USER_STATUS` 和 `USER_CONTEXT` 记录下来，供后续步骤参考。
> **如果 `USER_STATUS=offline 或 sleep**：可正常执行，因为技术讨论扫描是后台定时任务，不依赖用户在线。
> **如果 `USER_STATUS=busy 或 meeting**：同样正常执行，推送时可附加状态提示。

### 3. 提取技术讨论数据

```bash
python3 extract_tech.py --config /Users/serva/wechat-assistant/config.yaml
```

> 输出 JSON 到 stdout，按分类列出技术关键词的讨论频率、涉及的群、以及 highlights（包含上下文片段）。
> **如果 `already_done` 为 true** — 今天已经处理过，直接终止不重复推送。

### 4. 分析 JSON 输出

从各分类中提炼有价值的技术讨论。

#### 什么是重点技术讨论
- **新工具/产品首 impressions**（刚发布的工具的实际使用体验）
- **技术选型讨论**（A vs B 的对比分析）
- **踩坑/解决方案**（实际遇到的问题和解决办法）
- **行业趋势判断**（对某个技术方向的洞察）
- **代码/架构分享**（具体的实现方案）

#### 什么是噪音
- 仅提及名字但无实质讨论（"我用 claude 了"）
- 广告、推广
- 纯截图、链接无讨论
- 重复的转发

#### 分析方法
1. 优先关注 `highlights` 中有实质内容的讨论（不是只提了一下名字）
2. 合并同一话题在不同群的讨论（如多个群都在讨论同一个模型）
3. 提取最有价值的 insight（不是罗列所有提及）

### 5. 推送到飞书

格式：
```
🛠️ **YYYY-MM-DD 技术讨论日报**

---

### 🤖 AI/LLM 热门讨论

**Claude** — X 次提及 · Y 个群
> 最有价值的讨论摘要（群名 · 发送者 · HH:MM）
> 关键 insight 或结论

**GPT-4** — X 次提及 · Y 个群
> ...

---

### 💻 AI 编程工具

**Cursor** — X 次提及 · Y 个群
> ...

---

### 📊 今日统计

- 总技术提及：X 次（来自 Y 条消息）
- 热门分类：分类1（N次）、分类2（N次）、分类3（N次）
```

对于每个分类，只展示有实质性讨论的关键词（不只是被提及），跳过空分类。
如果所有分类都没有有价值的技术讨论，发一条简短的"昨天群里没什么技术讨论"。

### 5.5 状态栏

每条推送末尾加上状态栏：

```
---
🕐 cron: wechat-tech-scan · 运行于 YYYY-MM-DD HH:MM · 覆盖：昨天全天 · 技术提及 X · 有效讨论 Y · 状态: {USER_STATUS}
```

### 5.6 写入 assistant.db

将技术讨论结果写入 SQLite 数据库：

```bash
python3 /Users/serva/.hermes/skills/social-media/wechat-assistant/scripts/db_writer.py --db ~/wechat-assistant/assistant.db --table tech_highlights --data '[{items: category, keyword, count, groups, highlights(JSON array)}]'
python3 /Users/serva/.hermes/skills/social-media/wechat-assistant/scripts/db_writer.py --db ~/wechat-assistant/assistant.db --scan-log "tech:ok:X分类Y条有效讨论"
```

### 6. 状态

extract_tech.py 已自动更新 scan_state.json 中的 `tech.daily_done`，无需额外操作。
