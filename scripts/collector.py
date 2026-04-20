#!/usr/bin/env python3
"""
collector.py — 微信消息采集转存（一次性同步命令）
逻辑：读 decrypted/ 下已解密的 message DB → 增量转存到 collector.db
用法：
  python3 collector.py --config config.yaml --sync                    # 同步所有 watched_chats
  python3 collector.py --config config.yaml --sync --chatroom ID      # 同步单个群
"""
import os, sys, json, time, glob, sqlite3, hashlib, re, argparse
import xml.etree.ElementTree as _ET
from datetime import datetime, timezone, timedelta

_TZ8 = timezone(timedelta(hours=8))


# ═══════════════════════════════════════════════════════════
# 配置加载（复用 decrypt/config.py）
# ═══════════════════════════════════════════════════════════
def _load_config(path):
    """加载配置，复用 decrypt/config.py 的 load_config"""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'decrypt'))
    from config import load_config
    return load_config(path)


def parse_args():
    parser = argparse.ArgumentParser(description='微信消息采集器')
    parser.add_argument('--config', required=True, help='YAML 配置文件路径')
    parser.add_argument('--sync', action='store_true', help='执行一次增量同步后退出')
    parser.add_argument('--chatroom', help='只同步指定的 chatroom_id')
    parser.add_argument('--discover', action='store_true', help='扫描解密DB，自动发现并注册所有群/私聊到 watched_chats')
    parser.add_argument('--recent-hours', type=int, default=24,
                        help='只同步最近N小时内有消息的会话（默认24h，0=全量）')
    return parser.parse_args()


# ═══════════════════════════════════════════════════════════
# 全局变量（main 入口初始化）
# ═══════════════════════════════════════════════════════════
COLLECTOR_DB = ''
CONTACT_DB = ''
MSG_DIR = ''
SELF_WXID = ''


# ═══════════════════════════════════════════════════════════
# 联系人名称缓存
# ═══════════════════════════════════════════════════════════
_names = {}


def load_names():
    global _names
    try:
        with sqlite3.connect(CONTACT_DB) as conn:
            conn.text_factory = lambda b: b.decode('utf-8', errors='replace')
            for r in conn.execute("SELECT username, nick_name, remark FROM contact WHERE username != ''"):
                u, n, rk = r
                _names[u] = (rk or '').strip() or (n or '').strip() or u
        print(f'[names] {len(_names)} loaded')
    except Exception as e:
        print(f'[names] {e}')


def get_name(uid):
    return _names.get(uid, uid)


# ═══════════════════════════════════════════════════════════
# 解压 zstd 内容
# ═══════════════════════════════════════════════════════════
try:
    import zstandard as zstd
    _dctx = zstd.ZstdDecompressor()

    def decomp(data):
        try:
            if isinstance(data, (bytes, bytearray)) and len(data) > 4:
                return _dctx.decompress(data, max_output_size=1048576).decode('utf-8', errors='replace')
        except Exception:
            pass
        return data.decode('utf-8', errors='replace') if isinstance(data, (bytes, bytearray)) else str(data or '')
except ImportError:
    def decomp(data):
        return data.decode('utf-8', errors='replace') if isinstance(data, (bytes, bytearray)) else str(data or '')


# ═══════════════════════════════════════════════════════════
# collector.db 初始化
# ═══════════════════════════════════════════════════════════
def init_db():
    with sqlite3.connect(COLLECTOR_DB) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chatroom_id TEXT NOT NULL,
                sender      TEXT,
                content     TEXT,
                msg_time    INTEGER,
                local_id    TEXT,
                msg_type    INTEGER DEFAULT 1,
                UNIQUE(chatroom_id, local_id)
            );
            CREATE INDEX IF NOT EXISTS idx_chat_time ON messages(chatroom_id, msg_time DESC);
            CREATE INDEX IF NOT EXISTS idx_chat_local ON messages(chatroom_id, local_id);
            CREATE TABLE IF NOT EXISTS watched_chats (
                chatroom_id   TEXT PRIMARY KEY,
                chatroom_name TEXT,
                added_at      INTEGER DEFAULT (strftime('%s','now'))
            );
            CREATE INDEX IF NOT EXISTS idx_watched_name ON watched_chats(chatroom_name);
            CREATE TABLE IF NOT EXISTS sync_state (
                chatroom_id   TEXT PRIMARY KEY,
                last_local_id TEXT DEFAULT '0',
                last_sync_at  INTEGER DEFAULT 0
            );
        """)
    print(f'[db] {COLLECTOR_DB}')


# ═══════════════════════════════════════════════════════════
# 找消息表（微信 4.x: Msg_{MD5(chatroom_id)}0）
# ═══════════════════════════════════════════════════════════
_table_cache = {}
_TABLE_CACHE_TTL = 300

def _chatroom_to_table(chatroom_id):
    md5 = hashlib.md5(chatroom_id.encode('utf-8')).hexdigest()
    return f"Msg_{md5}"

def _build_full_table_cache(watched_set):
    """一次扫描所有 message DB，预建 table_cache。
    返回 {chatroom_id: (db_path, table_name)} 映射。
    把 O(chats × DBs) 变成 O(DBs × tables_per_db)。
    """
    global _table_cache
    # 建立期望的 hash → chatroom_id 反向映射
    expected_hashes = {}
    for cid in watched_set:
        h = _chatroom_to_table(cid)
        expected_hashes[h] = cid

    found = 0
    db_files = sorted(glob.glob(os.path.join(MSG_DIR, '*.db')))
    for db_file in db_files:
        if db_file.endswith('-wal') or db_file.endswith('-shm'):
            continue
        try:
            conn = sqlite3.connect(f'file:{db_file}?mode=ro', uri=True)
            try:
                tables = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
                ).fetchall()
                for (tname,) in tables:
                    # tname = "Msg_{hash}"
                    if tname in expected_hashes:
                        cid = expected_hashes[tname]
                        _table_cache[cid] = (db_file, tname)
                        found += 1
            finally:
                conn.close()
        except Exception as e:
            print(f'[cache] {os.path.basename(db_file)}: {e}')

    print(f'[cache] 预建缓存: {found}/{len(watched_set)} 个会话有消息表（扫描 {len(db_files)} 个DB）')
    return found

def find_msg_table(chatroom_id):
    if chatroom_id in _table_cache:
        return _table_cache[chatroom_id]

    # fallback: 逐DB查找（兼容 --chatroom 单独同步）
    expected = _chatroom_to_table(chatroom_id)

    for db_file in sorted(glob.glob(os.path.join(MSG_DIR, '*.db'))):
        if db_file.endswith('-wal') or db_file.endswith('-shm'):
            continue
        try:
            conn = sqlite3.connect(f'file:{db_file}?mode=ro', uri=True)
            try:
                row = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (expected,)
                ).fetchone()
                if row:
                    cnt = conn.execute(f"SELECT COUNT(*) FROM [{expected}]").fetchone()[0]
                    if cnt > 0:
                        _table_cache[chatroom_id] = (db_file, expected)
                        return db_file, expected
            finally:
                conn.close()
        except Exception as e:
            print(f'[find_msg_table] {os.path.basename(db_file)}: {e}')

    _table_cache[chatroom_id] = (None, None)
    return None, None


# ═══════════════════════════════════════════════════════════
# Name2Id: real_sender_id → wxid 映射
# ═══════════════════════════════════════════════════════════
_n2id_cache = {}
_n2id_cache_ts = {}


def _load_name2id(db_path):
    now = time.time()
    if db_path in _n2id_cache and now - _n2id_cache_ts.get(db_path, 0) < _TABLE_CACHE_TTL:
        return _n2id_cache[db_path]
    mapping = {}
    try:
        conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
        conn.text_factory = lambda b: b.decode('utf-8', errors='replace')
        try:
            has_n2id = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='Name2Id'"
            ).fetchone()
            if has_n2id:
                for row in conn.execute("SELECT rowid, user_name FROM Name2Id"):
                    mapping[row[0]] = row[1]
        finally:
            conn.close()
    except Exception as e:
        print(f'[n2id] load failed {os.path.basename(db_path)}: {e}')
    _n2id_cache[db_path] = mapping
    _n2id_cache_ts[db_path] = now
    return mapping


# ═══════════════════════════════════════════════════════════
# 统一 sender 解析
# ═══════════════════════════════════════════════════════════
def _resolve_sender(raw_sender, name2id, is_dm, chatroom_id):
    if raw_sender is None:
        return '' if is_dm else get_name(chatroom_id)

    try:
        sid = int(raw_sender)
        wxid = name2id.get(sid, '') if name2id else ''
    except (ValueError, TypeError):
        wxid = str(raw_sender)

    if wxid == SELF_WXID:
        return '__self__'
    if wxid and not wxid.isdigit():
        name = get_name(wxid)
        if name and not name.startswith('<?xml') and not name.startswith('<msg'):
            return name

    if is_dm:
        return get_name(chatroom_id)
    return get_name(chatroom_id) if not wxid else get_name(wxid)


# ═══════════════════════════════════════════════════════════
# 增量同步单个群
# ═══════════════════════════════════════════════════════════
def sync_one(chatroom_id, last_local_id='0'):
    db_path, table = find_msg_table(chatroom_id)
    if not db_path:
        return 0, last_local_id

    try:
        conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
        conn.text_factory = lambda b: b.decode('utf-8', errors='replace')
        try:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            id_col = next((c for c in ['local_id', 'MsgLocalID', 'rowid'] if c in cols), 'rowid')
            time_col = next((c for c in ['create_time', 'msg_time', 'CreateTime'] if c in cols), None)
            sender_col = next((c for c in ['real_sender_id', 'sender', 'StrTalker'] if c in cols), None)
            content_col = next((c for c in ['message_content', 'compress_content', 'content', 'Content'] if c in cols), None)
            type_col = next((c for c in ['local_type', 'MsgType', 'Type'] if c in cols), None)
            status_col = next((c for c in ['status'] if c in cols), None)
            if not content_col:
                return 0, last_local_id
            sel = (
                f"SELECT {id_col},{sender_col or 'NULL'},{content_col},{time_col or '0'},"
                f"{type_col or '1'},{status_col or '0'} "
                f"FROM {table} WHERE CAST({id_col} AS INTEGER) > CAST(? AS INTEGER) "
                f"ORDER BY CAST({id_col} AS INTEGER) ASC LIMIT 2000"
            )
            rows = conn.execute(sel, (str(last_local_id),)).fetchall()
        finally:
            conn.close()
    except Exception as e:
        print(f'[sync] {chatroom_id}: {e}')
        return 0, last_local_id

    name2id = _load_name2id(db_path)
    if not rows:
        return 0, last_local_id

    inserted = 0
    new_lid = last_local_id
    coll = sqlite3.connect(COLLECTOR_DB, timeout=30)
    try:
        coll.execute("PRAGMA journal_mode=WAL")
        coll.execute("PRAGMA busy_timeout=30000")
    except Exception:
        pass

    try:
        coll.execute("BEGIN")
        is_dm = '@chatroom' not in chatroom_id and '@im.chatroom' not in chatroom_id
        for row in rows:
            lid, _raw_sender, content_raw, msg_time, msg_type = row[0], row[1], row[2], row[3], row[4]

            if isinstance(content_raw, (bytes, bytearray)):
                raw_text = decomp(content_raw)
            else:
                raw_text = str(content_raw or '')

            _bad = raw_text.count('\ufffd')
            _ctrl = sum(1 for c in raw_text[:100] if ord(c) < 32 and c not in '\n\r\t')
            if len(raw_text) > 5 and (_bad / max(len(raw_text), 1) > 0.08 or _ctrl > 5):
                _TYPE_MAP = {
                    1: '[📝 文本]', 3: '[🖼️ 图片]', 34: '[🎤 语音]', 43: '[🎥 视频]',
                    47: '[😄 表情]', 49: '[📎 文件/链接]', 10000: '[💬 系统消息]',
                    10002: '[📋 合并转发]'
                }
                mt = int(msg_type or 1)
                content = _TYPE_MAP.get(mt, f'[📎 消息类型 {mt}]')
            else:
                content = raw_text

            sender = _resolve_sender(_raw_sender, name2id, is_dm, chatroom_id)

            content = content[:2000]
            lid_str = str(lid)
            try:
                coll.execute(
                    "INSERT OR IGNORE INTO messages(chatroom_id,sender,content,msg_time,local_id,msg_type) VALUES(?,?,?,?,?,?)",
                    (chatroom_id, sender, content, int(msg_time or 0), lid_str, int(msg_type or 1))
                )
                changed = coll.execute("SELECT changes()").fetchone()[0]
                if changed:
                    inserted += 1
                    new_lid = lid_str
                else:
                    exists = coll.execute(
                        "SELECT 1 FROM messages WHERE chatroom_id=? AND local_id=?",
                        (chatroom_id, lid_str)
                    ).fetchone()
                    if exists:
                        new_lid = lid_str
                    else:
                        print(f"[sync] write skipped {chatroom_id}:{lid_str}, stopping batch")
                        break
            except sqlite3.Error as e:
                print(f'[sync] write failed {chatroom_id}:{lid_str}: {e}')
                break
    finally:
        try:
            coll.commit()
        except Exception:
            new_lid = last_local_id
        try:
            coll.close()
        except Exception:
            pass
    return inserted, new_lid


# ═══════════════════════════════════════════════════════════
# 公众号 / 系统号过滤
# ═══════════════════════════════════════════════════════════
_FILTER_IDS = {
    'brandservicesessionholder', 'brandsessionholder', 'notifymessage',
    'floatbottle', 'fmessage', 'weixin', 'qqmail', 'qmessage', 'tmessage',
    'medianote', 'voipnotify', 'voipmsg', 'weixiread', 'wxid_exporter',
    'mphelper', 'newsapp',
}


def _is_spam(uid):
    if not uid:
        return True
    if uid.startswith('@'):
        return True
    if uid.startswith('gh_'):
        return True
    if uid in _FILTER_IDS:
        return True
    return False


# ═══════════════════════════════════════════════════════════
# 自动发现所有群/私聊
# ═══════════════════════════════════════════════════════════
def discover_chatrooms():
    """扫描 session.db 和 contact.db，发现所有 chatroom_id 并注册到 watched_chats。"""
    discovered = set()

    # 从 session.db 获取所有会话
    dec_root = os.path.dirname(MSG_DIR)
    session_db = os.path.join(dec_root, 'session', 'session.db')
    if os.path.exists(session_db):
        try:
            conn = sqlite3.connect(f'file:{session_db}?mode=ro', uri=True)
            conn.text_factory = lambda b: b.decode('utf-8', errors='replace')
            # session 表通常有 username 字段
            for tbl in ['SessionTable', 'session', 'Session']:
                try:
                    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()]
                    name_col = next((c for c in ['userName', 'username', 'strUsrName'] if c in cols), None)
                    if name_col:
                        rows = conn.execute(f"SELECT {name_col} FROM {tbl}").fetchall()
                        for (uid,) in rows:
                            if uid and not _is_spam(uid):
                                discovered.add(uid)
                        break
                except Exception:
                    continue
            conn.close()
        except Exception as e:
            print(f'[discover] session.db 读取失败: {e}')

    if os.path.exists(CONTACT_DB):
        try:
            conn = sqlite3.connect(f'file:{CONTACT_DB}?mode=ro', uri=True)
            conn.text_factory = lambda b: b.decode('utf-8', errors='replace')
            rows = conn.execute(
                "SELECT username, nick_name, remark FROM contact WHERE username != '' AND username NOT LIKE 'gh_%'"
            ).fetchall()
            conn.close()
            new_names = 0
            new_groups = 0
            for username, nick_name, remark in rows:
                display = (remark or '').strip() or (nick_name or '').strip() or username
                if display != _names.get(username, ''):
                    _names[username] = display
                    new_names += 1
                if username.endswith('@chatroom') and username not in discovered:
                    discovered.add(username)
                    new_groups += 1
            print(f'[discover] contact.db: {new_names} 个名称更新, {new_groups} 个新群')
        except Exception as e:
            print(f'[discover] contact.db 读取失败: {e}')

    if not discovered:
        print('[discover] 未发现任何会话，请检查解密是否成功')
        return 0

    # 注册到 watched_chats
    added = 0
    with sqlite3.connect(COLLECTOR_DB, timeout=30) as conn:
        for cid in discovered:
            name = get_name(cid)
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO watched_chats(chatroom_id, chatroom_name) VALUES(?, ?)",
                    (cid, name)
                )
                if conn.execute("SELECT changes()").fetchone()[0]:
                    added += 1
            except Exception:
                pass
        conn.commit()

    total = len(discovered)
    groups = sum(1 for c in discovered if '@chatroom' in c)
    dms = total - groups
    print(f'[discover] 发现 {total} 个会话（{groups} 群 + {dms} 私聊），新注册 {added} 个')
    return added


# ═══════════════════════════════════════════════════════════
# 最近活跃会话过滤（基于 session.db）
# ═══════════════════════════════════════════════════════════
def _get_recent_chats(watched_set, hours=24):
    """查询 session.db，返回最近 N 小时内有消息的 watched chatroom_ids。
    如果 session.db 不可用，返回 None（由调用方决定 fallback）。
    """
    dec_root = os.path.dirname(MSG_DIR)
    session_db = os.path.join(dec_root, 'session', 'session.db')
    if not os.path.exists(session_db):
        return None

    try:
        conn = sqlite3.connect(f'file:{session_db}?mode=ro', uri=True)
        conn.text_factory = lambda b: b.decode('utf-8', errors='replace')
        try:
            # 获取最近活跃的会话
            cutoff = int(time.time()) - hours * 3600
            rows = conn.execute(
                "SELECT username FROM SessionTable WHERE last_timestamp > ?",
                (cutoff,)
            ).fetchall()
            recent = {r[0] for r in rows} & watched_set
            return recent
        finally:
            conn.close()
    except Exception as e:
        print(f'[recent] session.db 读取失败: {e}')
        return None


# ═══════════════════════════════════════════════════════════
# 主同步逻辑
# ═══════════════════════════════════════════════════════════
def run_sync(chatroom_filter=None, auto_discover=True, recent_hours=24):
    """执行一次增量同步。
    chatroom_filter: 非空则只同步该会话。
    auto_discover: watched_chats 为空时自动执行发现。
    recent_hours: 只同步最近 N 小时内有消息的会话（基于 session.db）。
                  0 表示同步全部（全量模式，仅用于首次或调试）。
    """
    with sqlite3.connect(COLLECTOR_DB) as conn:
        watched = conn.execute("SELECT chatroom_id FROM watched_chats").fetchall()

    # 首次运行自动发现（--chatroom 也触发，确保目标在 watched_chats 中）
    if not watched and auto_discover:
        if chatroom_filter:
            with sqlite3.connect(COLLECTOR_DB, timeout=30) as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO watched_chats(chatroom_id, chatroom_name) VALUES(?, ?)",
                    (chatroom_filter, get_name(chatroom_filter))
                )
                conn.commit()
            with sqlite3.connect(COLLECTOR_DB) as conn:
                watched = conn.execute("SELECT chatroom_id FROM watched_chats").fetchall()
        else:
            print('[sync] watched_chats 为空，自动发现会话...')
            discover_chatrooms()
            with sqlite3.connect(COLLECTOR_DB) as conn:
                watched = conn.execute("SELECT chatroom_id FROM watched_chats").fetchall()

    if not watched:
        print('[sync] 没有要同步的会话。请先运行 --discover 或手动添加。')
        return 0

    watched_set = {cid for (cid,) in watched}

    # ── 第1步：预建 table cache（一次扫描所有 DB） ──
    _build_full_table_cache(watched_set)

    # ── 第2步：过滤出最近活跃的会话 ──
    if chatroom_filter:
        # --chatroom 模式：只同步指定的
        sync_set = {chatroom_filter} & watched_set
        if not sync_set:
            sync_set = {chatroom_filter}  # 不在 watched 也要同步
        filter_desc = f'指定: {chatroom_filter}'
    elif recent_hours > 0:
        # 增量模式：只同步最近活跃的
        recent = _get_recent_chats(watched_set, hours=recent_hours)
        if recent is not None:
            sync_set = recent
            filter_desc = f'最近 {recent_hours}h 活跃: {len(sync_set)}/{len(watched_set)}'
        else:
            # session.db 不可用，fallback 到全量
            sync_set = watched_set
            filter_desc = f'全量（session.db 不可用）: {len(sync_set)}'
    else:
        sync_set = watched_set
        filter_desc = f'全量模式: {len(sync_set)}'

    print(f'[sync] {filter_desc}')

    if not sync_set:
        print('[sync] 没有最近活跃的会话需要同步。')
        return 0

    with sqlite3.connect(COLLECTOR_DB) as conn:
        states = dict(conn.execute("SELECT chatroom_id, last_local_id FROM sync_state").fetchall())

    total_inserted = 0
    start_time = time.time()
    sync_list = sorted(sync_set)
    total_chats = len(sync_list)

    for idx, cid in enumerate(sync_list, 1):
        last = states.get(cid, '0')
        chat_total = 0
        chat_name = get_name(cid)[:20]
        prefix = f"[{idx}/{total_chats}] {chat_name:20}"
        while True:
            n, new_lid = sync_one(cid, last)
            progressed = new_lid != last
            if progressed:
                with sqlite3.connect(COLLECTOR_DB, timeout=30) as conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO sync_state(chatroom_id,last_local_id,last_sync_at) VALUES(?,?,strftime('%s','now'))",
                        (cid, new_lid)
                    )
                chat_total += n
                last = new_lid
            if not progressed:
                break
        if chat_total > 0:
            elapsed = time.time() - start_time
            rate = total_inserted / max(elapsed, 1)
            print(f'{prefix} +{chat_total} (总计 {total_inserted + chat_total}, {rate:.0f}/s)')
            total_inserted += chat_total
        elif chatroom_filter:
            db_path, _ = find_msg_table(cid)
            if not db_path:
                print(f'{prefix} 跳过（未找到消息表）')

    elapsed = time.time() - start_time
    rate = total_inserted / max(elapsed, 1)
    print(f'\n[sync] 完成: {total_inserted} 条新消息, {elapsed:.1f}s, {rate:.0f}/s')
    return total_inserted


# ═══════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════
if __name__ == '__main__':
    args = parse_args()

    if not args.sync and not args.discover:
        print("请使用 --sync 或 --discover 参数", file=sys.stderr)
        sys.exit(1)

    cfg = _load_config(args.config)

    COLLECTOR_DB = cfg['collector_db']
    SELF_WXID = cfg.get('self_wxid', '')
    dec = cfg['decrypted_dir']
    MSG_DIR = os.path.join(dec, 'message')  # decrypt_db.py 输出目录
    CONTACT_DB = os.path.join(dec, 'contact', 'contact.db')

    init_db()
    load_names()

    if args.discover:
        discover_chatrooms()

    if args.sync:
        run_sync(chatroom_filter=args.chatroom, recent_hours=args.recent_hours)
