#!/usr/bin/env python3
"""Clash 订阅合并主程序。

多源抓取 -> 解析 -> 清洗去重 -> 按地区分组 -> 套策略组与规则模板 -> 输出 Meta 配置。

用法:
    python merge.py                      # 读 sources.txt，输出 dist/config.yaml
    python merge.py -o config.yaml       # 指定输出
    python merge.py -s my-sources.txt    # 指定源清单
环境变量:
    SUB_URLS   追加的订阅链接，换行或逗号分隔
"""
from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

from crawler import resolve_page, response_text
from parsers import b64_decode, parse_links
from regions import detect_region, strip_flags
from speedtest import is_udp_only, measure

ROOT = Path(__file__).resolve().parent
CST = timezone(timedelta(hours=8))

SUPPORTED_TYPES = {
    "ss", "vmess", "vless", "trojan", "hysteria", "hysteria2", "tuic", "http", "socks5",
}
BAD_SERVERS = {"", "127.0.0.1", "localhost", "0.0.0.0", "::1"}

DEFAULT_OPTIONS = {
    "request": {"timeout": 20, "retries": 2, "user_agent": "clash.meta"},
    "filter": {
        "exclude_keywords": [
            "剩余流量", "剩余", "过期", "到期", "已过期", "官网", "网址",
            "充值", "续费", "购买", "套餐", "公告", "流量", "重置",
            "traffic", "expire", "expired", "official", "renew",
        ],
        "exclude_types": [],
    },
    "dedupe": {"enabled": True},
    "naming": {"prefix_flag": True, "ensure_unique": True, "max_length": 60},
    "speedtest": {
        "enabled": True,
        "workers": 200,          # 并发探测数
        "timeout": 3.0,          # 单次连接超时（秒）
        "drop_unreachable": True,# 连不上的直接丢弃
        "max_latency_ms": 0,     # 延迟上限，超过则丢弃；0 = 不限制
        "keep_top": 0,           # 只保留最快的 N 个；0 = 全保留
        "keep_per_region": 0,    # 每个地区最多保留 N 个；0 = 不限制
        "tag_name": True,        # 节点名后标注实测延迟
        "min_keep": 30,          # 存活数低于此值时回退不过滤（防 CI 网络受限误杀）
    },
    "groups": {
        "by_region": True,
        "fast": {"count": 30},   # ⚡ 低延迟组取前 N 个
        "auto_select": {
            "interval": 300,
            "tolerance": 50,
            "url": "https://www.gstatic.com/generate_204",
            "max_nodes": 80,
        },
    },
}


def now_cst() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_options(path: Path) -> dict:
    if not path.exists():
        return DEFAULT_OPTIONS
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return deep_merge(DEFAULT_OPTIONS, data)


def load_sources(path: Path) -> list[str]:
    """读取源清单 + 环境变量 SUB_URLS。"""
    urls: list[str] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)

    env = os.environ.get("SUB_URLS", "")
    for chunk in env.replace(",", "\n").splitlines():
        chunk = chunk.strip()
        if chunk and not chunk.startswith("#"):
            urls.append(chunk)

    # 保序去重
    seen, result = set(), []
    for url in urls:
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


def expand_sources(entries: list[str]) -> list[str]:
    """把 page: 开头的发布站地址展开成实际订阅链接，其余地址原样保留。"""
    result: list[str] = []
    with requests.Session() as session:
        for entry in entries:
            if entry.lower().startswith("page:"):
                result.extend(resolve_page(entry[5:].strip(), session))
            else:
                result.append(entry)

    seen, unique = set(), []
    for url in result:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def mask_url(url: str) -> str:
    """日志里隐藏链接中的敏感参数。"""
    if len(url) <= 48:
        return url
    return f"{url[:36]}...{url[-8:]}"


def fetch_source(url: str, options: dict) -> tuple[str, str]:
    """抓取单个订阅，返回 (内容, 备注信息)。失败抛异常。"""
    conf = options["request"]
    if url.startswith("file://") or (not url.startswith("http") and Path(url).exists()):
        local = Path(url[7:] if url.startswith("file://") else url)
        return local.read_text(encoding="utf-8"), "本地文件"

    headers = {"User-Agent": conf["user_agent"]}
    last_error = None
    for attempt in range(conf["retries"] + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=conf["timeout"])
            resp.raise_for_status()
            note = resp.headers.get("subscription-userinfo", "")
            return response_text(resp), note
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < conf["retries"]:
                import time
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(str(last_error))


def parse_subscription(text: str) -> list[dict]:
    """识别三类订阅内容：Clash YAML / base64 文本 / 分享链接列表。"""
    text = (text or "").strip()
    if not text:
        return []

    candidates = [text]
    decoded = b64_decode(text)
    if decoded:
        candidates.append(decoded)

    for candidate in candidates:
        try:
            data = yaml.safe_load(candidate)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data, dict) and isinstance(data.get("proxies"), list):
            return [n for n in data["proxies"] if isinstance(n, dict)]

    for candidate in candidates:
        nodes = parse_links(candidate)
        if nodes:
            return nodes
    return []


def fingerprint(node: dict) -> str:
    secret = node.get("uuid") or node.get("password") or node.get("id") or ""
    return f"{node.get('type')}|{node.get('server')}|{node.get('port')}|{secret}".lower()


def is_valid(node: dict, options: dict) -> bool:
    if not isinstance(node, dict):
        return False
    ntype = str(node.get("type", "")).lower()
    if ntype not in SUPPORTED_TYPES:
        return False
    if ntype in {t.lower() for t in options["filter"]["exclude_types"]}:
        return False
    if str(node.get("server", "")).strip().lower() in BAD_SERVERS:
        return False
    try:
        if int(node.get("port", 0)) <= 0:
            return False
    except (TypeError, ValueError):
        return False

    name = str(node.get("name", ""))
    lowered = name.lower()
    return not any(kw.lower() in lowered for kw in options["filter"]["exclude_keywords"])


def normalize(node: dict) -> dict:
    """统一字段：端口转 int，hy2 归一为 hysteria2，补 udp。"""
    node = dict(node)
    try:
        node["port"] = int(node["port"])
    except (TypeError, ValueError):
        pass
    ntype = str(node.get("type", "")).lower()
    if ntype == "hy2":
        ntype = "hysteria2"
    node["type"] = ntype
    node.setdefault("udp", True)
    return node


def rename(node: dict, options: dict, used: dict) -> dict:
    """加国旗前缀并保证节点名唯一。"""
    name = str(node.get("name", "")).strip()
    if not name:
        name = f'{node.get("type", "node")}-{node.get("server")}'
    clean = strip_flags(name) or name

    if options["naming"]["prefix_flag"]:
        _, flag = detect_region(name)
        if not clean.startswith(flag):
            clean = f"{flag} {clean}"

    max_len = int(options["naming"]["max_length"])
    if len(clean) > max_len:
        clean = clean[:max_len].rstrip()

    if options["naming"]["ensure_unique"]:
        if clean in used:
            used[clean] += 1
            clean = f"{clean} #{used[clean]}"
        else:
            used[clean] = 1

    node["name"] = clean
    return node


def apply_speedtest(nodes: list[dict], options: dict) -> tuple[list[dict], dict]:
    """探测延迟：丢弃连不上的 -> 按延迟升序 -> 标注延迟 -> 按需限量。
    返回 (节点列表, 统计)。UDP 协议（hy2/tuic）不探测，原样保留。"""
    conf = options["speedtest"]
    stats = {"total": len(nodes), "tested": 0, "alive": 0, "dropped": 0}

    results = measure(nodes, workers=int(conf["workers"]), timeout=float(conf["timeout"]))
    scored: list[tuple[dict, float | None]] = []
    for index, node in enumerate(nodes):
        if is_udp_only(node):
            scored.append((node, None))
            continue
        stats["tested"] += 1
        latency = results.get(index)
        if latency is None:
            if conf["drop_unreachable"]:
                stats["dropped"] += 1
                continue
            scored.append((node, None))
            continue
        stats["alive"] += 1
        scored.append((node, latency))

    max_latency = float(conf["max_latency_ms"] or 0)
    if max_latency > 0:
        before = len(scored)
        scored = [
            (n, lat) for n, lat in scored
            if lat is None or lat <= max_latency
        ]
        stats["dropped"] += before - len(scored)

    # 可达的按延迟升序，未探测的排最后
    scored.sort(key=lambda item: (item[1] is None, item[1] or 0.0))
    kept = [node for node, _ in scored]

    per_region = int(conf["keep_per_region"] or 0)
    if per_region > 0:
        counter: dict[str, int] = {}
        limited = []
        for node in kept:
            region, _ = detect_region(strip_flags(node["name"]))
            counter[region] = counter.get(region, 0) + 1
            if counter[region] <= per_region:
                limited.append(node)
        kept = limited

    keep_top = int(conf["keep_top"] or 0)
    if keep_top > 0:
        kept = kept[:keep_top]

    # 保护：CI 网络受限时可能大面积误判，节点太少就退回不过滤的结果
    min_keep = int(conf.get("min_keep", 30))
    fallback = len(kept) < min_keep <= len(nodes)
    if fallback:
        print(f"[测速] 存活节点仅 {len(kept)} 个（少于 {min_keep}），疑似网络受限，回退为不过滤")
        kept = nodes

    if conf["tag_name"] and not fallback:
        kept_ids = {id(node) for node in kept}
        for node, latency in scored:
            if id(node) in kept_ids and latency is not None:
                node["name"] = f'{node["name"]} [{latency:.0f}ms]'

    stats["kept"] = len(kept)
    return kept, stats


def spread_pick(buckets: dict[str, list[str]], limit: int) -> list[str]:
    """跨地区轮询取样：保证每个地区都有代表，不会让测速组被某一地区占满。"""
    total = sum(len(v) for v in buckets.values())
    if total <= limit:
        return [name for members in buckets.values() for name in members]

    order = sorted(buckets.values(), key=len, reverse=True)
    picked: list[str] = []
    index = 0
    while len(picked) < limit:
        added = False
        for members in order:
            if index < len(members):
                picked.append(members[index])
                added = True
                if len(picked) >= limit:
                    break
        if not added:
            break
        index += 1
    return picked


def build_groups(nodes: list[dict], base: dict, options: dict) -> list[dict]:
    """扩展模板里的 ALL_PROXIES / REGION_GROUPS 占位，并生成地区分组。"""
    names = [n["name"] for n in nodes]

    buckets: dict[str, list[str]] = {}
    for node in nodes:
        region, _ = detect_region(strip_flags(node["name"]))
        buckets.setdefault(region, []).append(node["name"])

    region_groups: list[dict] = []
    use_region = options["groups"]["by_region"] and len(buckets) > 1
    if use_region:
        for region in sorted(buckets, key=lambda r: -len(buckets[r])):
            flag = detect_region(region)[1]
            region_groups.append({
                "name": f"{flag} {region} · {len(buckets[region])}",
                "type": "select",
                "proxies": buckets[region] + ["DIRECT"],
            })
        top_members = [g["name"] for g in region_groups]
    else:
        top_members = names

    # 节点已按延迟升序，直接取前 N 个即"最快"
    fast_count = int(options["groups"].get("fast", {}).get("count", 0) or 0)
    fast_members = names[:fast_count] if fast_count > 0 else []

    auto = options["groups"]["auto_select"]
    max_nodes = int(auto.get("max_nodes", 80))
    auto_members = spread_pick(buckets, max_nodes) if max_nodes > 0 else names

    groups: list[dict] = []
    for raw in base.get("proxy-groups", []):
        group = dict(raw)
        expanded: list[str] = []
        for item in group.get("proxies") or []:
            if item == "ALL_PROXIES":
                expanded.extend(auto_members)
            elif item == "FAST_PROXIES":
                expanded.extend(fast_members or auto_members)
            elif item == "REGION_GROUPS":
                expanded.extend(top_members)
            else:
                expanded.append(item)

        seen, final = set(), []
        for item in expanded:
            if item not in seen:
                seen.add(item)
                final.append(item)
        group["proxies"] = final or ["DIRECT"]

        if group.get("type") in {"url-test", "fallback"}:
            group["url"] = auto["url"]
            group["interval"] = auto["interval"]
            group["tolerance"] = auto["tolerance"]
        groups.append(group)

    # 地区分组插在「直连/拦截」等固定组之前
    anchor = next(
        (i for i, g in enumerate(groups) if "直连" in str(g.get("name", ""))),
        len(groups),
    )
    return groups[:anchor] + region_groups + groups[anchor:]


def dump_config(config: dict, path: Path, stats: dict) -> None:
    header = [
        "# Clash 合并订阅 - 自动生成，请勿手动编辑",
        f"# 更新时间：{now_cst()}（UTC+8）",
        f"# 数据源：{stats['ok']} 个成功 / {stats['total']} 个",
        f"# 节点数：{stats['nodes']}（已去重，来源 {stats['raw']} 个）",
    ]
    if stats.get("alive") is not None:
        header.append(f"# 连通性：探测 {stats['tested']} 个，存活 {stats['alive']} 个"
                      f"（已按实测延迟升序排列，节点名后为延迟）")
    header.append("")
    body = yaml.safe_dump(
        config, allow_unicode=True, sort_keys=False,
        default_flow_style=False, width=4096,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(header) + body, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="合并 Clash 订阅")
    parser.add_argument("-s", "--sources", default=str(ROOT / "sources.txt"))
    parser.add_argument("-o", "--output", default=str(ROOT / "dist" / "config.yaml"))
    parser.add_argument("-c", "--config", default=str(ROOT / "config" / "base.yaml"))
    parser.add_argument("--options", default=str(ROOT / "config" / "options.yaml"))
    parser.add_argument("--no-speedtest", action="store_true", help="跳过延迟探测，只做合并去重")
    args = parser.parse_args()

    options = load_options(Path(args.options))
    base = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}

    entries = load_sources(Path(args.sources))
    if not entries:
        print("[错误] 没有配置任何订阅源：请填写 sources.txt 或设置 SUB_URLS", file=sys.stderr)
        return 1

    sources = expand_sources(entries)
    if not sources:
        print("[错误] 没有解析到任何可用订阅地址，保留原配置不覆盖", file=sys.stderr)
        return 1

    def work(url: str):
        try:
            text, _note = fetch_source(url, options)
            return url, parse_subscription(text), None
        except Exception as exc:  # noqa: BLE001
            return url, [], str(exc)

    raw_nodes: list[dict] = []
    ok = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for url, nodes, error in pool.map(work, sources):
            if error:
                print(f"[失败] {mask_url(url)} -> {error}")
                continue
            ok += 1
            print(f"[成功] {mask_url(url)} -> {len(nodes)} 个节点")
            raw_nodes.extend(nodes)

    kept: list[dict] = []
    seen_fp: set[str] = set()
    used_names: dict[str, int] = {}
    for node in raw_nodes:
        node = normalize(node)
        if not is_valid(node, options):
            continue
        if options["dedupe"]["enabled"]:
            fp = fingerprint(node)
            if fp in seen_fp:
                continue
            seen_fp.add(fp)
        kept.append(rename(node, options, used_names))

    if not kept:
        print("[错误] 没有解析到任何可用节点，保留原配置不覆盖", file=sys.stderr)
        return 1

    speed_stats = None
    if options["speedtest"]["enabled"] and not args.no_speedtest:
        print(f"[测速] 开始探测 {len(kept)} 个节点（并发 {options['speedtest']['workers']}，"
              f"超时 {options['speedtest']['timeout']}s）...")
        kept, speed_stats = apply_speedtest(kept, options)
        print(f"[测速] 存活 {speed_stats['alive']}/{speed_stats['tested']} "
              f"-> 保留 {speed_stats['kept']} 个节点")

    if not kept:
        print("[错误] 测速后没有可用节点，保留原配置不覆盖", file=sys.stderr)
        return 1

    config = dict(base)
    config["proxies"] = kept
    config["proxy-groups"] = build_groups(kept, base, options)

    out = Path(args.output)
    dump_config(config, out, {
        "ok": ok, "total": len(sources),
        "nodes": len(kept), "raw": len(raw_nodes),
        **(speed_stats or {}),
    })
    print(f"[完成] {len(raw_nodes)} -> {len(kept)} 个节点，已写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
