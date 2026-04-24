#!/usr/bin/env python3
"""
prepare_preference_scan.py — 固定读取累计偏好归档，供 preference-scan cron 使用。

只读运行时数据，不调用 AI，不改状态。
"""
import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from state_manager import StateManager

_TZ8 = timezone(timedelta(hours=8))
_DEFAULT_DIMENSIONS = [
    'tech_preferences',
    'business_insights',
    'decision_patterns',
    'communication_style',
    'writing_style',
]


def parse_args():
    parser = argparse.ArgumentParser(description='准备 preference-scan 的固定输入')
    parser.add_argument('--config', required=True, help='运行时 config.yaml 路径')
    parser.add_argument('--state', help='scan_state.json 路径，默认与 config 同目录')
    parser.add_argument('--days', type=int, default=7, help='读取最近 N 天归档，默认 7')
    parser.add_argument('--max-preferences', type=int, default=36, help='最多返回偏好消息数')
    parser.add_argument('--max-writing-samples', type=int, default=40, help='最多返回写作样本数')
    parser.add_argument('--max-content-chars', type=int, default=180, help='单条内容最大字符数')
    parser.add_argument('--force', action='store_true', help='忽略当天已运行标记')
    return parser.parse_args()


def _runtime_dir(config_path):
    return os.path.dirname(os.path.abspath(config_path))


def _state_path(config_path, state_arg=None):
    if state_arg:
        return state_arg
    return os.path.join(_runtime_dir(config_path), 'scan_state.json')


def _load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def _clip(text, max_chars):
    text = '' if text is None else str(text)
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + '…'


def _sample_evenly(items, limit):
    if limit <= 0:
        return []
    if len(items) <= limit:
        return items
    if limit == 1:
        return [items[-1]]
    step = (len(items) - 1) / float(limit - 1)
    picked = []
    used = set()
    for i in range(limit):
        idx = round(i * step)
        if idx in used:
            continue
        used.add(idx)
        picked.append(items[idx])
    return picked


def _dedupe_preferences(items):
    seen = set()
    out = []
    for item in sorted(items, key=lambda value: value.get('msg_time', 0)):
        key = (item.get('chatroom_id', ''), item.get('msg_time', 0), item.get('content', ''))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _dedupe_samples(items):
    seen = set()
    out = []
    for item in sorted(items, key=lambda value: value.get('msg_time', 0)):
        key = (item.get('msg_time', 0), item.get('content', ''))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _load_archives(preferences_dir, days):
    today = datetime.now(tz=_TZ8).date()
    files = []
    for offset in range(days):
        day = (today - timedelta(days=offset)).isoformat()
        path = os.path.join(preferences_dir, f'{day}.json')
        if os.path.exists(path):
            files.append(path)

    preferences = []
    samples = []
    per_file = []
    for path in sorted(files):
        data = _load_json(path, {})
        file_prefs = data.get('preferences', [])
        preferences.extend(file_prefs)
        sample_meta = data.get('writing_samples_meta', [])
        if sample_meta:
            samples.extend(sample_meta)
        else:
            for content in data.get('writing_samples', []):
                samples.append({'content': content, 'msg_time': 0, 'time': ''})
        per_file.append({
            'path': path,
            'date': data.get('date') or os.path.basename(path).removesuffix('.json'),
            'preference_count': len(file_prefs),
            'writing_samples_count': len(sample_meta) or len(data.get('writing_samples', [])),
            'mode': data.get('mode', ''),
        })
    return files, per_file, _dedupe_preferences(preferences), _dedupe_samples(samples)


def _category_counts(preferences):
    counts = defaultdict(int)
    for item in preferences:
        for category in item.get('categories', []):
            counts[category] += 1
    return dict(sorted(counts.items()))


def _compact_preferences(items, max_chars):
    compact = []
    for item in items:
        compact.append({
            'categories': item.get('categories', []),
            'content': _clip(item.get('content', ''), max_chars),
            'contact': item.get('contact', ''),
            'time': item.get('time', ''),
            'msg_time': item.get('msg_time', 0),
        })
    return compact


def _compact_samples(items, max_chars):
    return [
        {
            'content': _clip(item.get('content', ''), max_chars),
            'time': item.get('time', ''),
            'msg_time': item.get('msg_time', 0),
        }
        for item in items
    ]


def _feedback_stats(db_path):
    if not os.path.exists(db_path):
        return {'total': 0, 'by_type': {}, 'by_action': {}, 'by_hour': {}, 'note': 'assistant.db not found'}
    try:
        conn = sqlite3.connect(db_path)
        cutoff = (datetime.now(tz=_TZ8) - timedelta(days=7)).isoformat()
        rows = conn.execute(
            """
            SELECT push_type, priority, user_action, push_time
            FROM push_feedback
            WHERE push_time > ?
            ORDER BY push_time DESC
            """,
            (cutoff,),
        ).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        return {'total': 0, 'by_type': {}, 'by_action': {}, 'by_hour': {}, 'note': f'feedback unavailable: {exc}'}

    by_type = {}
    by_action = {'acted': 0, 'ignored': 0, 'snoozed': 0, 'pending': 0}
    by_hour = defaultdict(lambda: {'total': 0, 'acted': 0, 'ignored': 0})
    for push_type, priority, action, push_time in rows:
        action = action or 'pending'
        if push_type not in by_type:
            by_type[push_type] = {'total': 0, 'acted': 0, 'ignored': 0, 'snoozed': 0, 'pending': 0}
        by_type[push_type]['total'] += 1
        by_type[push_type][action] = by_type[push_type].get(action, 0) + 1
        by_action[action] = by_action.get(action, 0) + 1
        hour = push_time[11:13] if push_time and len(push_time) >= 13 else ''
        if hour:
            by_hour[hour]['total'] += 1
            if action in ('acted', 'ignored'):
                by_hour[hour][action] += 1
    return {
        'total': len(rows),
        'by_type': by_type,
        'by_action': by_action,
        'by_hour': dict(sorted(by_hour.items())),
    }


def _profile_summary(profile):
    dimensions = profile.get('dimensions', {}) if isinstance(profile, dict) else {}
    counts = {}
    total = 0
    for name, value in dimensions.items():
        conclusions = value.get('conclusions', []) if isinstance(value, dict) else []
        counts[name] = len(conclusions)
        total += len(conclusions)
    return {
        'last_updated': profile.get('last_updated', '') if isinstance(profile, dict) else '',
        'dimensions_count': len(dimensions),
        'conclusions_count': total,
        'conclusions_by_dimension': counts,
    }


def main():
    args = parse_args()
    work_dir = _runtime_dir(args.config)
    state_path = _state_path(args.config, args.state)
    preferences_dir = os.path.join(work_dir, 'preferences')
    profile_path = os.path.join(work_dir, 'profile', 'servasyy_profile.json')
    db_path = os.path.join(work_dir, 'assistant.db')
    today = datetime.now(tz=_TZ8).date().isoformat()

    sm = StateManager(state_path)
    preference_state = sm.get_preference_state()
    user_status, user_context = sm.infer_user_status()

    files, per_file, preferences, writing_samples = _load_archives(preferences_dir, args.days)
    sampled_preferences = _sample_evenly(preferences, args.max_preferences)
    sampled_samples = _sample_evenly(writing_samples, args.max_writing_samples)
    existing_profile = _load_json(profile_path, {})

    already_done = preference_state.get('last_run_date') == today
    has_data = bool(preferences or writing_samples)
    should_run = bool(args.force or (not already_done and has_data))
    skip_reason = ''
    if not should_run:
        if already_done and not args.force:
            skip_reason = 'already_ran_today'
        elif not has_data:
            skip_reason = 'no_archived_preferences'

    output = {
        'should_run': should_run,
        'skip_reason': skip_reason,
        'today': today,
        'paths': {
            'work_dir': work_dir,
            'state_path': state_path,
            'preferences_dir': preferences_dir,
            'profile_path': profile_path,
            'assistant_db': db_path,
            'final_profile_input': '/tmp/wechat_preference_profile.json',
        },
        'user_status': {'status': user_status, 'context': user_context},
        'preference_state': preference_state,
        'stats': {
            'days': args.days,
            'files_count': len(files),
            'preference_count': len(preferences),
            'preference_sample_count': len(sampled_preferences),
            'category_counts': _category_counts(preferences),
            'writing_samples_total': len(writing_samples),
            'writing_samples_count': len(sampled_samples),
            'existing_profile': _profile_summary(existing_profile),
        },
        'files': per_file,
        'preferences': _compact_preferences(sampled_preferences, args.max_content_chars),
        'writing_samples': _compact_samples(sampled_samples, args.max_content_chars),
        'existing_profile': existing_profile,
        'feedback_stats': _feedback_stats(db_path),
        'required_dimensions': _DEFAULT_DIMENSIONS,
        'rules': [
            '保留 existing_profile 中仍然有效的结论，不要重写成全新画像。',
            '每次最多新增 5 条真正有新信息的 conclusions。',
            '新增或更新 conclusions 必须包含 confidence、source_count、evidence、first_seen、last_seen。',
            '输出画像 JSON 时必须是完整 JSON 对象，不要 Markdown。',
        ],
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
