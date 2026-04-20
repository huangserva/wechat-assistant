#!/usr/bin/env python3
"""
extract_trending.py — 从 collector.db 提取热点事件，输出 JSON（不调 AI）

检测逻辑：
1. 被多个群同时讨论的话题（跨群热度）
2. 被多次转发的 URL/文章（链接热度）
3. 单群内高频出现的关键词（群内热度）
4. 消息量突增的群（活跃度异常）

支持两种模式：
  今日累计模式（默认）：从今天 00:00 到当前时间，每次扫描全天数据
  全量模式（--full）：扫指定日期的整天（原始行为）

用法：
  python3 extract_trending.py --config config.yaml
  python3 extract_trending.py --config config.yaml --full --date 2026-03-12
  python3 extract_trending.py --config config.yaml --top 30
  python3 extract_trending.py --config config.yaml --min-groups 2
  python3 extract_trending.py --config config.yaml --state /path/to/scan_state.json

输出 JSON 到 stdout:
{
  "date": "2026-03-12",
  "mode": "today_cumulative" | "full",
  "scan_window": {"start_ts": ..., "end_ts": ...},
  "cross_group_topics": [...],
  "trending_urls": [...],
  "active_groups": [...],
  "high_freq_keywords": [...],
  "existing_topics": [...],
  "already_done_today": false,
  "scan_state_path": "..."
}
"""
import sqlite3
import json
import os
import sys
import argparse
import re
from datetime import datetime, timezone, timedelta
from collections import Counter, defaultdict
from urllib.parse import urlparse, parse_qs

_TZ8 = timezone(timedelta(hours=8))

_STOP_WORDS = set(
    '的 了 是 在 我 你 他 她 它 们 这 那 有 没 不 也 就 都 而 与 或 但'
    ' 如果 因为 所以 什么 怎么 哪 为啥 啊 呢 吧 嗯 哦 哈 呀 啦 嘛'
    ' the a an is are was were be been have has had do does did '
    ' and or but if then so no not just very really can will '
    ' to of in for on with at by from up about into over after'
    ' null undefined true false xml cdata uuid md5 img src href'
    ' risk-file-flag risk-file-md5-list clawemail sechashinfobase64'
    ' duration size fileid hdsize hdmd5 hdfileid stillimagetimems'
    ' imgsourceinfo live code'
    .split()
)

_STOP_BIGRAMS = set(
    '这个 那个 一个 一下 一些 什么 没有 可以 已经 我们 他们 现在'
    ' 还是 或者 但是 而且 不过 然而 所以 因为 虽然 这样 那样 这些 那些'
    ' https http com www 微信 分享 图片 视频 语音 消息 链接 文件'
    ' 看 说 想 知道 觉得 发 做 用 去 来 会 能 要 让 被 把 比 等'
    ' 不是 就是 还是 这个 那个 什么 怎么 可以 已经 因为 所以 但是'
    ' 而且 不过 然而 或者 还有 就是 只是 然后 其实 这个 那个 这样 那样'
    ' 自己 别人 大家 现在 之前 之后 时候 今天 明天 昨天 刚才'
    ' 问题 方式 东西 部分 相关 需要 开始 使用 可能 应该 一样'
    ' 一直 一起 一般 一样 一下 一些 不要 不会 不能 不同 不断'
    ' 很多 非常 真的 特别 直接 其实 确实 基本上 差不多'
    ' 谢谢 好的 没有 哈哈 嗯嗯 对的 是的 可以 行的 OK ok'
    ' 对比 支持 升级 版本 工作 内容 大家 你的 我们 这个'
    ' 感觉 看到 发现 觉得 认为 认为 希望 理解 关注'
    ' 版本不支 本不支持 不支持展 支持展示 持展示该 展示该内 示该内容'
    ' 请升级至 最新版本 升级至最 新至最新'
    ' null undefined true false'
    ' cdata uuid md5 img src href'
    ' 破涕为笑 爱心 鼓掌 恭喜 发怒 捂脸 旺柴 尴尬 冷汗 偷笑'
    ' 微信红包 红包 拼手气红包'
    ' 级至最新 至最新版 一个工作'
    ' 个工作邮 管理你的 先给它一 给它一个 它一个工'
    ' 你管理你 帮你管理 发工资前 先给它一个工作邮箱'
    ' 帮你管理你的事务'
    ' 拍了拍 所有人 哈哈哈哈'
    ' 实践 名可免费体验 可免费体 免费体验 我用'
    ' 呲牙 哈哈哈 奋斗 晕 歇会 玫瑰 强 墨 礼物 蛋糕 撇嘴'
    ' 上跑通网 跑通网易 上跑通网易'
    ' 理你的事 你的事务 文末前 名可免费'
    ' 当前微信版本不支持展示该内容 当前微信 前微信版 微信版本 信版本不'
    ' 当前版本不支持展示该内容 当前版本 前版本不 版本不支 不支持展 支持展示'
    ' 小时 老板 开源了'
    ' 的孩子在 孩子在跳 子在跳舞 神的孩子 神的孩子在跳舞'
    ' 在跳舞 在跳舞 跳舞'
    ' 然后 然后 然后'
    .split()
)

_NOISE_BIGRAMS = _STOP_WORDS | _STOP_BIGRAMS

# ═══════════════════════════════════════════════════════════
# 关键词 → 话题归类映射（合并同义词和子话题到父话题）
# ═══════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════
# 话题映射（精简版：只保留大小写归一化和真正同义词，不再跨产品合并）
# 设计原则：
#   - 同一产品不同叫法可以合并：chatgpt→ChatGPT, gpt4o→GPT-4o
#   - 不同产品不能合并：opus≠Claude, cursor≠copilot, r1≠DeepSeek
#   - 保留 __IGNORE__ 标记（learned 层会用到）
# ═══════════════════════════════════════════════════════════
_TOPIC_ALIASES = {
    # AI 模型 — 仅大小写归一化，不同模型各自独立
    'claude': 'Claude', 'anthropic': 'Anthropic',
    'opus': 'Opus', 'sonnet': 'Sonnet', 'haiku': 'Haiku',
    'chatgpt': 'ChatGPT', 'openai': 'OpenAI',
    'gpt4': 'GPT-4', 'gpt-4': 'GPT-4', 'gpt4o': 'GPT-4o', 'gpt-4o': 'GPT-4o',
    'o1': 'O1', 'o3': 'O3', 'o4': 'O4',
    'sora': 'Sora',
    'gemini': 'Gemini', 'google.ai': 'Gemini',
    'deepseek': 'DeepSeek',
    'r1': 'R1', 'v3': 'V3',
    'llama': 'Llama', 'meta.ai': 'Meta AI',
    'qwen': 'Qwen', '通义': '通义千问', '万相': '万相',
    'doubao': '豆包',
    # AI 工具 — 各自独立
    'codex': 'Codex', 'cursor': 'Cursor', 'copilot': 'Copilot',
    'augment': 'Augment', 'windsurf': 'Windsurf', 'trae': 'Trae',
    'aider': 'Aider', 'codeium': 'Codeium', 'tabnine': 'Tabnine',
    'replit': 'Replit',
    # Agent 框架 — 各自独立
    'hermes': 'Hermes', 'openclaw': 'OpenClaw', 'evolver': 'Evolver',
    'creao': 'Creao', 'memos': 'MemOS', 'mem0': 'MemOS',
    # AI 基础设施 — 只归一化真正同义的
    'embedding': 'Embedding',
    '微调': '微调', 'fine.tune': '微调', 'sft': 'SFT',
    'rlhf': 'RLHF', 'dpo': 'DPO', 'grpo': 'GRPO',
    'mcp': 'MCP',
    # 开发工具 — 各自独立
    'k8s': 'K8s', 'kubernetes': 'Kubernetes',
    'neovim': 'Neovim',
    'golang': 'Go',
    # 电商/商业
    'tiktok': 'TikTok', 'shopify': 'Shopify',
    'bilibili': 'B站',
    # 社交
    '推特': 'Twitter/X', 'twitter': 'Twitter/X',
}


def _load_learned_aliases(config_path):
    """加载 LLM 学习到的动态别名映射"""
    config_dir = os.path.dirname(os.path.abspath(config_path))
    path = os.path.join(config_dir, 'learned_aliases.json')
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data.get('aliases', {})
        except (json.JSONDecodeError, IOError):
            pass
    return {}


# 合并后的映射缓存（种子 + 学习层）
_merged_aliases = None


def _get_all_aliases(config_path=None):
    """返回种子映射 + 学习层映射的合并结果"""
    global _merged_aliases
    if _merged_aliases is not None:
        return _merged_aliases
    merged = dict(_TOPIC_ALIASES)  # 种子层（静态）
    if config_path:
        learned = _load_learned_aliases(config_path)
        # 学习层优先级更高：可以覆盖种子的错误映射
        merged.update(learned)
    _merged_aliases = merged
    return merged


def _normalize_topic(keyword, config_path=None):
    """将原始关键词映射到归一化的话题名"""
    kw_lower = keyword.lower().strip()
    return _get_all_aliases(config_path).get(kw_lower, keyword)

_XML_NOISE = {
    'msgsource', 'tmp_node', 'publisher-id', 'silence', 'membercount',
    'signature', 'alnode', 'eggincluded', 'chatroom', 'cf', 'inlenlist',
    'pua', 'view', 'action', 'showtype', 'soundtype', 'contentattr',
    'mediatagname', 'messageext', 'messageaction', 'statextstr',
    'sourceusername', 'sourcedisplayname', 'streamvideo', 'canvaspageitem',
    'refermsg', 'appattach', 'extinfo', 'appmsg', 'sdkver',
    'totallen', 'attachid', 'emoticonmd5', 'fileext', 'aeskey',
    'cdnthumbaeskey', 'cdnthumburl', 'cdnthumblength', 'cdnthumbheight',
    'cdnthumbwidth', 'cdnmidheight', 'cdnmidwidth', 'cdnhdheight',
    'cdnhdwidth', 'cdnmidimgurl', 'cdnbigimgurl', 'length', 'hdlength',
    'forwardflag', 'dataurl', 'lowdataurl', 'streamvideourl',
    'streamvideototaltime', 'streamvideotitle', 'streamvideowording',
    'streamvideoweburl', 'streamvideothumburl', 'streamvideoaduxinfo',
    'streamvideopublishid', 'secHashInfoBase64', 'imgdatahash',
    'platform_signature', 'islargefilemsg', 'androidsource',
    'fromusername', 'appinfo', 'version', 'appname', 'isforceupdate',
    'msg', 'sequence_id', 'bizflag', 'type', 'title', 'sec_msg_node',
    'username', 'des', 'content', 'url', 'lowurl', 'name', 'topnew',
    'cover', 'width', 'height', 'digest', 'text_title', 'itemshowtype',
    'pub_time', 'shorturl', 'longurl', 'summary', 'has_redpacket_cover',
    'createtime', 'chatusr', 'displayname', 'category', 'count',
    'weappinfo', 'weburl', 'thumburl',
}


def parse_args():
    parser = argparse.ArgumentParser(description='从 collector.db 提取热点事件（支持增量扫描）')
    parser.add_argument('--config', required=True, help='YAML 配置文件路径')
    parser.add_argument('--date', default='yesterday', help='日期: yesterday 或 YYYY-MM-DD（仅 --full 模式生效）')
    parser.add_argument('--top', type=int, default=20, help='每类返回 Top N')
    parser.add_argument('--min-groups', type=int, default=2, help='跨群话题最少出现群数')
    parser.add_argument('--min-count', type=int, default=5, help='关键词最少出现次数')
    parser.add_argument('--full', action='store_true', default=False,
                        help='全量模式：扫描指定日期整天（默认为增量模式）')
    parser.add_argument('--state', default=None,
                        help='scan_state.json 路径（默认从 config 同目录推导）')
    parser.add_argument('--update-aliases', default=None,
                        help='JSON 文件路径，包含 LLM 学习到的新 alias→topic 映射，合并到 learned_aliases.json')
    return parser.parse_args()


def load_config(config_path):
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'decrypt'))
    from config import load_config as _load
    return _load(config_path)


def _extract_urls(text):
    urls = re.findall(r'https?://[^\s<>"\']+', text)
    cleaned = []
    for u in urls:
        u = u.rstrip(').,;:，。；：）')
        if any(d in u for d in ('tc.qq.com/stodownload', 'vweixinf.tc.qq.com',
                                 'wxapp.tc.qq.com', 'wxfile://', 'weixin://')):
            continue
        try:
            p = urlparse(u)
            host = p.netloc.lower()
            path = p.path.rstrip('/')
            if host in ('mp.weixin.qq.com', 'weixin.sogou.com'):
                biz = parse_qs(p.query).get('__biz', [''])[0]
                mid = parse_qs(p.query).get('mid', [''])[0]
                if biz:
                    cleaned.append(f'{host}?__biz={biz}&mid={mid}')
                else:
                    cleaned.append(f'{host}{path}')
            elif host in ('x.com', 'twitter.com', 'v2ex.com', 'github.com',
                         'juejin.cn', 'sspai.com', '36kr.com', 'zhihu.com'):
                segs = [s for s in path.split('/') if s]
                if len(segs) >= 2:
                    cleaned.append(f'{host}/{segs[0]}/{segs[1]}')
                else:
                    cleaned.append(f'{host}{path}')
            else:
                cleaned.append(f'{host}{path}'[:120])
        except Exception:
            cleaned.append(u[:120])
    return cleaned


# 泛化大类词——在跨群统计时天然高频，几乎无信息量
_GENERIC_WORDS = frozenset(
    'claude gpt chatgpt openai ai agent 模型 训练 推理 部署 '
    'api prompt token 人工智能 大模型 LLM llm '
    'copilot cursor 编程 代码 开发 工具 插件 '
    'gpt4 gpt-4 gpt4o gpt-4o gemini deepseek llama '
    '微信公众号 微信文章 小程序 微信群 '
    'python rust typescript javascript swift docker '
    .split()
)

# 用于 bigram 的英文词边界
_WORD_RE = re.compile(r'[a-zA-Z][\w.+-]*[a-zA-Z0-9]')


def _tokenize(text):
    tokens = []
    # --- Pass 1: 提取英文单词 ---
    en_words = []
    for m in _WORD_RE.finditer(text):
        w = m.group().lower()
        if len(w) < 3 or w in _STOP_WORDS or w in _XML_NOISE:
            continue
        if w.startswith('wxid_') or '_' in w and len(w) > 12:
            continue
        # 过滤微信号/用户名模式：含数字+字母混合，像 candy520nznf, gavin8800
        if re.match(r'^[a-z]+\d+[a-z]*\d*$', w) and len(w) > 5:
            continue
        if re.match(r'^[a-z]+[_-]\w+$', w) and len(w) > 6:
            continue
        if re.match(r'^[a-z]{3,}(?:url|id|size|md5|flag|info|type|time|ms|len|num|key|hash|token|base64|lyric)$', w):
            continue
        if w in ('app', 'template_id', 'songlyric', 'hongbao', 'wxpay',
                 'atuserlist', 'sendusername'):
            continue
        if re.match(r'^[a-z]+[A-Z][a-zA-Z]+(?:Url|Id|List|Name|Info|Handler|Flag|Time|Type|Num)$', w):
            continue
        if len(w) > 12 and not any(c in w for c in '-.'):
            continue
        en_words.append(w)
    tokens.extend(en_words)

    # --- Pass 2: 英文 bigram（相邻两个英文词组合）---
    for i in range(len(en_words) - 1):
        a, b = en_words[i], en_words[i + 1]
        bigram = f'{a} {b}'
        # 过滤：两个都是泛化词的组合没意义
        if a in _GENERIC_WORDS and b in _GENERIC_WORDS:
            continue
        if len(a) < 3 or len(b) < 3:
            continue
        tokens.append(bigram)

    # --- Pass 3: 中文短语 ---
    for m in re.finditer(r'[\u4e00-\u9fff]{2,}', text):
        phrase = m.group()
        if phrase in _XML_NOISE:
            continue
        if 2 <= len(phrase) <= 4:
            if phrase not in _NOISE_BIGRAMS and phrase not in _STOP_WORDS:
                tokens.append(phrase)
        elif len(phrase) >= 5:
            if phrase not in _NOISE_BIGRAMS and phrase not in _STOP_WORDS:
                tokens.append(phrase)
            for i in range(len(phrase) - 3):
                tri = phrase[i:i+4]
                if tri not in _NOISE_BIGRAMS and tri not in _STOP_WORDS:
                    tokens.append(tri)

    # --- Pass 4: 中英混合 bigram（英文词 + 紧跟的中文短语）---
    # 例: "Claude Mythos架构" → "claude mythos架构"
    for m in re.finditer(r'([a-zA-Z][\w.+-]*[a-zA-Z0-9])\s*([\u4e00-\u9fff]{2,})', text):
        en_part = m.group(1).lower()
        cn_part = m.group(2)
        if cn_part in _XML_NOISE or cn_part in _NOISE_BIGRAMS:
            continue
        if len(en_part) >= 3 and len(cn_part) >= 2:
            tokens.append(f'{en_part} {cn_part}')

    return tokens


def _extract_article_title(text):
    m = re.search(r'<title><!\[CDATA\[(.*?)\]\]></title>', text)
    if m:
        return m.group(1).strip()
    m = re.search(r'<title>(.*?)</title>', text)
    if m:
        return m.group(1).strip()
    return None


def main():
    args = parse_args()
    cfg = load_config(args.config)
    collector_db = cfg['collector_db']

    # ─── 确定 scan_state.json 路径并初始化 StateManager ───
    if args.state:
        state_path = args.state
    else:
        # 从 config 文件同目录推导 scan_state.json
        config_dir = os.path.dirname(os.path.abspath(args.config))
        state_path = os.path.join(config_dir, 'scan_state.json')

    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, SCRIPT_DIR)
    from state_manager import StateManager
    sm = StateManager(state_path)

    now = datetime.now(tz=_TZ8)
    today_str = now.strftime('%Y-%m-%d')

    # ─── 读取当前 state ───
    trending_state = sm.get_trending()
    existing_topics = trending_state.get('items', [])
    last_scan_ts = trending_state.get('last_scan_ts', 0)
    daily_done = trending_state.get('daily_done', '')
    already_done_today = (daily_done == today_str)

    # ─── 确定扫描时间窗口 ───
    if args.full:
        # 全量模式：扫描指定日期的整天（保留原始行为）
        if args.date == 'yesterday':
            d = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        else:
            d = datetime.strptime(args.date, '%Y-%m-%d').replace(tzinfo=_TZ8)
        ts_start = int(d.timestamp())
        ts_end = ts_start + 86400
        date_label = d.strftime('%Y-%m-%d')
        mode = 'full'
    else:
        # 今日累计模式：每次从今天 00:00 到 now，看全天的数据
        today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        ts_start = int(today_midnight.timestamp())
        ts_end = int(now.timestamp())
        date_label = today_str
        mode = 'today_cumulative'

    # ─── 增量模式下如果 today 已经汇总过，输出警告但不跳过 ───
    if already_done_today and not args.full:
        print(f"[WARN] trending daily_done already set for {today_str}, "
              f"proceeding with incremental scan anyway", file=sys.stderr)

    # ─── 查询数据库 ───
    conn = sqlite3.connect(collector_db)
    conn.text_factory = lambda b: b.decode('utf-8', 'replace')

    rows = conn.execute("""
        SELECT m.chatroom_id, m.sender, m.content, m.msg_time, m.msg_type,
               w.chatroom_name
        FROM messages m
        JOIN watched_chats w ON m.chatroom_id = w.chatroom_id
        WHERE m.chatroom_id LIKE '%@chatroom'
          AND m.msg_time >= ? AND m.msg_time < ?
        ORDER BY m.msg_time
    """, (ts_start, ts_end)).fetchall()

    group_names = {}
    group_msg_counts = Counter()
    url_counter = Counter()
    url_context = {}
    keyword_by_group = defaultdict(Counter)

    for chatroom_id, sender, content, msg_time, msg_type, group_name in rows:
        group_names[chatroom_id] = group_name
        group_msg_counts[chatroom_id] += 1

        if not content or len(content) < 5:
            continue
        if sender == '__self__':
            continue
        if content.startswith('[📎') or content.startswith('[🖼️') or content.startswith('[🎤'):
            continue

        urls = _extract_urls(content)
        for u in urls:
            url_counter[u] += 1
            if u not in url_context:
                title = _extract_article_title(content)
                url_context[u] = {
                    'title': title,
                    'first_seen_group': group_name,
                    'first_seen_time': datetime.fromtimestamp(msg_time, _TZ8).strftime('%H:%M'),
                }

        clean = content
        if content.startswith('<?xml') or content.startswith('<msg'):
            title = _extract_article_title(content)
            if title:
                clean = title
            else:
                continue
        clean = re.sub(r'https?://\S+', ' ', clean)
        clean = re.sub(r'<[^>]+>', ' ', clean)
        clean = re.sub(r'[\U00010000-\U0010ffff]', ' ', clean)
        clean = re.sub(r'[a-zA-Z0-9_]+=\x22', ' ', clean)
        XML_ATTRS = {'aeskey', 'cdnthumbaeskey', 'cdnthumburl', 'encryver',
                     'cdnthumblength', 'totallen', 'attachid', 'fileext',
                     'md5', 'hevc', 'secHashInfoBase64', 'streamvideo'}
        if any(clean.lower().strip().startswith(x) for x in XML_ATTRS):
            continue

        tokens = _tokenize(clean)
        # 归一化：把同义词/子话题合并到父话题
        normalized = [_normalize_topic(t, args.config) for t in tokens]
        # 过滤被标记为 __IGNORE__ 的 token（人名/噪音）
        normalized = [t for t in normalized if t != '__IGNORE__']
        keyword_by_group[chatroom_id].update(normalized)

    conn.close()

    # ─── 跨群话题 ───
    cross_group_kw = Counter()
    for chatroom_id, kw_counter in keyword_by_group.items():
        for kw in kw_counter:
            cross_group_kw[kw] += 1

    cross_topics = []
    for kw, group_count in cross_group_kw.most_common(200):
        if group_count < args.min_groups:
            continue
        if len(kw) < 2:
            continue
        if kw in _STOP_WORDS:
            continue
        # 过滤泛化大类词（单 token 形式），但保留 bigram 如 "claude mythos"
        kw_lower = kw.lower()
        if ' ' not in kw and kw_lower in _GENERIC_WORDS:
            continue
        total_mentions = sum(keyword_by_group[g][kw] for g in keyword_by_group)
        if total_mentions < args.min_count:
            continue
        source_groups = []
        for g, kc in keyword_by_group.items():
            if kw in kc and kc[kw] >= 2:
                source_groups.append(group_names.get(g, g))
        # 标记是否经过归一化
        all_aliases = _get_all_aliases(args.config)
        normalized = (kw != all_aliases.get(kw.lower().strip(), kw))
        cross_topics.append({
            'keyword': kw,
            'groups_count': group_count,
            'total_mentions': total_mentions,
            'source_groups': source_groups[:10],
            'is_merged': normalized,
        })
    cross_topics = cross_topics[:args.top]

    # ─── 热门 URL ───
    trending_urls = []
    for url, count in url_counter.most_common(args.top):
        if count < 2:
            break
        ctx = url_context.get(url, {})
        trending_urls.append({
            'url': url,
            'share_count': count,
            'title': ctx.get('title') or '',
            'first_seen_group': ctx.get('first_seen_group', ''),
            'first_seen_time': ctx.get('first_seen_time', ''),
        })

    # ─── 活跃群 ───
    active_groups = []
    avg_msgs = sum(group_msg_counts.values()) / max(len(group_msg_counts), 1)
    for gid, count in group_msg_counts.most_common(args.top):
        if count < max(avg_msgs * 2, 10):
            continue
        active_groups.append({
            'group_id': gid,
            'group_name': group_names.get(gid, gid),
            'message_count': count,
            'avg_daily': round(avg_msgs, 1),
        })

    # ─── 高频关键词 ───
    all_kw = Counter()
    for kc in keyword_by_group.values():
        all_kw.update(kc)
    high_freq = []
    for kw, count in all_kw.most_common(args.top * 3):
        if len(kw) < 2 or kw in _STOP_WORDS or count < args.min_count:
            continue
        # 过滤泛化大类词
        kw_lower = kw.lower()
        if ' ' not in kw and kw_lower in _GENERIC_WORDS:
            continue
        groups_with_kw = sum(1 for kc in keyword_by_group.values() if kw in kc)
        # 过滤只在1个群出现、不在归一化映射中的纯小写长词（大概率是用户名/人名）
        if groups_with_kw <= 1 and re.match(r'^[a-z]{4,}$', kw) and kw not in _TOPIC_ALIASES:
            continue
        high_freq.append({
            'keyword': kw,
            'count': count,
            'groups': groups_with_kw,
        })
        if len(high_freq) >= args.top:
            break

    # ─── 更新 state ───
    sm.update_trending(cross_topics, ts_end)
    sm.mark_trending_daily_done(today_str)
    sm.cleanup_old_trending(days=3)

    # ─── 更新学习层映射（如果 LLM 提供了新的 alias） ───
    if args.update_aliases:
        config_dir = os.path.dirname(os.path.abspath(args.config))
        learned_path = os.path.join(config_dir, 'learned_aliases.json')
        try:
            with open(args.update_aliases, 'r', encoding='utf-8') as f:
                new_aliases = json.load(f)
            if not isinstance(new_aliases, dict):
                new_aliases = {'aliases': new_aliases}
            aliases_to_add = new_aliases.get('aliases', new_aliases)
            
            # 加载现有
            existing = _load_learned_aliases(args.config)
            added = 0
            for k, v in aliases_to_add.items():
                if k not in existing and k not in _TOPIC_ALIASES:
                    existing[k] = v
                    added += 1
            
            if added > 0:
                learned_data = {
                    'aliases': existing,
                    'last_updated': today_str,
                    'stats': {
                        'total_learned': len(existing),
                        'total_applied': existing.get('_applied_count', 0),
                    }
                }
                with open(learned_path, 'w', encoding='utf-8') as f:
                    json.dump(learned_data, f, ensure_ascii=False, indent=2)
                print(f"[INFO] Learned {added} new aliases, total: {len(existing)}", file=sys.stderr)
        except Exception as e:
            print(f"[WARN] Failed to update aliases: {e}", file=sys.stderr)

    # ─── 输出 ───
    output = {
        'date': date_label,
        'mode': mode,
        'scan_window': {
            'start_ts': ts_start,
            'end_ts': ts_end,
            'start_time': datetime.fromtimestamp(ts_start, _TZ8).strftime('%Y-%m-%d %H:%M:%S'),
            'end_time': datetime.fromtimestamp(ts_end, _TZ8).strftime('%Y-%m-%d %H:%M:%S'),
        },
        'total_groups': len(group_msg_counts),
        'total_messages': sum(group_msg_counts.values()),
        'cross_group_topics': cross_topics,
        'trending_urls': trending_urls,
        'active_groups': active_groups,
        'high_freq_keywords': high_freq,
        'existing_topics': existing_topics,
        'already_done_today': already_done_today,
        'scan_state_path': state_path,
        'topic_aliases': {
            'seed_count': len(_TOPIC_ALIASES),
            'learned_count': len(_load_learned_aliases(args.config)) if args.config else 0,
        },
    }

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
