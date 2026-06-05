"""Smoke-тести SSRF-фільтра. Запуск: pip install pytest && pytest -q"""
import socket
import security


def test_blocks_localhost_literal():
    assert security.is_safe("http://127.0.0.1/") is False
    assert security.is_safe("http://[::1]/") is False

def test_blocks_localhost_name():
    assert security.is_safe("http://localhost/") is False

def test_blocks_private():
    assert security.is_safe("http://10.0.0.5/") is False
    assert security.is_safe("http://192.168.1.1/") is False
    assert security.is_safe("http://172.16.0.1/") is False

def test_blocks_cloud_metadata():
    assert security.is_safe("http://169.254.169.254/") is False

def test_blocks_unspecified():
    assert security.is_safe("http://0.0.0.0:8000/") is False

def test_blocks_decimal_localhost():
    # 2130706433 == 127.0.0.1; резолв цілого у loopback АБО gaierror → у будь-якому разі False
    assert security.is_safe("http://2130706433/") is False

def test_blocks_non_http_scheme():
    assert security.is_safe("file:///etc/passwd") is False
    assert security.is_safe("gopher://127.0.0.1/") is False
    assert security.is_safe("ftp://example.com/") is False

def test_blocks_nonstandard_port():
    # навіть на «публічному» хості :6379 не пускаємо (внутрішній Redis)
    assert security.is_safe("http://example.com:6379/") is False

def test_blocks_internal_suffix():
    assert security.is_safe("http://api.internal/") is False
    assert security.is_safe("http://db.svc/") is False

def test_blocks_dual_stack_when_any_ip_private(monkeypatch):
    # A=публічний, AAAA=loopback → блок (rebinding / dual-stack)
    def fake_getaddrinfo(host, *a, **k):
        return [
            (socket.AF_INET, 0, 0, "", ("1.2.3.4", 0)),
            (socket.AF_INET6, 0, 0, "", ("::1", 0)),
        ]
    monkeypatch.setattr(security.socket, "getaddrinfo", fake_getaddrinfo)
    assert security.is_safe("http://dual.example/") is False

def test_allows_public(monkeypatch):
    def fake_getaddrinfo(host, *a, **k):
        return [(socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))]
    monkeypatch.setattr(security.socket, "getaddrinfo", fake_getaddrinfo)
    assert security.is_safe("https://example.com/") is True
