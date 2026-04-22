#!/usr/bin/env python3
"""
db_writer.py — wechat-assistant 统一写入 assistant.db 的工具

供 cron prompt 中的 LLM 调用，将扫描结果持久化到 SQLite。

用法：
  # 写入 trending 扫描结果
  python3 db_writer.py --db ~/wechat-assistant/assistant.db --table trending_topics \
    --data '[{"keyword":"Claude","groups_count":6,"total_mentions":24,"source_groups":["群A","群B"],"is_merged":true}]'

  # 写入 todo
  python3 db_writer.py --db ~/wechat-assistant/assistant.db --table todos \
    --data '[{"id":"t1","contact":"张三","summary":"发链接","status":"open","created_ts":1776654323}]'

  # 从 JSON 文件读取
  python3 db_writer.py --db ~/wechat-assistant/assistant.db --table trending_topics \
    --file /tmp/trending_result.json

  # 查询（用于调试）
  python3 db_writer.py --db ~/wechat-assistant/assistant.db --query "SELECT * FROM trending_topics WHERE scan_date='2026-04-20'"

支持的表：trending_topics, trending_urls, todos, calendar_events, tech_highlights, preferences, profile_snapshots, digests
"""
import sqlite3
import json
import os
import sys
import argparse
from datetime import datetime, timezone, timedelta

_TZ8 = timezone(timedelta(hours=8))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trending_topics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_ts     INTEGER NOT NULL,          -- 扫描时间戳
    scan_date   TEXT NOT NULL,             -- YYYY-MM-DD
    keyword     TEXT NOT NULL,             -- 归一化后的话题名
    groups_count INTEGER DEFAULT 0,        -- 跨群数
    total_mentions INTEGER DEFAULT 0,     -- 总提及次数
    source_groups TEXT,                    -- JSON array of group names
    is_merged   INTEGER DEFAULT 0,        -- 是否经过归一化合并
    UNIQUE(scan_date, keyword)             -- 每天每个话题只保留最新
);

CREATE TABLE IF NOT EXISTS trending_urls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_ts     INTEGER NOT NULL,
    scan_date   TEXT NOT NULL,
    url         TEXT NOT NULL,
    title       TEXT DEFAULT '',
    share_count INTEGER DEFAULT 0,
    first_group TEXT DEFAULT '',
    first_time  TEXT DEFAULT '',
    UNIQUE(scan_date, url)
);

CREATE TABLE IF NOT EXISTS todos (
    id          TEXT PRIMARY KEY,          -- todo_001 等
    contact     TEXT DEFAULT '',
    summary     TEXT NOT NULL,
    status      TEXT DEFAULT 'open',       -- open / done / cancelled
    created_ts  INTEGER,                  -- 创建时间戳
    created_date TEXT,                    -- YYYY-MM-DD
    resolved_ts INTEGER,                 -- 完成时间戳
    resolved_date TEXT,                  -- YYYY-MM-DD
    context     TEXT DEFAULT '',
    updated_ts  INTEGER                  -- 最后更新时间
);

CREATE TABLE IF NOT EXISTS calendar_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_ts     INTEGER NOT NULL,
    scan_date   TEXT NOT NULL,
    event_date  TEXT,                     -- 事件日期
    content     TEXT NOT NULL,
    contact     TEXT DEFAULT '',
    status      TEXT DEFAULT 'pending',   -- pending / confirmed / done / cancelled
    UNIQUE(scan_date, event_date, content)
);

CREATE TABLE IF NOT EXISTS tech_highlights (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_ts     INTEGER NOT NULL,
    scan_date   TEXT NOT NULL,
    category    TEXT NOT NULL,            -- AI/LLM, 编程语言 等
    keyword     TEXT NOT NULL,
    count       INTEGER DEFAULT 0,
    groups      INTEGER DEFAULT 0,
    highlights  TEXT DEFAULT '',          -- JSON array of context snippets
    UNIQUE(scan_date, category, keyword)
);

CREATE TABLE IF NOT EXISTS preferences (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,            -- YYYY-MM-DD
    category    TEXT DEFAULT '',          -- tech/business/decision/opinion
    content     TEXT NOT NULL,
    msg_time    INTEGER,                 -- 原始消息时间戳
    chatroom_id TEXT DEFAULT '',
    UNIQUE(date, category, content, msg_time)
);

CREATE TABLE IF NOT EXISTS profile_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,            -- YYYY-MM-DD
    dimension   TEXT NOT NULL,            -- tech/business/decision/communication/writing/personal
    conclusions TEXT NOT NULL,            -- JSON array of conclusions
    UNIQUE(date, dimension)
);

CREATE TABLE IF NOT EXISTS digests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,             -- YYYY-MM-DD
    content     TEXT NOT NULL,            -- JSON blob of digest data
    created_ts  INTEGER
);

CREATE TABLE IF NOT EXISTS scan_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_ts     INTEGER NOT NULL,
    scan_date   TEXT NOT NULL,
    scan_type   TEXT NOT NULL,            -- todo/calendar/trending/tech/digest/insight/preference
    status      TEXT DEFAULT 'ok',        -- ok / error / no_data
    message     TEXT DEFAULT '',          -- 状态栏信息或错误
    groups_count INTEGER DEFAULT 0,
    messages_count INTEGER DEFAULT 0,
    duration_ms INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS push_feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    push_time   TEXT NOT NULL,            -- 推送时间 ISO格式
    push_type   TEXT NOT NULL,            -- todo/calendar/trending/digest/tech
    content_summary TEXT NOT NULL,        -- 推送内容摘要
    priority    TEXT DEFAULT 'normal',    -- urgent/normal/deferred
    user_action TEXT DEFAULT NULL,        -- acted/ignored/snoozed（NULL=未判断）
    action_time TEXT DEFAULT NULL,        -- 用户行动时间
    inferred_from TEXT DEFAULT NULL       -- 推断来源（todo_resolved/feishu_read/timeout等）
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_trending_date ON trending_topics(scan_date);
CREATE INDEX IF NOT EXISTS idx_trending_keyword ON trending_topics(keyword);
CREATE INDEX IF NOT EXISTS idx_todos_status ON todos(status);
CREATE INDEX IF NOT EXISTS idx_todos_created ON todos(created_date);
CREATE INDEX IF NOT EXISTS idx_cal_date ON calendar_events(event_date);
CREATE INDEX IF NOT EXISTS idx_tech_date ON tech_highlights(scan_date);
CREATE INDEX IF NOT EXISTS idx_prefs_date ON preferences(date);
CREATE INDEX IF NOT EXISTS idx_profile_date ON profile_snapshots(date);
CREATE INDEX IF NOT EXISTS idx_scan_log_type ON scan_log(scan_type, scan_date);
CREATE INDEX IF NOT EXISTS idx_push_feedback_type ON push_feedback(push_type, push_time);
CREATE INDEX IF NOT EXISTS idx_push_feedback_action ON push_feedback(user_action);
"""


def _now_ts():
    return int(datetime.now(tz=_TZ8).timestamp())


def _now_date():
    return datetime.now(tz=_TZ8).strftime('%Y-%m-%d')


def _ensure_db(db_path):
    """确保数据库存在并建表"""
    conn = sqlite3.connect(db_path)
    conn.text_factory = lambda b: b.decode('utf-8', 'replace')
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def write_trending_topics(conn, items, scan_ts=None, scan_date=None):
    scan_ts = scan_ts or _now_ts()
    scan_date = scan_date or _now_date()
    for item in items:
        conn.execute("""
            INSERT OR REPLACE INTO trending_topics
                (scan_ts, scan_date, keyword, groups_count, total_mentions, source_groups, is_merged)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            scan_ts, scan_date,
            item.get('keyword', ''),
            item.get('groups_count', 0),
            item.get('total_mentions', 0),
            json.dumps(item.get('source_groups', []), ensure_ascii=False),
            1 if item.get('is_merged') else 0,
        ))
    conn.commit()
    return len(items)


def write_trending_urls(conn, items, scan_ts=None, scan_date=None):
    scan_ts = scan_ts or _now_ts()
    scan_date = scan_date or _now_date()
    for item in items:
        conn.execute("""
            INSERT OR REPLACE INTO trending_urls
                (scan_ts, scan_date, url, title, share_count, first_group, first_time)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            scan_ts, scan_date,
            item.get('url', ''),
            item.get('title', ''),
            item.get('share_count', 0),
            item.get('first_seen_group', ''),
            item.get('first_seen_time', ''),
        ))
    conn.commit()
    return len(items)


def write_todos(conn, items):
    now_ts = _now_ts()
    for item in items:
        # 如果 status 是 done 且没有 resolved_ts，自动设置
        status = item.get('status', 'open')
        resolved_ts = item.get('resolved_ts')
        resolved_date = item.get('resolved_date')
        if status == 'done' and not resolved_ts:
            resolved_ts = now_ts
            resolved_date = _now_date()
        conn.execute("""
            INSERT OR REPLACE INTO todos
                (id, contact, summary, status, created_ts, created_date, resolved_ts, resolved_date, context, updated_ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            item.get('id', f'todo_{now_ts}'),
            item.get('contact', ''),
            item.get('summary', ''),
            status,
            item.get('created_ts'),
            item.get('created_date'),
            resolved_ts,
            resolved_date,
            item.get('context', ''),
            now_ts,
        ))
    conn.commit()
    return len(items)


def write_calendar_events(conn, items, scan_ts=None, scan_date=None):
    scan_ts = scan_ts or _now_ts()
    scan_date = scan_date or _now_date()
    for item in items:
        conn.execute("""
            INSERT OR REPLACE INTO calendar_events
                (scan_ts, scan_date, event_date, content, contact, status)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            scan_ts, scan_date,
            item.get('event_date', ''),
            item.get('content', ''),
            item.get('contact', ''),
            item.get('status', 'pending'),
        ))
    conn.commit()
    return len(items)


def write_tech_highlights(conn, items, scan_ts=None, scan_date=None):
    scan_ts = scan_ts or _now_ts()
    scan_date = scan_date or _now_date()
    for item in items:
        highlights = item.get('highlights', [])
        if isinstance(highlights, list):
            highlights = json.dumps(highlights, ensure_ascii=False)
        conn.execute("""
            INSERT OR REPLACE INTO tech_highlights
                (scan_ts, scan_date, category, keyword, count, groups, highlights)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            scan_ts, scan_date,
            item.get('category', ''),
            item.get('keyword', ''),
            item.get('count', 0),
            item.get('groups', 0),
            highlights,
        ))
    conn.commit()
    return len(items)


def write_preferences(conn, items, date=None):
    date = date or _now_date()
    for item in items:
        conn.execute("""
            INSERT OR IGNORE INTO preferences
                (date, category, content, msg_time, chatroom_id)
            VALUES (?, ?, ?, ?, ?)
        """, (
            date,
            item.get('category', ''),
            item.get('content', ''),
            item.get('msg_time'),
            item.get('chatroom_id', ''),
        ))
    conn.commit()
    return len(items)


def write_profile_snapshots(conn, items, date=None):
    date = date or _now_date()
    for item in items:
        conclusions = item.get('conclusions', [])
        if isinstance(conclusions, list):
            conclusions = json.dumps(conclusions, ensure_ascii=False)
        conn.execute("""
            INSERT OR REPLACE INTO profile_snapshots
                (date, dimension, conclusions)
            VALUES (?, ?, ?)
        """, (
            date,
            item.get('dimension', ''),
            conclusions,
        ))
    conn.commit()
    return len(items)


def write_digests(conn, items):
    now_ts = _now_ts()
    for item in items:
        content = item.get('content', '')
        if isinstance(content, (dict, list)):
            content = json.dumps(content, ensure_ascii=False)
        conn.execute("""
            INSERT OR REPLACE INTO digests
                (date, content, created_ts)
            VALUES (?, ?, ?)
        """, (
            item.get('date', _now_date()),
            content,
            now_ts,
        ))
    conn.commit()
    return len(items)


def write_scan_log(conn, scan_type, status='ok', message='', 
                   groups_count=0, messages_count=0, duration_ms=0):
    conn.execute("""
        INSERT INTO scan_log
            (scan_ts, scan_date, scan_type, status, message, groups_count, messages_count, duration_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (_now_ts(), _now_date(), scan_type, status, message,
          groups_count, messages_count, duration_ms))
    conn.commit()


_TABLE_WRITERS = {
    'trending_topics': write_trending_topics,
    'trending_urls': write_trending_urls,
    'todos': write_todos,
    'calendar_events': write_calendar_events,
    'tech_highlights': write_tech_highlights,
    'preferences': write_preferences,
    'profile_snapshots': write_profile_snapshots,
    'digests': write_digests,
}


def write_push_feedback(conn, items):
    """写入推送反馈记录"""
    now_str = datetime.now(tz=_TZ8).isoformat()
    for item in items:
        conn.execute("""
            INSERT INTO push_feedback
                (push_time, push_type, content_summary, priority, user_action, action_time, inferred_from)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            item.get('push_time', now_str),
            item.get('push_type', ''),
            item.get('content_summary', ''),
            item.get('priority', 'normal'),
            item.get('user_action'),
            item.get('action_time'),
            item.get('inferred_from'),
        ))
    conn.commit()
    return len(items)


# Register push_feedback in table writers
_TABLE_WRITERS['push_feedback'] = write_push_feedback


def main():
    parser = argparse.ArgumentParser(description='写入 assistant.db')
    parser.add_argument('--db', required=True, help='assistant.db 路径')
    parser.add_argument('--table', help='要写入的表名')
    parser.add_argument('--data', help='JSON 数据字符串')
    parser.add_argument('--file', help='JSON 数据文件路径')
    parser.add_argument('--query', help='执行 SQL 查询（调试用）')
    parser.add_argument('--scan-log', help='记录 scan_log，格式: type:status:message')
    parser.add_argument('--init', action='store_true', help='只初始化数据库建表')
    args = parser.parse_args()

    conn = _ensure_db(args.db)

    if args.init:
        print(f"[OK] Database initialized: {args.db}")
        conn.close()
        return

    if args.query:
        rows = conn.execute(args.query).fetchall()
        cols = [d[0] for d in conn.execute(args.query).description]
        print(json.dumps([dict(zip(cols, r)) for r in rows], ensure_ascii=False, indent=2))
        conn.close()
        return

    if args.scan_log:
        parts = args.scan_log.split(':', 2)
        scan_type = parts[0] if len(parts) > 0 else 'unknown'
        status = parts[1] if len(parts) > 1 else 'ok'
        message = parts[2] if len(parts) > 2 else ''
        write_scan_log(conn, scan_type, status, message)
        print(f"[OK] scan_log: {scan_type} {status}")
        conn.close()
        return

    if not args.table:
        print("[ERROR] --table is required when not using --query/--scan-log/--init", file=sys.stderr)
        sys.exit(1)

    # 读取数据
    if args.file:
        with open(args.file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    elif args.data:
        data = json.loads(args.data)
    else:
        print("[ERROR] --data or --file is required", file=sys.stderr)
        sys.exit(1)

    if not isinstance(data, list):
        data = [data]

    # 写入
    writer = _TABLE_WRITERS.get(args.table)
    if not writer:
        print(f"[ERROR] Unknown table: {args.table}. Available: {list(_TABLE_WRITERS.keys())}", file=sys.stderr)
        sys.exit(1)

    count = writer(conn, data)
    print(f"[OK] {args.table}: wrote {count} rows")
    conn.close()


if __name__ == '__main__':
    main()
