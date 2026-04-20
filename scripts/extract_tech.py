#!/usr/bin/env python3
"""
extract_tech.py — 从 collector.db 提取技术讨论，输出 JSON（不调 AI）

检测逻辑：
1. 按技术分类扫描消息（AI/ML、编程语言、开发工具、云服务、框架库等）
2. 提取包含技术关键词的上下文（前后各 1 条消息）
3. 按热度排序（讨论次数 × 讨论群数）

用法：
  python3 extract_tech.py --config config.yaml
  python3 extract_tech.py --config config.yaml --date 2026-03-12
  python3 extract_tech.py --config config.yaml --category ai
  python3 extract_tech.py --config config.yaml --top 30

输出 JSON 到 stdout:
{
  "date": "2026-03-12",
  "categories": {
    "AI/LLM": [{"keyword": "Claude", "count": 15, "groups": 3, "highlights": [...]}]
  }
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

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from state_manager import StateManager

_TZ8 = timezone(timedelta(hours=8))

TECH_KEYWORDS = {
    'AI 聊天/对话': [
        'chatgpt', 'openai', 'claude', 'anthropic', 'gemini',
        'kimi', 'moonshot', '豆包', 'doubao', '文心一言', 'ernie', '文心',
        '通义千问', 'qwen', '智谱', 'chatglm', '百川', 'deepseek',
        'grok', 'perplexity', 'pi', 'copilot',
        'yi', 'minimax', 'abab', 'stepfun', '阶跃星辰',
    ],
    'AI 编程': [
        'cursor', 'windsurf', 'augment', 'cline', 'aider', 'trae',
        'codeium', 'tabnine', 'supermaven', 'devin', 'sweep',
        'copilot', 'codex', 'code interpreter',
        'bolt.new', 'lovable', 'v0', 'v0.dev', 'replit',
        'tempo', 'quotient', 'granite',
    ],
    'AI Agent/自动化': [
        'openclaw', 'hermes', 'n8n', 'zapier', 'make.com',
        'coze', '扣子', 'dify', 'fastgpt', 'lobechat', 'open-webui',
        'crewai', 'autogen', 'langgraph', 'semantic kernel',
        'bee-agent', 'mastra', 'genkit', 'agno', 'phidata',
        'letta', 'mem0', 'memgpt', 'camel', 'taskweaver',
        'browser-use', 'playwright', 'puppeteer', 'selenium',
        'rpa', '自动化', 'workflow', '工作流',
    ],
    'AI 图片/视频/音频': [
        'midjourney', 'mj', 'dall-e', 'ideogram', 'flux', 'stable diffusion', 'sd',
        'comfyui', 'webui', 'fooocus', 'invoke ai', 'civitai',
        'sora', 'runway', 'pika', 'kling', '可灵', 'hailuo', 'vidu',
        'luma', 'haiper', 'minimax video',
        'suno', 'udio', 'elevenlabs', 'fish audio',
        'heygen', 'd-id', 'synthesia',
    ],
    'AI 知识/效率': [
        'notion ai', 'obsidian', 'roam research', 'logseq',
        'notebooklm', 'notebook lm', 'google notebooklm',
        'raycast ai', 'arc browser', 'arc search',
        'mem.ai', 'reflect', 'heptabase', 'tana',
        'readwise', 'omnivore', 'cubox', 'eagle',
        'cobblepot', 'ima', 'ima知识库',
    ],
    'AI 模型/训练': [
        'gpt-4', 'gpt-4o', 'gpt-4.5', 'gpt-3.5', 'o1', 'o3', 'o4-mini',
        'claude 3.5', 'claude 4', 'sonnet', 'opus', 'haiku',
        'gemini pro', 'gemini ultra', 'gemini flash',
        'llama', 'mistral', 'mixtral', 'phi', 'gemma',
        'deepseek-v3', 'deepseek-r1', 'deepseek coder',
        'qwen2', 'qwen3', 'glm-4', 'internlm',
        'llm', '大模型', '大语言模型', '语言模型', '基础模型', '基座模型',
        '微调', 'fine-tun', 'rlhf', 'dpo', 'lora', 'qlora',
        'rag', '检索增强', '向量数据库', 'embedding',
        'mcp', 'model context protocol', 'a2a', 'acp',
        'context window', '上下文窗口', 'token', 'tokenizer',
        'vllm', 'ollama', 'huggingface', 'sglang', 'tensorrt-llm',
        'cuda', 'gpu', 'tpu', 'npu', '算力', '推理卡', 'h100', 'h200', 'b200',
        '开源模型', '开放权重', 'open weights',
    ],
    'AI 提示词/工程': [
        'prompt', '提示词', '提示工程', 'prompt engineering',
        'system prompt', 'few-shot', 'chain of thought', 'cot',
        'function call', 'tool use', 'computer use',
        'multimodal', '多模态', 'vision',
        'hallucination', '幻觉', 'reasoning', '推理',
        'alignment', '对齐', 'safety', '安全',
    ],
    'AI 框架/SDK': [
        'langchain', 'llamaindex', 'haystack',
        'weaviate', 'pinecone', 'chromadb', 'milvus', 'qdrant', 'faiss', 'pgvector',
        'openai sdk', 'anthropic sdk', 'vercel ai sdk',
        'pydantic ai', 'instructor', 'litellm', 'portkey',
    ],
    '社交/内容平台': [
        '小红书', '抖音', 'tiktok', 'youtube', 'bilibili', 'b站',
        'twitter', 'x.com', 'threads', 'instagram', 'meta',
        '微信公众号', '视频号', 'wechat', '朋友圈',
        '微博', 'weibo', '知乎', 'zhihu',
        'reddit', 'hacker news', 'product hunt',
        'substack', 'medium', 'notion',
        '播客', 'podcast', '小宇宙',
        '商单', 'kol', 'mcn', '带货', '直播带货',
        '涨粉', '引流', '裂变', '私域',
    ],
    '电商/支付': [
        '淘宝', '天猫', '京东', 'pdd', '拼多多', '1688',
        'shopify', 'woocommerce', 'magento',
        '支付宝', '微信支付', 'stripe', 'paypal',
        '独立站', 'shopline', '店匠',
        '跨境', 'cross-border', 'shein', 'temu',
    ],
    'Web3/加密': [
        'bitcoin', 'btc', 'ethereum', 'eth', 'solana', 'sol',
        'web3', '区块链', 'blockchain', 'defi', 'nft',
        'smart contract', '智能合约', 'solidity',
        'token', 'coin', 'dao', 'airdrop', '空投',
        'metamask', 'wallet', '钱包',
        'opensea', 'uniswap', 'binance', 'coinbase',
        'layer2', 'rollup', 'zk', '零知识证明',
    ],
    '编程语言': [
        'python', 'rust', 'golang', 'typescript', 'javascript',
        'swift', 'kotlin', 'java ', 'c++', 'ruby', 'zig',
        'lua', 'dart', 'elixir', 'haskell', 'ocaml',
        'shell', 'bash', 'powershell',
        'wasm', 'webassembly',
    ],
    '前端/框架': [
        'react', 'vue', 'nextjs', 'next.js', 'nuxt', 'svelte', 'solidjs',
        'angular', 'astro', 'remix', 'hono',
        'tailwind', 'shadcn', 'radix', 'ant design',
        'three.js', 'webgl', 'canvas',
        'flutter', 'react native', 'swiftui', 'compose',
    ],
    '后端/数据库': [
        'django', 'fastapi', 'flask', 'spring', 'express', 'koa',
        'nginx', 'redis', 'postgres', 'mysql', 'mongodb',
        'supabase', 'firebase', 'planetcale', 'cockroachdb',
        'sqlite', 'prisma', 'drizzle', 'typeorm',
        'graphql', 'rest api', 'grpc',
        'kafka', 'rabbitmq', 'elasticsearch', 'clickhouse',
        '消息队列', 'event sourcing', 'cqrs',
    ],
    '基础设施/DevOps': [
        'docker', 'kubernetes', 'k8s',
        'github', 'gitlab', 'bitbucket',
        'vercel', 'cloudflare', 'netlify', 'railway', 'render',
        'aws', 'azure', 'gcp', '阿里云', '腾讯云', '华为云',
        'terraform', 'ansible', 'pulumi',
        'ci/cd', 'github actions', 'jenkins',
        'grafana', 'prometheus', 'datadog',
        'serverless', 'edge function', 'lambda',
        'sre', 'devops', '可观测性',
        'turborepo', 'monorepo', 'microservice', '微服务',
    ],
    '硬件/芯片/汽车': [
        'nvidia', 'amd', 'intel', 'apple silicon', 'm1', 'm2', 'm3', 'm4',
        '芯片', 'chip', 'semiconductor', '半导体',
        'tesla', '自动驾驶', 'autonomous', 'lidar', '激光雷达',
        '机器人', 'robot', 'humanoid', '人形机器人',
        'iot', '物联网', 'embedded', '嵌入式',
        'drone', '无人机', '大疆', 'dji',
        'ar', 'vr', 'mr', 'xr', 'vision pro', 'apple vision',
        '摩托', 'motorcycle', '电动车',
    ],
}


def _build_pattern(keywords):
    parts = []
    for kw in keywords:
        kw_escaped = re.escape(kw.strip())
        if re.match(r'^[a-zA-Z]', kw):
            parts.append(r'\b' + kw_escaped + r'\b')
        else:
            parts.append(kw_escaped)
    return re.compile('|'.join(parts), re.IGNORECASE)


def parse_args():
    parser = argparse.ArgumentParser(description='从 collector.db 提取技术讨论')
    parser.add_argument('--config', required=True, help='YAML 配置文件路径')
    parser.add_argument('--date', default='yesterday', help='日期: yesterday 或 YYYY-MM-DD')
    parser.add_argument('--top', type=int, default=15, help='每类返回 Top N')
    parser.add_argument('--category', default='', help='只输出指定分类（如 ai）')
    parser.add_argument('--state', default='', help='scan_state.json 路径（默认从 config 同目录推导）')
    return parser.parse_args()


def load_config(config_path):
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'decrypt'))
    from config import load_config as _load
    return _load(config_path)


def main():
    args = parse_args()
    cfg = load_config(args.config)
    collector_db = cfg['collector_db']

    now = datetime.now(tz=_TZ8)
    if args.date == 'yesterday':
        d = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    else:
        d = datetime.strptime(args.date, '%Y-%m-%d').replace(tzinfo=_TZ8)
    ts_start = int(d.timestamp())
    ts_end = ts_start + 86400
    date_label = d.strftime('%Y-%m-%d')

    # ── State / 日去重 ──────────────────────────────────────
    if args.state:
        state_path = args.state
    else:
        config_dir = os.path.dirname(os.path.abspath(args.config))
        state_path = os.path.join(config_dir, 'scan_state.json')

    sm = StateManager(state_path)

    # 检查该日期是否已扫描过
    tech_state = sm.get_tech_state()
    if tech_state.get('daily_done') == date_label:
        print(json.dumps({
            'date': date_label,
            'already_done': True,
            'message': '该日期技术讨论已扫描过，跳过',
            'scan_state_path': state_path,
        }, ensure_ascii=False, indent=2))
        return

    category_filter = args.category.lower().replace('/', '').replace(' ', '')
    categories = {}
    for cat, keywords in TECH_KEYWORDS.items():
        cat_key = cat.lower().replace('/', '').replace(' ', '')
        if category_filter and category_filter not in cat_key:
            continue
        categories[cat] = {
            'pattern': _build_pattern(keywords),
            'keyword_list': keywords,
            'matches': Counter(),
            'groups': defaultdict(set),
            'highlights': defaultdict(list),
        }

    if not categories:
        print(json.dumps({'error': f'未找到分类: {args.category}', 'available': list(TECH_KEYWORDS.keys())}))
        sys.exit(1)

    conn = sqlite3.connect(collector_db)
    conn.text_factory = lambda b: b.decode('utf-8', 'replace')

    rows = conn.execute("""
        SELECT m.chatroom_id, m.sender, m.content, m.msg_time,
               w.chatroom_name
        FROM messages m
        JOIN watched_chats w ON m.chatroom_id = w.chatroom_id
        WHERE m.chatroom_id LIKE '%@chatroom'
          AND m.msg_time >= ? AND m.msg_time < ?
        ORDER BY m.msg_time
    """, (ts_start, ts_end)).fetchall()

    group_names = {}
    for chatroom_id, sender, content, msg_time, group_name in rows:
        group_names[chatroom_id] = group_name

        if not content or len(content) < 5:
            continue
        if sender == '__self__':
            continue
        if content.startswith('[📎') or content.startswith('[🖼️') or content.startswith('[🎤'):
            continue

        text = content
        if content.startswith('<?xml') or content.startswith('<msg'):
            m = re.search(r'<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>', content)
            if m:
                text = m.group(1)
            else:
                text = re.sub(r'<[^>]+>', ' ', content)

        for cat, data in categories.items():
            hits = data['pattern'].findall(text)
            if not hits:
                continue
            for hit in set(h.lower() for h in hits):
                data['matches'][hit] += 1
                data['groups'][hit].add(chatroom_id)
                if len(data['highlights'][hit]) < 5:
                    time_str = datetime.fromtimestamp(msg_time, _TZ8).strftime('%H:%M')
                    snippet = text[:200].strip()
                    data['highlights'][hit].append({
                        'group': group_name,
                        'sender': sender,
                        'time': time_str,
                        'snippet': snippet,
                    })

    conn.close()

    result = {}
    for cat, data in categories.items():
        items = []
        for kw, count in data['matches'].most_common(args.top):
            groups = data['groups'][kw]
            items.append({
                'keyword': kw,
                'count': count,
                'groups_count': len(groups),
                'group_names': [group_names.get(g, g) for g in list(groups)[:10]],
                'highlights': data['highlights'][kw],
            })
        if items:
            result[cat] = items

    total_tech_mentions = sum(sum(item['count'] for item in items) for items in result.values())

    # 标记该日期已完成
    sm.mark_tech_done(date_label)

    output = {
        'date': date_label,
        'already_done': False,
        'scan_state_path': state_path,
        'total_messages': len(rows),
        'total_tech_mentions': total_tech_mentions,
        'categories': result,
    }

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
