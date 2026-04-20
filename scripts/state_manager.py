#!/usr/bin/env python3
"""
state_manager.py — 统一的 scan_state.json 读写模块

所有 extract 脚本共享这个 state 文件，记录已处理的消息/事件/待办。
"""
import json
import os
import fcntl
from datetime import datetime, timezone, timedelta

_TZ8 = timezone(timedelta(hours=8))


class StateManager:
    """统一状态管理器，支持文件锁防止并发写入冲突"""

    def __init__(self, state_path):
        self.state_path = state_path

    def _read(self):
        """读取 state，不存在则返回初始结构"""
        if not os.path.exists(self.state_path):
            return {
                'todos': {'items': [], 'last_scan_ts': 0},
                'calendar': {'items': [], 'last_scan_ts': 0},
                'digest': {'daily_done': ''},
                'trending': {'items': [], 'last_scan_ts': 0, 'daily_done': ''},
                'tech': {'daily_done': ''},
                'insight': {'last_run_date': ''},
                'preference': {'last_run_date': '', 'run_count': 0},
            }
        try:
            with open(self.state_path, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {
                'todos': {'items': [], 'last_scan_ts': 0},
                'calendar': {'items': [], 'last_scan_ts': 0},
                'digest': {'daily_done': ''},
                'trending': {'items': [], 'last_scan_ts': 0, 'daily_done': ''},
                'tech': {'daily_done': ''},
                'insight': {'last_run_date': ''},
                'preference': {'last_run_date': '', 'run_count': 0},
            }

    def _write(self, state):
        """原子写入 state（先写临时文件再 rename）"""
        tmp = self.state_path + '.tmp'
        with open(tmp, 'w') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            json.dump(state, f, ensure_ascii=False, indent=2)
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        os.replace(tmp, self.state_path)

    # ─── Todos ───────────────────────────────────────────────

    def get_todos(self):
        """获取所有 todo items"""
        return self._read()['todos']

    def update_todos(self, items, last_scan_ts=None):
        """更新 todo items，可选更新 last_scan_ts"""
        state = self._read()
        state['todos']['items'] = items
        if last_scan_ts is not None:
            state['todos']['last_scan_ts'] = last_scan_ts
        self._write(state)

    def get_todo_last_scan_ts(self):
        return self._read()['todos'].get('last_scan_ts', 0)

    def mark_todo_done(self, todo_id):
        """标记某个 todo 为 done"""
        state = self._read()
        for item in state['todos']['items']:
            if item.get('id') == todo_id and item.get('status') != 'done':
                item['status'] = 'done'
                item['resolved'] = datetime.now(tz=_TZ8).isoformat()
                self._write(state)
                return True
        return False

    def cleanup_old_todos(self, days=7):
        """归档 done 超过 N 天的 todo（从 items 中移除）"""
        state = self._read()
        cutoff = datetime.now(tz=_TZ8) - timedelta(days=days)
        before = len(state['todos']['items'])
        state['todos']['items'] = [
            item for item in state['todos']['items']
            if item.get('status') != 'done' or
               (item.get('resolved') and
                datetime.fromisoformat(item['resolved']) > cutoff)
        ]
        removed = before - len(state['todos']['items'])
        if removed > 0:
            self._write(state)
        return removed

    # ─── Calendar ────────────────────────────────────────────

    def get_calendar(self):
        return self._read()['calendar']

    def update_calendar(self, items, last_scan_ts=None):
        state = self._read()
        state['calendar']['items'] = items
        if last_scan_ts is not None:
            state['calendar']['last_scan_ts'] = last_scan_ts
        self._write(state)

    def get_calendar_last_scan_ts(self):
        return self._read()['calendar'].get('last_scan_ts', 0)

    def update_calendar_status(self, item_id, status):
        """更新某个日历项的状态 (pending → confirmed → expired)"""
        state = self._read()
        for item in state['calendar']['items']:
            if item.get('id') == item_id:
                item['status'] = status
                if status == 'confirmed':
                    item['confirmed_at'] = datetime.now(tz=_TZ8).isoformat()
                self._write(state)
                return True
        return False

    def cleanup_old_calendar(self, days=7):
        """清理过期日历项（expired 或 confirmed 超过 N 天）"""
        state = self._read()
        cutoff = datetime.now(tz=_TZ8) - timedelta(days=days)
        before = len(state['calendar']['items'])
        state['calendar']['items'] = [
            item for item in state['calendar']['items']
            if item.get('status') == 'pending' or
               (item.get('confirmed_at') and
                datetime.fromisoformat(item['confirmed_at']) > cutoff)
        ]
        removed = before - len(state['calendar']['items'])
        if removed > 0:
            self._write(state)
        return removed

    # ─── Digest ──────────────────────────────────────────────

    def get_digest_state(self):
        return self._read()['digest']

    def mark_digest_done(self, date_str):
        state = self._read()
        state['digest']['daily_done'] = date_str
        self._write(state)

    # ─── Trending ────────────────────────────────────────────

    def get_trending(self):
        return self._read()['trending']

    def update_trending(self, items, last_scan_ts=None):
        state = self._read()
        state['trending']['items'] = items
        if last_scan_ts is not None:
            state['trending']['last_scan_ts'] = last_scan_ts
        self._write(state)

    def get_trending_last_scan_ts(self):
        return self._read()['trending'].get('last_scan_ts', 0)

    def mark_trending_daily_done(self, date_str):
        state = self._read()
        state['trending']['daily_done'] = date_str
        self._write(state)

    def cleanup_old_trending(self, days=3):
        """清理 trending items 超过 N 天的"""
        state = self._read()
        cutoff = (datetime.now(tz=_TZ8) - timedelta(days=days)).strftime('%Y-%m-%d')
        before = len(state['trending']['items'])
        state['trending']['items'] = [
            item for item in state['trending']['items']
            if item.get('date', '') >= cutoff
        ]
        removed = before - len(state['trending']['items'])
        if removed > 0:
            self._write(state)
        return removed

    # ─── Tech ────────────────────────────────────────────────

    def get_tech_state(self):
        return self._read()['tech']

    def mark_tech_done(self, date_str):
        state = self._read()
        state['tech']['daily_done'] = date_str
        self._write(state)

    # ─── Preference ─────────────────────────────────────────

    def get_preference_state(self):
        return self._read().get('preference', {'last_run_date': '', 'run_count': 0})

    def mark_preference_done(self, date_str):
        state = self._read()
        if 'preference' not in state:
            state['preference'] = {'last_run_date': '', 'run_count': 0}
        state['preference']['last_run_date'] = date_str
        state['preference']['run_count'] = state['preference'].get('run_count', 0) + 1
        self._write(state)

    # ─── General ─────────────────────────────────────────────

    def get_full_state(self):
        return self._read()

    def write_full_state(self, state):
        self._write(state)
