#!/usr/bin/env python3
"""
insight.py — 读取最近 N 天的 digest JSON，输出合并数据供 LLM 分析。
输出到 stdout，包含：
  - digests: 每天的干货数据
  - group_profiles: 现有群画像
  - topic_threads: 现有话题线索
  - scan_state_path: 状态文件路径
  - days_analyzed: 分析的天数

用法：
  python3 insight.py --config /path/to/config.yaml [--days 3] [--all]
"""

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timedelta


def load_config(config_path):
    """使用共享的 config 模块加载 YAML 配置"""
    try:
        from decrypt.config import load_config as _load
        return _load(config_path)
    except ImportError:
        # fallback: 手动解析
        import yaml
        with open(config_path) as f:
            return yaml.safe_load(f)


def get_data_dir(config_path):
    """数据目录 = config.yaml 所在目录"""
    return os.path.dirname(os.path.abspath(config_path))


def load_recent_digests(data_dir, days=3):
    """加载最近 N 天的 digest JSON"""
    digest_dir = os.path.join(data_dir, "digests")
    if not os.path.isdir(digest_dir):
        return []

    today = datetime.now().date()
    cutoff = today - timedelta(days=days)

    digests = []
    for f in sorted(glob.glob(os.path.join(digest_dir, "*.json"))):
        try:
            fname_date = os.path.basename(f).replace(".json", "")
            fdate = datetime.strptime(fname_date, "%Y-%m-%d").date()
        except ValueError:
            continue

        if fdate >= cutoff:
            with open(f) as fh:
                data = json.load(fh)
                data["_file"] = os.path.basename(f)
                digests.append(data)

    return digests


def load_json_safe(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def main():
    parser = argparse.ArgumentParser(description="Insight analysis data collector")
    parser.add_argument("--config", required=True, help="Path to config.yaml")
    parser.add_argument("--days", type=int, default=3, help="Number of recent days to analyze (default: 3)")
    parser.add_argument("--all", action="store_true", help="Load all available digests (ignore --days)")
    args = parser.parse_args()

    config = load_config(args.config)
    data_dir = get_data_dir(args.config)

    # Load digests
    if args.all:
        days = 999
    else:
        days = args.days

    digests = load_recent_digests(data_dir, days)

    if not digests:
        result = {
            "digests": [],
            "group_profiles": {},
            "topic_threads": [],
            "scan_state_path": os.path.join(data_dir, "scan_state.json"),
            "group_profiles_path": os.path.join(data_dir, "group_profiles.json"),
            "topic_threads_path": os.path.join(data_dir, "topic_threads.json"),
            "days_analyzed": 0,
            "date_range": None,
            "message": "没有找到 digest 数据。确保 digest cron 已运行并保存了 JSON。"
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0)

    # Load existing profiles and threads
    profiles = load_json_safe(os.path.join(data_dir, "group_profiles.json"))
    threads = load_json_safe(os.path.join(data_dir, "topic_threads.json"))

    # Extract date range
    dates = [d.get("date", d.get("_file", "?")) for d in digests]

    # Build per-group item frequency
    group_item_counts = {}
    for d in digests:
        for g in d.get("groups", []):
            gname = g.get("name", "unknown")
            if gname not in group_item_counts:
                group_item_counts[gname] = {"total_items": 0, "dates": set(), "categories": {}}
            group_item_counts[gname]["total_items"] += len(g.get("items", []))
            group_item_counts[gname]["dates"].add(d.get("date", ""))
            for item in g.get("items", []):
                cat = item.get("category", "other")
                group_item_counts[gname]["categories"][cat] = group_item_counts[gname]["categories"].get(cat, 0) + 1

    # Convert sets to sorted lists for JSON
    for g in group_item_counts.values():
        g["dates"] = sorted(g["dates"])

    result = {
        "digests": digests,
        "digest_count": len(digests),
        "date_range": f"{dates[0]} ~ {dates[-1]}" if dates else None,
        "days_analyzed": len(digests),
        "group_summary": group_item_counts,
        "existing_profiles": profiles.get("groups", {}),
        "existing_threads": threads.get("threads", []),
        "scan_state_path": os.path.join(data_dir, "scan_state.json"),
        "group_profiles_path": os.path.join(data_dir, "group_profiles.json"),
        "topic_threads_path": os.path.join(data_dir, "topic_threads.json"),
    }

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
