#!/usr/bin/env python3
"""
finalize_preference_scan.py — 校验并落地 preference-scan 画像结果。

负责：原子写 profile、更新 scan_state、写 assistant.db 快照与 scan_log。
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from state_manager import StateManager

_TZ8 = timezone(timedelta(hours=8))
_REQUIRED_DIMENSIONS = [
    'tech_preferences',
    'business_insights',
    'decision_patterns',
    'communication_style',
    'writing_style',
]


def parse_args():
    parser = argparse.ArgumentParser(description='落地 preference-scan 输出')
    parser.add_argument('--config', required=True, help='运行时 config.yaml 路径')
    parser.add_argument('--profile-input', required=True, help='AI 生成的完整画像 JSON 文件')
    parser.add_argument('--state', help='scan_state.json 路径，默认与 config 同目录')
    parser.add_argument('--scan-log-message', default='', help='写入 scan_log 的简短说明')
    parser.add_argument('--dry-run', action='store_true', help='只校验，不写入')
    return parser.parse_args()


def _runtime_dir(config_path):
    return os.path.dirname(os.path.abspath(config_path))


def _state_path(config_path, state_arg=None):
    if state_arg:
        return state_arg
    return os.path.join(_runtime_dir(config_path), 'scan_state.json')


def _load_json(path):
    with open(path, 'r', encoding='utf-8') as handle:
        return json.load(handle)


def _atomic_write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    os.replace(tmp, path)


def _validate_profile(profile):
    if not isinstance(profile, dict):
        raise ValueError('profile must be a JSON object')
    dimensions = profile.get('dimensions')
    if not isinstance(dimensions, dict):
        raise ValueError('profile.dimensions must be an object')
    for name in _REQUIRED_DIMENSIONS:
        dimensions.setdefault(name, {'conclusions': []})
    for name, value in dimensions.items():
        if not isinstance(value, dict):
            raise ValueError(f'profile.dimensions.{name} must be an object')
        conclusions = value.setdefault('conclusions', [])
        if not isinstance(conclusions, list):
            raise ValueError(f'profile.dimensions.{name}.conclusions must be a list')
    today = datetime.now(tz=_TZ8).date().isoformat()
    profile.setdefault('version', 2)
    profile.setdefault('created', today)
    profile['last_updated'] = today
    history = profile.setdefault('update_history', [])
    if not isinstance(history, list):
        profile['update_history'] = []
    return profile


def _dimension_counts(profile):
    dimensions = profile.get('dimensions', {})
    counts = {}
    total = 0
    for name, value in dimensions.items():
        conclusions = value.get('conclusions', []) if isinstance(value, dict) else []
        counts[name] = len(conclusions)
        total += len(conclusions)
    return counts, total


def _write_scan_log(db_path, message):
    try:
        sys.path.insert(0, SCRIPT_DIR)
        import db_writer
        conn = db_writer._ensure_db(db_path)
        db_writer.write_scan_log(conn, 'preference', 'ok', message)
        conn.close()
        return True
    except Exception:
        return False


def _write_profile_snapshots(db_path, profile):
    try:
        sys.path.insert(0, SCRIPT_DIR)
        import db_writer
        conn = db_writer._ensure_db(db_path)
        items = []
        for dimension, value in profile.get('dimensions', {}).items():
            if isinstance(value, dict):
                items.append({'dimension': dimension, 'conclusions': value.get('conclusions', [])})
        db_writer.write_profile_snapshots(conn, items)
        conn.close()
        return len(items)
    except Exception:
        return 0


def main():
    args = parse_args()
    work_dir = _runtime_dir(args.config)
    profile_path = os.path.join(work_dir, 'profile', 'servasyy_profile.json')
    backup_dir = os.path.join(work_dir, 'profile', '.backups')
    state_path = _state_path(args.config, args.state)
    db_path = os.path.join(work_dir, 'assistant.db')
    today = datetime.now(tz=_TZ8).date().isoformat()

    profile = _validate_profile(_load_json(args.profile_input))
    counts, total = _dimension_counts(profile)

    backup_path = ''
    if not args.dry_run:
        if os.path.exists(profile_path):
            os.makedirs(backup_dir, exist_ok=True)
            stamp = datetime.now(tz=_TZ8).strftime('%Y%m%d_%H%M%S')
            backup_path = os.path.join(backup_dir, f'servasyy_profile.{stamp}.json')
            shutil.copy2(profile_path, backup_path)
        _atomic_write_json(profile_path, profile)
        sm = StateManager(state_path)
        sm.mark_preference_done(today)
        snapshot_count = _write_profile_snapshots(db_path, profile)
        message = args.scan_log_message or f'{len(counts)}维度·{total}结论'
        log_ok = _write_scan_log(db_path, message)
    else:
        snapshot_count = 0
        log_ok = False

    print(json.dumps({
        'ok': True,
        'dry_run': args.dry_run,
        'profile_path': profile_path,
        'backup_path': backup_path,
        'state_path': state_path,
        'last_run_date': today,
        'dimensions_count': len(counts),
        'conclusions_count': total,
        'conclusions_by_dimension': counts,
        'snapshots_written': snapshot_count,
        'scan_log_written': log_ok,
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (OSError, json.JSONDecodeError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
