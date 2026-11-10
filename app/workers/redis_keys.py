"""Redis key layout and Lua scripts shared by the worker modules.

Every key lives here so a typo cannot silently split state between two workers: a
mistyped session key, for example, lets two workers attach the same Telegram session.
"""
from __future__ import annotations

SESSION_LEASE_TTL_SECONDS = 90
SESSION_LEASE_RENEW_SECONDS = 20

# Compare-and-delete: only the owner may release a lease or a lock.
CAS_DELETE = (
    "if redis.call('get', KEYS[1]) == ARGV[1] "
    "then return redis.call('del', KEYS[1]) else return 0 end"
)
# Compare-and-expire: the owner proves it is alive without changing the token.
CAS_EXPIRE = (
    "if redis.call('get', KEYS[1]) == ARGV[1] "
    "then return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end"
)

WORKER_HEARTBEAT = "aigopushtg:worker:last_seen"


def session_lease(account_id: int) -> str:
    return f"aigopushtg:telegram-session:{account_id}"


def challenge_seen(account_id: int, chat_id: int, message_id: int) -> str:
    return f"aigopushtg:challenge:seen:{account_id}:{chat_id}:{message_id}"


def challenge_cooldown(account_id: int, chat_id: int) -> str:
    return f"aigopushtg:challenge:cooldown:{account_id}:{chat_id}"


def challenge_bot_dm(account_id: int, bot_username: str) -> str:
    return f"aigopushtg:challenge:bot:{account_id}:{bot_username.lower()}"


def chat_memory(account_id: int, chat_id: int) -> str:
    return f"aigopushtg:memory:{account_id}:{chat_id}:chat"


def person_memory(account_id: int, chat_id: int, sender_id: int) -> str:
    return f"aigopushtg:memory:{account_id}:{chat_id}:person:{sender_id}"


def autoresponder_message(account_id: int, chat_id: int, message_id: int) -> str:
    return f"aigopushtg:autoresponder:message:{account_id}:{chat_id}:{message_id}"


def autoresponder_cooldown(account_id: int, sender_id: int) -> str:
    return f"aigopushtg:autoresponder:cooldown:{account_id}:{sender_id}"


def trigger_lock(account_id: int) -> str:
    return f"aigopushtg:trigger-lock:{account_id}"


def trigger_cooldown(account_id: int, trigger_id: int) -> str:
    return f"aigopushtg:trigger-cooldown:{account_id}:{trigger_id}"


def trigger_message_cooldown(account_id: int) -> str:
    return f"aigopushtg:trigger-message-cooldown:{account_id}"


def reply_cooldown(account_id: int, chat_id: int) -> str:
    return f"aigopushtg:reply-cooldown:{account_id}:{chat_id}"


def learning_buffer(account_id: int, started_at_timestamp: int) -> str:
    return f"aigopushtg:learning:{account_id}:{started_at_timestamp}"


def channel_post(account_id: int, channel_id: int, post_id: int) -> str:
    return f"aigopushtg:channel-post:{account_id}:{channel_id}:{post_id}"


def channel_cooldown_lock(account_id: int, channel_id: int) -> str:
    return f"aigopushtg:channel-cooldown-lock:{account_id}:{channel_id}"


def channel_reply_cooldown(account_id: int, channel_id: int) -> str:
    return f"aigopushtg:channel-reply-cooldown:{account_id}:{channel_id}"


def log_destination_key(category: str) -> str:
    return f"log_chat_ids_{category}"
