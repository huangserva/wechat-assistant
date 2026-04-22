#!/usr/bin/env python3
"""
prepare_runtime.py — 共享微信运行时准备步骤，避免多个 cron 重复 refresh + sync。

策略：
1. 用文件锁串行化 refresh/sync，避免并发重复跑
2. 在 freshness 窗口内复用最近一次成功结果
3. refresh 真跑了以后，强制再跑一次 sync，确保 collector.db 跟上

用法：
  python3 prepare_runtime.py --config config.yaml
  python3 prepare_runtime.py --config config.yaml --refresh-max-age-sec 180 --sync-max-age-sec 180
"""
import argparse
import json
import os
import subprocess
import sys
import time
import fcntl
from datetime import datetime, timezone, timedelta

_TZ8 = timezone(timedelta(hours=8))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def parse_args():
    parser = argparse.ArgumentParser(description='共享微信 refresh + sync 运行时准备')
    parser.add_argument('--config', required=True, help='YAML 配置文件路径')
    parser.add_argument('--refresh-max-age-sec', type=int, default=300,
                        help='距离上次成功 refresh 的最大复用秒数')
    parser.add_argument('--sync-max-age-sec', type=int, default=300,
                        help='距离上次成功 sync 的最大复用秒数')
    parser.add_argument('--force-refresh', action='store_true', help='强制 refresh，不复用缓存')
    parser.add_argument('--force-sync', action='store_true', help='强制 sync，不复用缓存')
    return parser.parse_args()


def _config_dir(config_path):
    return os.path.dirname(os.path.abspath(config_path))


def _state_path(config_path):
    return os.path.join(_config_dir(config_path), 'runtime_prepare_state.json')


def _lock_path(config_path):
    return os.path.join(_config_dir(config_path), '.prepare_runtime.lock')


def _load_state(path):
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {
        'refresh': {'success_ts': 0, 'success_at': ''},
        'sync': {'success_ts': 0, 'success_at': ''},
    }


def _save_state(path, state):
    tmp_path = path + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def _fmt_age(age_sec):
    if age_sec < 60:
        return f'{age_sec}s'
    return f'{age_sec // 60}m{age_sec % 60:02d}s'


def _print_stream(label, text, stream):
    text = (text or '').strip()
    if not text:
        return
    for line in text.splitlines():
        print(line, file=stream)


def _run_step(name, cmd):
    started_at = time.time()
    proc = subprocess.run(
        cmd,
        cwd=SCRIPT_DIR,
        capture_output=True,
        text=True,
    )
    _print_stream(name, proc.stdout, sys.stdout)
    _print_stream(name, proc.stderr, sys.stderr)
    elapsed = time.time() - started_at
    print(f'[prepare] {name} finished in {elapsed:.1f}s (exit={proc.returncode})')
    return proc.returncode


def main():
    args = parse_args()
    state_path = _state_path(args.config)
    lock_path = _lock_path(args.config)
    os.makedirs(os.path.dirname(state_path) or '.', exist_ok=True)

    with open(lock_path, 'a+', encoding='utf-8') as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)

        state = _load_state(state_path)
        now_ts = int(time.time())

        refresh_state = state.get('refresh', {})
        refresh_age = now_ts - int(refresh_state.get('success_ts', 0) or 0)
        refresh_fresh = (
            not args.force_refresh and
            refresh_state.get('success_ts', 0) and
            refresh_age <= max(args.refresh_max_age_sec, 0)
        )

        refresh_ran = False
        if refresh_fresh:
            print(f'[prepare] reuse refresh (age={_fmt_age(refresh_age)} <= {args.refresh_max_age_sec}s)')
        else:
            rc = _run_step('refresh', [sys.executable, 'refresh_decrypt.py', '--config', args.config])
            if rc != 0:
                sys.exit(rc)
            refresh_ran = True
            now_ts = int(time.time())
            refresh_state = {
                'success_ts': now_ts,
                'success_at': datetime.now(tz=_TZ8).strftime('%Y-%m-%d %H:%M:%S'),
            }
            state['refresh'] = refresh_state
            _save_state(state_path, state)

        now_ts = int(time.time())
        sync_state = state.get('sync', {})
        sync_age = now_ts - int(sync_state.get('success_ts', 0) or 0)
        sync_fresh = (
            not refresh_ran and
            not args.force_sync and
            sync_state.get('success_ts', 0) and
            sync_age <= max(args.sync_max_age_sec, 0)
        )

        if sync_fresh:
            print(f'[prepare] reuse sync (age={_fmt_age(sync_age)} <= {args.sync_max_age_sec}s)')
        else:
            rc = _run_step('sync', [sys.executable, 'collector.py', '--config', args.config, '--sync'])
            if rc != 0:
                sys.exit(rc)
            now_ts = int(time.time())
            state['sync'] = {
                'success_ts': now_ts,
                'success_at': datetime.now(tz=_TZ8).strftime('%Y-%m-%d %H:%M:%S'),
            }
            _save_state(state_path, state)

        refresh_at = state.get('refresh', {}).get('success_at', '') or '-'
        sync_at = state.get('sync', {}).get('success_at', '') or '-'
        print(f'[prepare] ready | refresh_at={refresh_at} | sync_at={sync_at}')


if __name__ == '__main__':
    main()
