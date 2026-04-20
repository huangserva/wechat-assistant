#!/usr/bin/env python3
"""
extract_todos.py — 从 collector.db 提取私聊对话，输出 JSON（不调 AI）

用法：
  python3 extract_todos.py --config config.yaml           # 增量：最近 90 分钟
  python3 extract_todos.py --config config.yaml --full     # 全量：昨天整天

输出 JSON 到 stdout:
{
  "mode": "full|incremental",
  "ts_start": 1234567890,
  "ts_end": 1234567890,
  "conversations": [
    {"contact": "联系人名", "chatroom_id": "xxx", "messages": [
      {"who": "我|对方名", "content": "...", "time": "HH:MM"}
    ]}
  ],
  "existing_todos": [...],
  "scan_state_path": "..."
}
"""
import sqlite3
import json
import os
import sys
import argparse
from datetime import datetime, timezone, timedelta
from collections import defaultdict

_TZ8 = timezone(timedelta(hours=8))

# 共享模块路径
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from state_manager import StateManager


def parse_args():
    parser = argparse.ArgumentParser(description='从 collector.db 提取私聊对话')
    parser.add_argument('--config', required=True, help='YAML 配置文件路径')
    parser.add_argument('--full', action='store_true', help='全量模式：昨天整天')
    parser.add_argument('--state', help='scan_state.json 路径（默认从 config 推导）')
    return parser.parse_args()


def load_config(config_path):
    """加载配置"""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'decrypt'))
    from config import load_config as _load
    return _load(config_path)


def get_state_path(cfg, args):
    """获取 state 文件路径"""
    if args.state:
        return args.state
    # 从 config 路径推导：config.yaml 同级目录的 scan_state.json
    config_dir = os.path.dirname(os.path.abspath(args.config))
    return os.path.join(config_dir, 'scan_state.json')


def get_dms(collector_db, ts_start, ts_end):
    """获取时间范围内的私聊对话（按会话分组）"""
    conn = sqlite3.connect(collector_db)
    conn.text_factory = lambda b: b.decode('utf-8', 'replace')

    rows = conn.execute("""
        SELECT chatroom_id, sender, content, msg_time
        FROM messages
        WHERE chatroom_id NOT LIKE '%@chatroom'
        AND chatroom_id NOT LIKE 'gh_%'
        AND chatroom_id NOT LIKE 'brand%'
        AND chatroom_id NOT LIKE 'mphelper%'
        AND chatroom_id NOT LIKE '@placeholder%'
        AND chatroom_id NOT LIKE '%@openim'
        AND msg_time >= ? AND msg_time < ?
        AND content NOT LIKE '[img:%'
        AND content NOT LIKE '[🖼️%%'
        AND content NOT LIKE '<msg>%%'
        AND content NOT LIKE '<?xml%%'
        AND content NOT LIKE '[📎%%'
        AND length(content) > 1
        ORDER BY chatroom_id, msg_time
    """, (ts_start, ts_end)).fetchall()

    chats = defaultdict(list)
    for cid, sender, content, ts in rows:
        chats[cid].append({
            'who': '我' if sender == '__self__' else sender,
            'content': content[:300],
            'time': datetime.fromtimestamp(ts, tz=_TZ8).strftime('%H:%M')
        })

    # 查询每个会话的联系人名
    names = {}
    for cid in chats:
        r = conn.execute(
            'SELECT sender FROM messages WHERE chatroom_id=? AND sender != "__self__" AND sender != "" ORDER BY msg_time DESC LIMIT 1',
            (cid,)
        ).fetchone()
        names[cid] = r[0] if r and r[0] else cid

    # 过滤：只保留双向对话
    result = []
    for cid, msgs in chats.items():
        has_self = any(m['who'] == '我' for m in msgs)
        has_other = any(m['who'] != '我' for m in msgs)
        if has_self and has_other:
            result.append({
                'contact': names.get(cid, cid),
                'chatroom_id': cid,
                'messages': msgs,
            })

    conn.close()
    return result


def main():
    args = parse_args()
    cfg = load_config(args.config)

    collector_db = cfg['collector_db']
    state_path = get_state_path(cfg, args)
    sm = StateManager(state_path)

    now = datetime.now(tz=_TZ8)
    now_ts = int(now.timestamp())

    if args.full:
        # 全量：昨天整天
        today_0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday_0 = today_0 - timedelta(days=1)
        ts_start = int(yesterday_0.timestamp())
        ts_end = int(today_0.timestamp())
        mode = 'full'
    else:
        # 今日累计模式：每次从今天 00:00 到 now，看全天私聊
        today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        ts_start = int(today_midnight.timestamp())
        ts_end = now_ts
        mode = 'today_cumulative'

    conversations = get_dms(collector_db, ts_start, ts_end)

    # 从 scan_state 读取已有 open todos
    todo_state = sm.get_todos()
    existing_todos = [t for t in todo_state.get('items', []) if t.get('status') == 'open']

    # 兼容：也读取旧的 todos.json（如果存在）
    old_todos_file = cfg.get('todos_file', '')
    if old_todos_file and os.path.exists(old_todos_file):
        try:
            with open(old_todos_file) as f:
                old_todos = json.load(f)
            old_open = [t for t in old_todos if t.get('status') == 'open']
            # 合并：以 scan_state 为准，补充旧文件中 scan_state 没有的
            existing_ids = {t.get('id') for t in existing_todos}
            for t in old_open:
                if t.get('id') not in existing_ids:
                    existing_todos.append(t)
        except (json.JSONDecodeError, IOError):
            pass

    output = {
        'mode': mode,
        'ts_start': ts_start,
        'ts_end': ts_end,
        'scan_time': now.strftime('%Y-%m-%d %H:%M'),
        'conversations_count': len(conversations),
        'conversations': conversations,
        'existing_todos': existing_todos,
        'scan_state_path': state_path,
    }

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
