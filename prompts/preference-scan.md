# 用户偏好画像分析 — Cron Prompt（固定流程版）

## 目标

读取最近 7 天 `preferences/*.json` 的累计归档，结合现有 `profile/servasyy_profile.json`，做一次轻量增量画像更新，并输出中文飞书报告。

## 硬性规则

- **禁止**运行 `extract_preferences.py --days 7`；该命令会重扫 `collector.db`，成本高且不稳定。
- **禁止**使用 `delegate_task` / 子 Agent。
- **禁止**临时生成分析脚本；只能使用本 prompt 指定的两个固定脚本。
- **禁止**调用 `send_message`；cron 的最终回复会自动投递。
- 如果没有新数据或今天已完成，最终只回复 `[SILENT]`。

## 固定步骤

### 1. 准备输入

只运行这一条：

```bash
python3 /Users/serva/.hermes/skills/social-media/wechat-assistant/scripts/prepare_preference_scan.py --config /Users/serva/wechat-assistant/config.yaml
```

读取输出 JSON：
- 如果 `should_run == false`：最终回复 `[SILENT]`，不要做后续动作。
- 如果 `should_run == true`：继续下一步。

### 2. 增量分析

基于 prepare 输出中的：
- `preferences`
- `writing_samples`
- `existing_profile`
- `feedback_stats`
- `user_status`

更新画像。要求：
- 保留仍然有效的旧结论；不要重写成全新画像。
- 每次最多新增 5 条真正有新信息的 conclusions。
- 每条新增/更新结论必须包含：`finding`、`confidence`、`source_count`、`evidence`、`first_seen`、`last_seen`。
- 维度固定优先使用：`tech_preferences`、`business_insights`、`decision_patterns`、`communication_style`、`writing_style`。
- 输出必须是完整 JSON 对象，不要 Markdown。
- 输出内容必须是中文。

### 3. 写临时画像 JSON

将完整画像 JSON 写到：

```text
/tmp/wechat_preference_profile.json
```

### 4. 校验并落地

只运行这一条：

```bash
python3 /Users/serva/.hermes/skills/social-media/wechat-assistant/scripts/finalize_preference_scan.py --config /Users/serva/wechat-assistant/config.yaml --profile-input /tmp/wechat_preference_profile.json --scan-log-message "preference:ok"
```

如果 finalize 返回 `ok: false` 或命令失败：最终报告失败原因，不要伪装成功。

### 5. 中文报告

报告格式：

```text
🧑 用户画像更新（MM.DD）

### 🔧 技术偏好
- 新增/更新结论（confidence: high/medium/low，来源: N 条）

### 💼 商业见解
- ...

### ⚡ 决策模式
- ...

### 💬 沟通风格
- ...

### ✍️ 写作风格
- ...

---
📊 本期分析：偏好消息 N 条 · 写作样本 M 条（累计 T 条） · 画像维度 D · 状态: USER_STATUS
```

如果没有实质新增，只输出简短确认：

```text
🧑 用户画像确认（MM.DD）
无新增偏好发现。现有画像保持稳定。

---
📊 本期分析：偏好消息 N 条 · 写作样本 M 条（累计 T 条） · 画像维度 D · 状态: USER_STATUS
```
