"""Regression-тест на V-04: анонімні sender'и (sender_chat, без from_user)
мають потрапляти в dedup/rate-limit нарівні зі звичайними юзерами.
Запуск: pip install pytest && pytest -q"""
import os
from types import SimpleNamespace

# bot.py імпортує config.py, який fail-fast падає без BOT_TOKEN/allow-list —
# виставляємо ДО імпорту, нічого реального не зачіпає (ізольовано від прод-env).
os.environ.setdefault("BOT_TOKEN", "test:token")
os.environ.setdefault("ALLOW_OPEN_MODE", "true")

import bot


def _msg(user_id=None, sender_chat_id=None):
    from_user = SimpleNamespace(id=user_id) if user_id is not None else None
    sender_chat = SimpleNamespace(id=sender_chat_id) if sender_chat_id is not None else None
    return SimpleNamespace(from_user=from_user, sender_chat=sender_chat)


def test_sender_key_uses_user_id_when_present():
    assert bot._sender_key(_msg(user_id=123)) == 123


def test_sender_key_falls_back_to_sender_chat_for_anonymous():
    # from_user=None (лінкований канал/анонімний sender) — БЕЗ цього fallback
    # анонімні sender'и обходили dedup/rate-limit повністю (V-04).
    assert bot._sender_key(_msg(sender_chat_id=-100999)) == -100999


def test_sender_key_none_when_fully_anonymous():
    assert bot._sender_key(_msg()) is None


def test_sender_key_prefers_user_id_over_sender_chat():
    # Звичайний юзер у групі, де sender_chat теж може бути заданий (edge case) —
    # user_id має пріоритет.
    assert bot._sender_key(_msg(user_id=42, sender_chat_id=-100999)) == 42
