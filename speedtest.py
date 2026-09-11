#!/usr/bin/env python3
"""节点连通性与延迟探测。

用 TCP 直连 server:port 计时，失败即视为死节点。
注意：hysteria2 / tuic 走 UDP，TCP 探测必然失败，这类节点原样保留、不参与过滤。
"""
from __future__ import annotations

import socket
import time
from concurrent.futures import ThreadPoolExecutor

# 纯 UDP 协议，TCP 探测无意义
UDP_ONLY = {"hysteria", "hysteria2", "hy2", "tuic"}


def is_udp_only(node: dict) -> bool:
    return str(node.get("type", "")).lower() in UDP_ONLY


def _probe(host: str, port: int, timeout: float) -> float | None:
    """TCP 握手耗时（毫秒），失败返回 None。"""
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return (time.perf_counter() - start) * 1000
    except Exception:  # noqa: BLE001
        return None


def measure(nodes: list[dict], workers: int = 200, timeout: float = 3.0) -> dict[int, float]:
    """并发探测，返回 {节点下标: 延迟毫秒}。失败的不出现在结果里。"""
    targets = []
    for index, node in enumerate(nodes):
        if is_udp_only(node):
            continue
        try:
            port = int(node.get("port"))
        except (TypeError, ValueError):
            continue
        host = str(node.get("server", "")).strip()
        if not host or port <= 0:
            continue
        targets.append((index, host, port))

    if not targets:
        return {}

    results: dict[int, float] = {}
    with ThreadPoolExecutor(max_workers=min(workers, len(targets))) as pool:
        futures = {
            pool.submit(_probe, host, port, timeout): index
            for index, host, port in targets
        }
        # 逐个收结果：探测本身是并发的，单等上限防止 DNS 卡死拖垮整体
        for future, index in futures.items():
            try:
                latency = future.result(timeout=timeout + 3)
            except Exception:  # noqa: BLE001
                continue
            if latency is not None:
                results[index] = latency
    return results
