"""SSRF-захист. Єдиний барʼєр між недовіреним URL і вихідним запитом.
Перевіряє ВСІ резолвлені адреси (A+AAAA), нормалізує IPv4-mapped IPv6,
блокує приватні/loopback/link-local/reserved діапазони, нестандартні порти
й внутрішні host-суфікси. Викликається перед чергою, на КОЖЕН запит Chromium
(route handler) і після редиректів httpx."""
import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

# Явний блок-лист на додачу до is_global-перевірки (нижче) — пояснює намір
# і ловить крайні випадки (0.0.0.0/8, IPv4-mapped, NAT64 тощо).
BLOCKED_NETS = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "169.254.0.0/16",        # link-local + хмарний IMDS (169.254.169.254)
    "100.64.0.0/10",         # CGNAT
    "198.18.0.0/15",         # benchmarking
    "192.0.0.0/24", "192.0.2.0/24",
    "224.0.0.0/4", "240.0.0.0/4", "255.255.255.255/32",
    "::1/128", "fc00::/7", "fe80::/10",
    "::ffff:0:0/96",         # IPv4-mapped IPv6
    "64:ff9b::/96",          # NAT64
    "2001:db8::/32",
)]

# Тільки веб-порти. Звичайний користувач не кидає посилання на :6379 (Redis),
# :9200 (Elasticsearch), :2375 (docker) — а атака на внутрішні сервіси кидає.
ALLOWED_PORTS = {80, 443}

# Внутрішні DNS-суфікси (K8s / service mesh / consul). secure-by-default.
BLOCKED_HOST_SUFFIXES = (
    ".internal", ".local", ".localhost", ".cluster.local", ".svc", ".consul",
)


def _ip_blocked(ip_str: str) -> bool:
    obj = ipaddress.ip_address(ip_str)
    if isinstance(obj, ipaddress.IPv6Address) and obj.ipv4_mapped:
        obj = obj.ipv4_mapped  # ::ffff:127.0.0.1 -> 127.0.0.1
    return (
        obj.is_private or obj.is_loopback or obj.is_link_local
        or obj.is_multicast or obj.is_reserved or obj.is_unspecified
        or not obj.is_global
        or any(obj in net for net in BLOCKED_NETS)
    )


async def is_safe(url: str) -> bool:
    """True лише якщо схема http(s), порт дозволений, host не внутрішній,
    і ВСІ його IP (A+AAAA) — публічні. Будь-яка помилка резолву → False
    (безпечний дефолт). DNS-резолв ідe через loop.getaddrinfo (thread executor),
    щоб не блокувати event loop — інакше один повільний/застиглий резолвер
    стопорить увесь бот (черга, /health, всі чати) на час system DNS timeout."""
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return False
        host = p.hostname
        if not host:
            return False
        host = host.rstrip(".")  # "evil.com." == "evil.com" для DNS
        if host == "localhost" or any(host.endswith(s) for s in BLOCKED_HOST_SUFFIXES):
            return False
        port = p.port or (443 if p.scheme == "https" else 80)
        if port not in ALLOWED_PORTS:
            return False
        # Літерал-IP (без DNS).
        try:
            ipaddress.ip_address(host)
            return not _ip_blocked(host)
        except ValueError:
            pass
        # Доменне імʼя: резолвимо ВСІ адреси, кожна має бути публічною.
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        ips = {info[4][0] for info in infos}
        if not ips:
            return False
        return all(not _ip_blocked(ip) for ip in ips)
    except Exception:
        return False
