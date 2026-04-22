#!/usr/bin/env python3
"""
archive_preferences.py — 低频增量归档偏好/写作样本，供 todo-scan 顺手触发。

用法：
  python3 archive_preferences.py --config config.yaml
  python3 archive_preferences.py --config config.yaml --min-interval-sec 14400
  python3 archive_preferences.py --config config.yaml --force
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from extract_preferences import (
    _PREFERENCE_BOOTSTRAP_LOOKBACK_HOURS,
    _PREFERENCE_WINDOW_OVERLAP_SECONDS,
    _TZ8,
    get_all_self_messages,
    get_preference_messages,
    load_config,
)
from state_manager import StateManager


def parse_args():
    parser = argparse.ArgumentParser(description='低频增量归档偏好/写作样本')
    parser.add_argument('--config', required=True, help='YAML 配置文件路径')
    parser.add_argument('--state', help='scan_state.json 路径（默认从 config 推导）')
    parser.add_argument('--min-interval-sec', type=int, default=4 * 3600,
                        help='距离上次归档的最小间隔秒数，默认 4 小时')
    parser.add_argument('--force', action='store_true', help='忽略最小间隔，强制归档一次')
    return parser.parse_args()


def get_state_path(config_path, state_arg=None):
    if state_arg:
        return state_arg
    config_dir = os.path.dirname(os.path.abspath(config_path))
    return os.path.join(config_dir, 'scan_state.json')


def get_preferences_dir(config_path):
    config_dir = os.path.dirname(os.path.abspath(config_path))
    return os.path.join(config_dir, 'preferences')


def build_scan_window(sm, now):
    last_scan_ts = sm.get_preference_last_scan_ts()
    if last_scan_ts > 0:
        ts_start = max(0, last_scan_ts - _PREFERENCE_WINDOW_OVERLAP_SECONDS)
    else:
        ts_start = int((now - timedelta(hours=_PREFERENCE_BOOTSTRAP_LOOKBACK_HOURS)).timestamp())
    ts_end = int(now.timestamp())
    if ts_start >= ts_end:
        ts_start = max(0, ts_end - _PREFERENCE_WINDOW_OVERLAP_SECONDS)
    return ts_start, ts_end


def _dt_from_ts(ts):
    return datetime.fromtimestamp(ts, tz=_TZ8)


def _scan_window_dict(ts_start, ts_end):
    return {
        'start_ts': ts_start,
        'end_ts': ts_end,
        'start_time': _dt_from_ts(ts_start).strftime('%Y-%m-%d %H:%M:%S'),
        'end_time': _dt_from_ts(ts_end).strftime('%Y-%m-%d %H:%M:%S'),
    }


def _load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    return default


def _dedupe_preferences(existing, incoming):
    seen = {
        (item.get('chatroom_id', ''), item.get('msg_time', 0), item.get('content', ''))
        for item in existing
    }
    added = 0
    for item in sorted(incoming, key=lambda x: x.get('msg_time', 0)):
        key = (item.get('chatroom_id', ''), item.get('msg_time', 0), item.get('content', ''))
        if key in seen:
            continue
        existing.append(item)
        seen.add(key)
        added += 1
    existing.sort(key=lambda x: x.get('msg_time', 0))
    return added


def _load_sample_meta(existing_doc):
    meta = existing_doc.get('writing_samples_meta', [])
    if meta:
        return meta
    samples = []
    for content in existing_doc.get('writing_samples', []):
        samples.append({'content': content, 'msg_time': 0, 'time': ''})
    return samples


def _dedupe_writing_samples(existing_doc, incoming_rows):
    existing_meta = _load_sample_meta(existing_doc)
    seen = {(item.get('msg_time', 0), item.get('content', '')) for item in existing_meta}
    added = 0
    for content, msg_time in sorted(incoming_rows, key=lambda x: x[1]):
        key = (msg_time, content)
        if key in seen:
            continue
        existing_meta.append({
            'content': content,
            'msg_time': msg_time,
            'time': _dt_from_ts(msg_time).strftime('%Y-%m-%d %H:%M'),
        })
        seen.add(key)
        added += 1
    existing_meta.sort(key=lambda x: x.get('msg_time', 0))
    existing_doc['writing_samples_meta'] = existing_meta
    existing_doc['writing_samples'] = [item.get('content', '') for item in existing_meta]
    return added


def _rebuild_stats(preferences, writing_samples):
    cat_counts = defaultdict(int)
    for item in preferences:
        for category in item.get('categories', []):
            cat_counts[category] += 1
    return {
        'preference_count': len(preferences),
        'category_counts': dict(cat_counts),
        'writing_samples_count': len(writing_samples),
    }


def _group_preferences_by_day(preferences):
    grouped = defaultdict(list)
    for item in preferences:
        day = _dt_from_ts(item.get('msg_time', 0)).strftime('%Y-%m-%d')
        grouped[day].append(item)
    return grouped


def _group_samples_by_day(sample_rows):
    grouped = defaultdict(list)
    for content, msg_time in sample_rows:
        day = _dt_from_ts(msg_time).strftime('%Y-%m-%d')
        grouped[day].append((content, msg_time))
    return grouped


def main():
    args = parse_args()
    cfg = load_config(args.config)
    collector_db = cfg['collector_db']
    self_wxid = cfg.get('self_wxid', '')
    state_path = get_state_path(args.config, args.state)
    preferences_dir = get_preferences_dir(args.config)
    os.makedirs(preferences_dir, exist_ok=True)

    sm = StateManager(state_path)
    pref_state = sm.get_preference_state()
    now = datetime.now(tz=_TZ8)
    now_ts = int(now.timestamp())

    last_archive_ts = pref_state.get('last_archive_ts', 0)
    if (not args.force and last_archive_ts > 0 and
            now_ts - last_archive_ts < max(args.min_interval_sec, 0)):
        due_in = max(args.min_interval_sec, 0) - (now_ts - last_archive_ts)
        print(json.dumps({
            'ran': False,
            'skipped': True,
            'reason': 'interval_not_reached',
            'state_path': state_path,
            'last_archive_time': _dt_from_ts(last_archive_ts).strftime('%Y-%m-%d %H:%M:%S'),
            'next_due_in_sec': due_in,
            'min_interval_sec': args.min_interval_sec,
        }, ensure_ascii=False, indent=2))
        return

    last_scan_ts = pref_state.get('last_scan_ts', 0)
    if last_scan_ts > 0:
        ts_start = max(0, last_scan_ts - _PREFERENCE_WINDOW_OVERLAP_SECONDS)
    else:
        ts_start = int((now - timedelta(hours=_PREFERENCE_BOOTSTRAP_LOOKBACK_HOURS)).timestamp())
    ts_end = now_ts
    if ts_start >= ts_end:
        ts_start = max(0, ts_end - _PREFERENCE_WINDOW_OVERLAP_SECONDS)

    preferences = get_preference_messages(collector_db, ts_start, ts_end, self_wxid)
    writing_sample_rows = get_all_self_messages(collector_db, ts_start, ts_end, self_wxid, limit=200)

    pref_by_day = _group_preferences_by_day(preferences)
    sample_by_day = _group_samples_by_day(writing_sample_rows)
    updated_dates = sorted(set(pref_by_day) | set(sample_by_day))
    scan_window = _scan_window_dict(ts_start, ts_end)
    summary = []

    for day in updated_dates:
        archive_path = os.path.join(preferences_dir, f'{day}.json')
        doc = _load_json(archive_path, {
            'date': day,
            'mode': 'incremental_archive',
            'preferences': [],
            'writing_samples': [],
            'writing_samples_meta': [],
        })
        pref_added = _dedupe_preferences(doc.setdefault('preferences', []), pref_by_day.get(day, []))
        sample_added = _dedupe_writing_samples(doc, sample_by_day.get(day, []))
        doc['date'] = day
        doc['mode'] = 'incremental_archive'
        doc['ts_start'] = ts_start
        doc['ts_end'] = ts_end
        doc['scan_window'] = scan_window
        doc['scan_time'] = now.strftime('%Y-%m-%d %H:%M')
        doc['last_archive_at'] = now.strftime('%Y-%m-%d %H:%M:%S')
        doc['stats'] = _rebuild_stats(doc['preferences'], doc.get('writing_samples', []))
        with open(archive_path, 'w', encoding='utf-8') as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
        summary.append({
            'date': day,
            'archive_path': archive_path,
            'new_preferences': pref_added,
            'new_writing_samples': sample_added,
            'total_preferences': len(doc['preferences']),
            'total_writing_samples': len(doc.get('writing_samples', [])),
        })

    sm.update_preference_archive(last_scan_ts=ts_end, last_archive_ts=now_ts)

    print(json.dumps({
        'ran': True,
        'skipped': False,
        'mode': 'incremental_archive',
        'state_path': state_path,
        'scan_window': scan_window,
        'updated_dates': summary,
        'stats': {
            'preferences_found': len(preferences),
            'writing_samples_found': len(writing_sample_rows),
            'files_updated': len(summary),
        },
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
