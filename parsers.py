"""各协议分享链接 -> Clash 节点对象。

支持：ss / vmess / vless / trojan / hysteria2(hy2) / tuic
不支持：ssr（Clash.Meta 已移除该类型，直接丢弃）
"""
from __future__ import annotations

import base64
import json
import re
import urllib.parse

SCHEME_RE = re.compile(r"(?:ss|vmess|vless|trojan|hysteria2|hysteria|hy2|tuic)://", re.I)


def extract_links(text: str) -> list[str]:
    """逐行提取分享链接。

    不能用「非空白字符」正则：节点名（# 后面）常含空格，会被截断。
    改为按行处理，同一行内以「下一个协议头」或行尾作为边界。
    """
    links: list[str] = []
    for line in (text or "").splitlines():
        starts = [m.start() for m in SCHEME_RE.finditer(line)]
        for i, start in enumerate(starts):
            end = starts[i + 1] if i + 1 < len(starts) else len(line)
            link = line[start:end].strip().strip('"').rstrip(",")
            if link:
                links.append(link)
    return links


def b64_decode(text: str) -> str | None:
    """尝试标准 / URL-safe base64 解码，失败返回 None。"""
    s = (text or "").strip()
    if not s:
        return None
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            return decoder(s + "=" * ((-len(s)) % 4)).decode("utf-8", errors="ignore")
        except Exception:
            continue
    return None


def split_host_port(text: str) -> tuple[str, int]:
    """解析 host:port，兼容 IPv6 与 Hysteria 端口跳跃（取首段）。"""
    text = text.strip().strip("/")
    if text.startswith("["):
        host, _, port = text.partition("]")
        host, port = host[1:], port.lstrip(":")
    elif text.count(":") > 1:
        host, port = text, "443"
    else:
        host, _, port = text.rpartition(":")
    port = re.split(r"\D", port)[0] if port else ""
    try:
        return host.strip("[]"), int(port)
    except ValueError:
        return host.strip("[]"), 0


def query_params(text: str) -> dict:
    return {k: v[0] for k, v in urllib.parse.parse_qs(text, keep_blank_values=True).items()}


def truthy(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def unquote(text: str) -> str:
    return urllib.parse.unquote(text or "").strip()


def _split_fragment(body: str) -> tuple[str, str]:
    if "#" in body:
        raw, frag = body.split("#", 1)
        return raw, unquote(frag)
    return body, ""


def _split_query(body: str) -> tuple[str, dict]:
    if "?" in body:
        raw, query = body.split("?", 1)
        return raw, query_params(query)
    return body, {}


def _clean(proxy: dict) -> dict | None:
    """去空值、校验必需字段。"""
    proxy = {k: v for k, v in proxy.items() if v not in (None, "", [], {})}
    if not proxy.get("server") or not proxy.get("port"):
        return None
    try:
        proxy["port"] = int(proxy["port"])
    except (TypeError, ValueError):
        return None
    proxy.setdefault("name", f'{proxy.get("type", "node")}-{proxy["server"]}')
    proxy["name"] = str(proxy["name"]).strip() or f'{proxy.get("type", "node")}-{proxy["server"]}'
    proxy.setdefault("udp", True)
    return proxy


def parse_ss(link: str) -> dict | None:
    body, name = _split_fragment(link[5:])
    body, q = _split_query(body)

    if "@" in body:  # SIP002
        userinfo, hostport = body.rsplit("@", 1)
        decoded = b64_decode(userinfo) or ""
        source = decoded if ":" in decoded else userinfo
    else:  # legacy：整段 base64
        decoded = b64_decode(body) or ""
        if "@" not in decoded:
            return None
        source, hostport = decoded.rsplit("@", 1)
        source = source.split(":", 1)[0] + ":" + source.split(":", 1)[1] if ":" in source else source
    if ":" not in source:
        return None
    method, password = source.split(":", 1)

    server, port = split_host_port(hostport)
    proxy = {
        "name": name or f"SS-{server}",
        "type": "ss",
        "server": server,
        "port": port,
        "cipher": method,
        "password": password,
    }

    plugin = q.get("plugin")
    if plugin:
        # 形如 obfs=http;obfs-host=bing.com  或  v2ray-plugin;mode=websocket;host=x;path=/;tls
        parts = dict(
            p.split("=", 1) if "=" in p else (p, "")
            for p in plugin.split(";") if p
        )
        kind = parts.pop("obfs", None)
        mode = parts.pop("mode", "")
        if plugin.startswith("v2ray-plugin") or mode:
            proxy["plugin"] = "v2ray-plugin"
            opts = {"mode": mode or "websocket"}
            if parts.get("host"):
                opts["host"] = parts["host"]
            if parts.get("path"):
                opts["path"] = parts["path"]
            if truthy(parts.get("tls")):
                opts["tls"] = True
            proxy["plugin-opts"] = opts
        else:
            proxy["plugin"] = "obfs"
            opts = {"mode": kind or mode or "http"}
            if q.get("obfs-host") or parts.get("host"):
                opts["host"] = q.get("obfs-host") or parts.get("host")
            proxy["plugin-opts"] = opts
    return _clean(proxy)


def parse_vmess(link: str) -> dict | None:
    raw = b64_decode(link[8:])
    if not raw:
        return None
    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        return None

    net = (info.get("net") or "tcp").lower()
    tls = str(info.get("tls") or "").lower() == "tls"
    host = info.get("host") or ""
    path = info.get("path") or "/"

    proxy = {
        "name": info.get("ps") or f'VMESS-{info.get("add")}',
        "type": "vmess",
        "server": info.get("add"),
        "port": info.get("port"),
        "uuid": info.get("id"),
        "alterId": int(info.get("aid") or 0),
        "cipher": info.get("scy") or "auto",
        "tls": tls,
        "skip-cert-verify": truthy(info.get("allowInsecure")),
    }
    if info.get("sni"):
        proxy["servername"] = info["sni"]
    if net == "ws":
        proxy["network"] = "ws"
        proxy["ws-opts"] = {"path": path, "headers": {"Host": host}} if host else {"path": path}
    elif net == "h2":
        proxy["network"] = "h2"
        proxy["h2-opts"] = {"host": [host] if host else [], "path": path}
    elif net == "grpc":
        proxy["network"] = "grpc"
        proxy["grpc-opts"] = {"grpc-service-name": path.lstrip("/")}
    elif net == "http":
        proxy["network"] = "http"
        proxy["http-opts"] = {"path": [path], "headers": {"Host": [host]}} if host else {"path": [path]}
    return _clean(proxy)


def parse_vless(link: str) -> dict | None:
    body, name = _split_fragment(link[8:])
    body, q = _split_query(body)
    if "@" not in body:
        return None
    uuid, hostport = body.rsplit("@", 1)
    server, port = split_host_port(hostport)

    security = (q.get("security") or "none").lower()
    net = (q.get("type") or "tcp").lower()
    host = q.get("host") or ""
    path = q.get("path") or "/"

    proxy = {
        "name": name or f"VLESS-{server}",
        "type": "vless",
        "server": server,
        "port": port,
        "uuid": uuid,
        "tls": security in {"tls", "reality", "xtls"},
        "skip-cert-verify": truthy(q.get("allowInsecure")) or security == "reality",
    }
    if q.get("flow"):
        proxy["flow"] = q["flow"]
    if q.get("sni"):
        proxy["servername"] = q["sni"]
    if q.get("fp"):
        proxy["client-fingerprint"] = q["fp"]
    if security == "reality":
        opts = {}
        if q.get("pbk"):
            opts["public-key"] = q["pbk"]
        if q.get("sid"):
            opts["short-id"] = q["sid"]
        if opts:
            proxy["reality-opts"] = opts
    if net == "ws":
        proxy["network"] = "ws"
        proxy["ws-opts"] = {"path": path, "headers": {"Host": host}} if host else {"path": path}
    elif net == "grpc":
        proxy["network"] = "grpc"
        proxy["grpc-opts"] = {"grpc-service-name": q.get("serviceName") or path.lstrip("/")}
    elif net == "http":
        proxy["network"] = "http"
        proxy["http-opts"] = {"path": [path]}
    return _clean(proxy)


def parse_trojan(link: str) -> dict | None:
    body, name = _split_fragment(link[9:])
    body, q = _split_query(body)
    if "@" not in body:
        return None
    password, hostport = body.rsplit("@", 1)
    server, port = split_host_port(hostport)

    net = (q.get("type") or "tcp").lower()
    host = q.get("host") or ""
    path = q.get("path") or "/"

    proxy = {
        "name": name or f"TROJAN-{server}",
        "type": "trojan",
        "server": server,
        "port": port,
        "password": urllib.parse.unquote(password),
        "skip-cert-verify": truthy(q.get("allowInsecure")),
    }
    if q.get("sni"):
        proxy["sni"] = q["sni"]
    if q.get("alpn"):
        proxy["alpn"] = q["alpn"].split(",")
    if q.get("fp"):
        proxy["client-fingerprint"] = q["fp"]
    if net == "ws":
        proxy["network"] = "ws"
        proxy["ws-opts"] = {"path": path, "headers": {"Host": host}} if host else {"path": path}
    elif net == "grpc":
        proxy["network"] = "grpc"
        proxy["grpc-opts"] = {"grpc-service-name": q.get("serviceName") or path.lstrip("/")}
    return _clean(proxy)


def parse_hysteria2(link: str) -> dict | None:
    body = re.sub(r"^(?:hysteria2|hy2)://", "", link)
    body, name = _split_fragment(body)
    body, q = _split_query(body)
    if "@" not in body:
        return None
    password, hostport = body.rsplit("@", 1)
    server, port = split_host_port(hostport)

    proxy = {
        "name": name or f"HY2-{server}",
        "type": "hysteria2",
        "server": server,
        "port": port,
        "password": urllib.parse.unquote(password) or None,
        "skip-cert-verify": truthy(q.get("insecure")),
    }
    if q.get("sni"):
        proxy["sni"] = q["sni"]
    if q.get("obfs"):
        proxy["obfs"] = q["obfs"]
    if q.get("obfs-password"):
        proxy["obfs-password"] = q["obfs-password"]
    if q.get("alpn"):
        proxy["alpn"] = q["alpn"].split(",")
    return _clean(proxy)


def parse_tuic(link: str) -> dict | None:
    body, name = _split_fragment(link[7:])
    body, q = _split_query(body)
    if "@" not in body:
        return None
    userinfo, hostport = body.rsplit("@", 1)
    if ":" not in userinfo:
        return None
    uuid, password = userinfo.split(":", 1)
    server, port = split_host_port(hostport)

    proxy = {
        "name": name or f"TUIC-{server}",
        "type": "tuic",
        "server": server,
        "port": port,
        "uuid": uuid,
        "password": password,
        "skip-cert-verify": truthy(q.get("allowInsecure") or q.get("insecure")),
    }
    if q.get("sni"):
        proxy["sni"] = q["sni"]
    if q.get("alpn"):
        proxy["alpn"] = q["alpn"].split(",")
    if q.get("congestion_control") or q.get("congestion-controller"):
        proxy["congestion-controller"] = q.get("congestion_control") or q.get("congestion-controller")
    return _clean(proxy)


PARSERS = {
    "ss": parse_ss,
    "vmess": parse_vmess,
    "vless": parse_vless,
    "trojan": parse_trojan,
    "hysteria2": parse_hysteria2,
    "hy2": parse_hysteria2,
    "tuic": parse_tuic,
}


def parse_link(link: str) -> dict | None:
    """解析单条分享链接，失败返回 None。"""
    link = link.strip().rstrip(",")
    scheme = link.split("://", 1)[0].lower()
    parser = PARSERS.get(scheme)
    if not parser:
        return None
    try:
        return parser(link)
    except Exception:
        return None


def parse_links(text: str) -> list[dict]:
    nodes = []
    for link in extract_links(text):
        node = parse_link(link)
        if node:
            nodes.append(node)
    return nodes
