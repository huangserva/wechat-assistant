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

    # ─── Acknowledged（已读确认）─────────────────────────────

    def ack_todo(self, todo_id):
        """标记某个 todo 为已读（acknowledged=true）"""
        state = self._read()
        for item in state['todos']['items']:
            if item.get('id') == todo_id and not item.get('acknowledged'):
                item['acknowledged'] = True
                item['ack_time'] = datetime.now(tz=_TZ8).isoformat()
                self._write(state)
                return True
        return False

    def ack_all_todos(self):
        """标记所有 open todo 为已读"""
        state = self._read()
        count = 0
        for item in state['todos']['items']:
            if item.get('status') == 'open' and not item.get('acknowledged'):
                item['acknowledged'] = True
                item['ack_time'] = datetime.now(tz=_TZ8).isoformat()
                count += 1
        if count > 0:
            self._write(state)
        return count

    def auto_ack_old_todos(self, hours=2):
        """自动确认推送超过 N 小时仍未 resolve 的 todo 为已读"""
        state = self._read()
        cutoff = datetime.now(tz=_TZ8) - timedelta(hours=hours)
        count = 0
        for item in state['todos']['items']:
            if item.get('status') != 'open':
                continue
            if item.get('acknowledged'):
                continue
            # 用 created 时间判断：如果创建时间距今超过 hours 小时，说明已经推送过了
            created_str = item.get('created', '')
            if created_str:
                try:
                    created = datetime.fromisoformat(created_str)
                    if created < cutoff:
                        item['acknowledged'] = True
                        item['ack_time'] = datetime.now(tz=_TZ8).isoformat()
                        count += 1
                except (ValueError, TypeError):
                    pass
        if count > 0:
            self._write(state)
        return count

    # ─── User State（用户状态感知）────────────────────────────

    _USER_STATE_DEFAULT = {
        'current': {
            'status': 'idle',
            'context': '',
            'last_active': '',
            'active_todos': 0,
            'urgent_unresolved': 0,
            'source': 'inferred',
        },
        'schedule': {
            'working_hours': '09:00-23:00',
            'sleep_hours': '23:00-08:00',
            'timezone': 'Asia/Shanghai',
        },
        'patterns': {
            'avg_response_time_min': 0,
            'peak_active_hours': ['09:00-12:00', '14:00-18:00', '20:00-23:00'],
            'ignore_rate_last_7d': 0.0,
            'last_updated': '',
        },
        'feedback_stats': {
            'total_pushed': 0,
            'total_acted': 0,
            'total_ignored': 0,
            'total_snoozed': 0,
            'by_type': {},
        },
    }

    def _user_state_path(self):
        """user_state.json 和 scan_state.json 同目录"""
        return os.path.join(os.path.dirname(self.state_path), 'user_state.json')

    def get_user_state(self):
        """读取用户状态，不存在则返回默认"""
        path = self._user_state_path()
        if not os.path.exists(path):
            return json.loads(json.dumps(self._USER_STATE_DEFAULT))
        try:
            with open(path, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return json.loads(json.dumps(self._USER_STATE_DEFAULT))

    def update_user_state(self, updates):
        """局部更新 user_state（合并到现有 state）"""
        path = self._user_state_path()
        state = self.get_user_state()
        # 深度合并
        def _deep_merge(base, override):
            for k, v in override.items():
                if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                    _deep_merge(base[k], v)
                else:
                    base[k] = v
        _deep_merge(state, updates)
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            json.dump(state, f, ensure_ascii=False, indent=2)
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        os.replace(tmp, path)
        return state

    def infer_user_status(self):
        """推断用户当前状态，返回 (status, context)"""
        state = self._read()
        now = datetime.now(tz=_TZ8)
        now_time = now.strftime('%H:%M')

        # 读 user_state 的 schedule
        user_state = self.get_user_state()
        sleep_hours = user_state.get('schedule', {}).get('sleep_hours', '23:00-08:00')
        sleep_start, sleep_end = sleep_hours.split('-')

        # 1. 睡眠时间
        if sleep_start <= now_time or now_time < sleep_end:
            return 'sleeping', f'睡眠时间（{sleep_hours}）'

        # 2. 有 urgent 未解决
        todos = state.get('todos', {}).get('items', [])
        urgent_open = [t for t in todos if t.get('status') == 'open' and t.get('urgent')]
        if urgent_open:
            return 'busy', f'{len(urgent_open)}个紧急待办未处理'

        # 3. 最近30分钟有消息活动（检查 collector.db）
        try:
            import sqlite3
            db_dir = os.path.dirname(self.state_path)
            collector_db = os.path.join(db_dir, 'collector.db')
            if os.path.exists(collector_db):
                conn = sqlite3.connect(collector_db)
                cutoff = int(now.timestamp()) - 1800
                row = conn.execute(
                    'SELECT COUNT(*) FROM messages WHERE msg_time > ?', (cutoff,)
                ).fetchone()
                conn.close()
                if row[0] > 20:
                    return 'busy', f'消息活跃（最近30分钟{row[0]}条）'
                elif row[0] > 0:
                    return 'working', f'在线（最近30分钟{row[0]}条消息）'
        except Exception:
            pass

        return 'idle', '无活跃活动'

    def set_user_status(self, status, context='', source='user_set'):
        """手动设置用户状态。

        Args:
            status: 状态值 (online/offline/busy/idle/working 等)
            context: 状态描述/上下文信息
            source: 状态来源，默认 'user_set'；传 'inferred' 则视为推断

        Returns:
            更新后的完整 state 字典
        """
        from datetime import datetime, timezone

        state = self._read()
        current = state.setdefault('current', {})

        now = datetime.now(timezone.utc).isoformat()
        current['status'] = status
        current['context'] = context
        current['source'] = source
        current['last_active'] = now

        # 如果是手动设置（非 inferred），记录 manual_set_at 时间戳
        if source != 'inferred':
            current['manual_set_at'] = now
        else:
            current.pop('manual_set_at', None)

        self._write(state)
        return state

    def check_manual_status_expiry(self, hours=4):
        """检查手动设置的状态是否已过期。

        如果 current.source == 'user_set' 且 manual_set_at 距今超过 hours 小时，
        则恢复为 inferred 模式并重新推断真实状态。

        Args:
            hours: 过期时长（小时），默认 4 小时

        Returns:
            (expired: bool, new_status: str, new_context: str)
        """
        from datetime import datetime, timezone, timedelta

        state = self._read()
        current = state.get('current', {})
        source = current.get('source', 'inferred')
        manual_set_at_str = current.get('manual_set_at')

        # 仅当来源为 user_set 且存在 manual_set_at 时才检查
        if source != 'user_set' or not manual_set_at_str:
            return False, current.get('status', 'idle'), current.get('context', '')

        try:
            manual_set_at = datetime.fromisoformat(manual_set_at_str)
            if manual_set_at.tzinfo is None:
                manual_set_at = manual_set_at.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            if now - manual_set_at <= timedelta(hours=hours):
                # 尚未过期
                return False, current.get('status', 'idle'), current.get('context', '')
        except (ValueError, TypeError):
            # 时间戳解析失败，视为需要重新推断
            pass

        # 已过期 → 恢复 inferred 模式，重新推断
        new_status, new_context = self.infer_user_status()
        current['status'] = new_status
        current['context'] = new_context
        current['source'] = 'inferred'
        current.pop('manual_set_at', None)
        current['last_active'] = datetime.now(timezone.utc).isoformat()
        self._write(state)

        return True, new_status, new_context

    # ─── General ─────────────────────────────────────────────

    def get_full_state(self):
        return self._read()

    def write_full_state(self, state):
        self._write(state)
