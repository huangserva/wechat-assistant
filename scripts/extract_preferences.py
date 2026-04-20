#!/usr/bin/env python3
"""
extract_preferences.py — 从 collector.db 提取用户偏好/观点消息（不调 AI）

用法：
  python3 extract_preferences.py --config config.yaml           # 今日累计
  python3 extract_preferences.py --config config.yaml --full     # 昨天整天
  python3 extract_preferences.py --config config.yaml --days 7   # 最近 N 天

输出 JSON 到 stdout：
{
  "mode": "today|full|days",
  "ts_start": ...,
  "ts_end": ...,
  "preferences": [
    {"category": "tech|business|writing|decision|opinion",
     "content": "...",
     "context": "...",      // 上下文（前后消息）
     "contact": "...",
     "chatroom_id": "...",
     "time": "YYYY-MM-DD HH:MM",
     "msg_time": 1234567890}
  ]
}
"""
import sqlite3
import json
import os
import sys
import argparse
import re
from datetime import datetime, timezone, timedelta
from collections import defaultdict

_TZ8 = timezone(timedelta(hours=8))

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

# ═══════════════════════════════════════════════════════════
# 偏好/观点 关键词模式
# ═══════════════════════════════════════════════════════════

# 观点表达模式
_OPINION_PATTERNS = [
    r'我觉得', r'我认为', r'我想', r'在我看来', r'我的看法',
    r'说实话', r'老实说', r'讲真', r'说实话', r'坦白讲',
    r'其实吧', r'说白了', r'本质上', r'归根结底',
    r'这不合理', r'这不对', r'这有问题', r'这不行',
    r'为什么不', r'难道不应该', r'应该要',
    r'关键是', r'核心是', r'重点是', r'本质是',
    r'比[他它]好', r'比[他它]强', r'不如', r'远不如', r'吊打',
    r'过度了', r'没必要', r'多此一举', r'搞复杂了',
    r'方向错了', r'思路不对', r'方法有问题',
]

# 技术偏好模式
_TECH_PATTERNS = [
    r'(?:用|选|换|转)(?:了|到|回)?(?:Python|Java|Go|Rust|TypeScript|JS|C\+\+|Swift|Kotlin)',
    r'(?:框架|库|工具|引擎|平台|架构)(?:我|我们)?(?:选|用|决定|倾向)',
    r'(?:React|Vue|Next|Nuxt|Svelte|Angular)',
    r'(?:Docker|K8s|kubernetes|云原生|微服务|Serverless)',
    r'(?:LLM|GPT|Claude|Gemini|模型|推理|微调|RAG|Agent)',
    r'(?:MacOS|Linux|Windows|iOS|Android)',
    r'(?:PostgreSQL|MySQL|MongoDB|Redis|SQLite)',
    r'(?:性能|并发|延迟|吞吐|优化|加速)',
    r'(?:部署|上线|灰度|回滚|监控)',
    r'(?:测试|单测|集成测试|CI|CD)',
    r'(?:好用的|推荐的|不错的|垃圾|难用|坑)',
    r'(?:稳定|靠谱|成熟|生态|社区)',
]

# 决策模式
_DECISION_PATTERNS = [
    r'我决定', r'我们决定', r'就这样', r'就这么定了',
    r'先[干做搞试]',
    r'先不[干做搞要管]',
    r'还是[用选做](?:回|了)?',
    r'最后选了', r'最终方案',
    r'权衡下来', r'综合考虑',
    r'成本(?:太)?高', r'性价比', r'值得', r'不值得',
    r'预算', r'投资', r'回报',
]

# 商业/经营见解
_BUSINESS_PATTERNS = [
    r'(?:市场|行业|赛道|趋势)(?:的)?(?:方向|机会|风险|变化)',
    r'(?:客户|用户)(?:的)?(?:需求|痛点|反馈)',
    r'(?:产品|服务)(?:的)?(?:定位|差异化|优势)',
    r'(?:利润|营收|成本|毛利|净利)',
    r'(?:竞争|对手|竞品|护城河)',
    r'(?:模式|玩法|策略|打法)',
    r'(?:团队|管理|组织|人才)',
    r'(?:融资|估值|上市|并购)',
]

# 写作/表达风格（直接提取，不需要关键词匹配）
# 通过提取所有 __self__ 的文本消息，交给 AI 分析风格


def _compile_patterns():
    """编译所有模式，返回 (pattern, category) 列表"""
    patterns = []
    for p in _OPINION_PATTERNS:
        patterns.append((re.compile(p), 'opinion'))
    for p in _TECH_PATTERNS:
        patterns.append((re.compile(p), 'tech'))
    for p in _DECISION_PATTERNS:
        patterns.append((re.compile(p), 'decision'))
    for p in _BUSINESS_PATTERNS:
        patterns.append((re.compile(p), 'business'))
    return patterns

_PATTERNS = _compile_patterns()

# 最小长度：太短的消息没有分析价值
_MIN_CONTENT_LEN = 6

# 排除模式：系统消息、媒体占位符
_EXCLUDE_PREFIXES = ('[📝', '[🖼️', '[🎤', '[🎥', '[😄', '[📎', '[💬', '[📋',
                      '<?xml', '<msg', '[img:', '系统消息')


def classify_message(content):
    """判断一条消息是否包含偏好/观点，返回匹配的 category 列表或空列表"""
    if not content or len(content) < _MIN_CONTENT_LEN:
        return []
    if any(content.startswith(p) for p in _EXCLUDE_PREFIXES):
        return []

    categories = set()
    for regex, cat in _PATTERNS:
        if regex.search(content):
            categories.add(cat)

    return sorted(categories)


def parse_args():
    parser = argparse.ArgumentParser(description='从 collector.db 提取偏好/观点消息')
    parser.add_argument('--config', required=True, help='YAML 配置文件路径')
    parser.add_argument('--full', action='store_true', help='全量模式：昨天整天')
    parser.add_argument('--days', type=int, help='最近 N 天')
    return parser.parse_args()


def load_config(config_path):
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'decrypt'))
    from config import load_config as _load
    return _load(config_path)


def get_preference_messages(collector_db, ts_start, ts_end, self_wxid='__self__'):
    """提取时间范围内用户自己的偏好/观点消息，带上下文"""
    conn = sqlite3.connect(collector_db)
    conn.text_factory = lambda b: b.decode('utf-8', errors='replace')

    # 1. 获取所有 __self__ 的文本消息
    rows = conn.execute("""
        SELECT chatroom_id, sender, content, msg_time, msg_type
        FROM messages
        WHERE (sender = ? OR sender = '__self__')
        AND msg_time >= ? AND msg_time < ?
        AND content NOT LIKE '[img:%'
        AND content NOT LIKE '[🖼️%%'
        AND content NOT LIKE '<msg>%%'
        AND content NOT LIKE '<?xml%%'
        AND content NOT LIKE '[📎%%'
        AND content NOT LIKE '[📝%%'
        AND content NOT LIKE '[🎤%%'
        AND content NOT LIKE '[🎥%%'
        AND content NOT LIKE '[😄%%'
        AND content NOT LIKE '[💬%%'
        AND content NOT LIKE '[📋%%'
        AND content NOT LIKE '<voipmsg%%'
        AND content NOT LIKE '系统消息%%'
        AND content NOT LIKE '欢迎使用%%'
        AND content NOT LIKE '<sysmsg%%'
        AND length(content) > ?
        ORDER BY msg_time
    """, (self_wxid, ts_start, ts_end, _MIN_CONTENT_LEN)).fetchall()

    # 2. 分类：找出所有命中模式的 self 消息
    hits = []
    for cid, sender, content, ts, mt in rows:
        cats = classify_message(content)
        if cats:
            hits.append((cid, content, ts, cats))

    if not hits:
        conn.close()
        return []

    # 3. 批量获取联系人和上下文（减少 DB 连接次数）
    # 按 chatroom_id 分组
    by_cid = defaultdict(list)
    for cid, content, ts, cats in hits:
        by_cid[cid].append((content, ts, cats))

    # 查联系人名：私聊取对方名，群聊标记为 [群]
    contact_names = {}
    # 尝试从 session.db 取昵称
    session_db = os.path.join(os.path.dirname(collector_db), 'decrypted', 'session', 'session.db')
    session_conn = None
    if os.path.exists(session_db):
        try:
            session_conn = sqlite3.connect(session_db)
            session_conn.text_factory = lambda b: b.decode('utf-8', errors='replace')
        except Exception:
            session_conn = None

    for cid in by_cid:
        is_group = '@chatroom' in cid
        name = cid
        if is_group:
            # 群聊：从 SessionTable 取 last_sender_display_name（最近的联系人名）
            # 并标注为群聊
            group_label = f'群:{cid[:10]}...'
            if session_conn:
                r = session_conn.execute(
                    'SELECT last_sender_display_name FROM SessionTable WHERE username = ?', (cid,)
                ).fetchone()
                if r and r[0]:
                    group_label = f'群({r[0]})'
            name = group_label
        elif session_conn:
            # 私聊：从 SessionTable 取对方昵称
            r = session_conn.execute(
                'SELECT last_sender_display_name FROM SessionTable WHERE username = ?', (cid,)
            ).fetchone()
            name = r[0] if r and r[0] else cid
        else:
            # 降级：取最近非 self 发言者
            r = conn.execute(
                'SELECT sender FROM messages WHERE chatroom_id=? AND sender != "__self__" AND sender != "" ORDER BY msg_time DESC LIMIT 1',
                (cid,)
            ).fetchone()
            name = r[0] if r else cid
        contact_names[cid] = name

    if session_conn:
        session_conn.close()
    conn.close()

    # 构建结果（上下文用内存中的 rows 数据）
    # 先把所有 self 消息按 (cid, ts) 索引
    all_msgs_index = {}
    for cid, sender, content, ts, mt in rows:
        all_msgs_index.setdefault(cid, {})[ts] = content

    results = []
    for cid, items in by_cid.items():
        # 获取这个 chat 的所有消息时间点（排序）
        chat_ts_list = sorted(all_msgs_index.get(cid, {}).keys())

        for content, ts, cats in items:
            # 找前后各 2 条
            idx = chat_ts_list.index(ts) if ts in chat_ts_list else -1
            if idx >= 0:
                start_idx = max(0, idx - 2)
                end_idx = min(len(chat_ts_list), idx + 3)
                ctx_ts = chat_ts_list[start_idx:end_idx]
                context_parts = []
                for t in ctx_ts:
                    time_str = datetime.fromtimestamp(t, tz=_TZ8).strftime('%H:%M')
                    c = all_msgs_index[cid][t][:100]
                    marker = '→ ' if t == ts else '  '
                    context_parts.append(f"{marker}[{time_str}] {c}")
            else:
                context_parts = [f"→ {content[:100]}"]

            results.append({
                'categories': cats,
                'content': content[:500],
                'context': '\n'.join(context_parts),
                'contact': contact_names.get(cid, cid),
                'chatroom_id': cid,
                'time': datetime.fromtimestamp(ts, tz=_TZ8).strftime('%Y-%m-%d %H:%M'),
                'msg_time': ts,
            })

    return results


def get_all_self_messages(collector_db, ts_start, ts_end, self_wxid='__self__', limit=200):
    """提取用户自己的所有文本消息（用于写作风格分析）
    返回最多 limit 条，按时间均匀采样
    """
    conn = sqlite3.connect(collector_db)
    conn.text_factory = lambda b: b.decode('utf-8', errors='replace')

    # 先看总数
    cnt = conn.execute("""
        SELECT COUNT(*) FROM messages
        WHERE (sender = ? OR sender = '__self__')
        AND msg_time >= ? AND msg_time < ?
        AND msg_type = 1
        AND length(content) > 10
    """, (self_wxid, ts_start, ts_end)).fetchone()[0]

    if cnt <= limit:
        rows = conn.execute("""
            SELECT content, msg_time FROM messages
            WHERE (sender = ? OR sender = '__self__')
            AND msg_time >= ? AND msg_time < ?
            AND msg_type = 1
            AND length(content) > 10
            ORDER BY msg_time
        """, (self_wxid, ts_start, ts_end)).fetchall()
    else:
        # 均匀采样
        rows = conn.execute("""
            SELECT content, msg_time FROM messages
            WHERE (sender = ? OR sender = '__self__')
            AND msg_time >= ? AND msg_time < ?
            AND msg_type = 1
            AND length(content) > 10
            ORDER BY RANDOM() LIMIT ?
        """, (self_wxid, ts_start, ts_end, limit)).fetchall()
        rows.sort(key=lambda x: x[1])

    conn.close()
    return rows


def main():
    args = parse_args()
    cfg = load_config(args.config)

    collector_db = cfg['collector_db']
    self_wxid = cfg.get('self_wxid', '')

    now = datetime.now(tz=_TZ8)
    now_ts = int(now.timestamp())

    if args.full:
        today_0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday_0 = today_0 - timedelta(days=1)
        ts_start = int(yesterday_0.timestamp())
        ts_end = int(today_0.timestamp())
        mode = 'full'
    elif args.days:
        ts_start = int((now - timedelta(days=args.days)).timestamp())
        ts_end = now_ts
        mode = f'last_{args.days}_days'
    else:
        today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        ts_start = int(today_midnight.timestamp())
        ts_end = now_ts
        mode = 'today'

    # 提取偏好消息
    preferences = get_preference_messages(collector_db, ts_start, ts_end, self_wxid)

    # 提取写作样本
    writing_samples = get_all_self_messages(collector_db, ts_start, ts_end, self_wxid, limit=50)

    # 统计
    cat_counts = defaultdict(int)
    for p in preferences:
        for c in p['categories']:
            cat_counts[c] += 1

    output = {
        'mode': mode,
        'ts_start': ts_start,
        'ts_end': ts_end,
        'scan_time': now.strftime('%Y-%m-%d %H:%M'),
        'stats': {
            'preference_count': len(preferences),
            'category_counts': dict(cat_counts),
            'writing_samples_count': len(writing_samples),
        },
        'preferences': preferences,
        'writing_samples': [c for c, _ in writing_samples],
    }

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
