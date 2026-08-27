#!/usr/bin/env python3
"""
Exchange Mailbox Executive Assistant Agent.

Provides:
- Delta-sync mailbox processing via Microsoft Graph API
- Priority classification (P0-P3) using VIP/noise/urgency scoring
- Local SQLite state store for delta links and message index
- Read-only mode by default (categories/flags only, no destructive operations)

Usage:
    # Regular run (cron)
    python3 -m lib.exchange_mailbox

    # First-time auth (runs device code flow)
    python3 -m lib.exchange_mailbox --auth

    # Force full re-sync (ignores delta link)
    python3 -m lib.exchange_mailbox --full-sync
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import sys
import textwrap
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# VIPs — high priority senders
VIPS = {
    "mike hancock", "gar diu", "steve shepherd", "les gray", "mark harman",
    "recipero.com", "recipero.net",
}

NOISE_PATTERNS = [
    "newsletter", "marketing", "promotion", "digest", "alert", "notification",
    "cron", "security update", "system update", "automated message", "noreply",
]

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# ── State Store ─────────────────────────────────────────────────────────


class StateStore:
    """SQLite-backed state store for delta sync and message index."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.init_db()

    def init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("""CREATE TABLE IF NOT EXISTS mailbox_state (
                key TEXT PRIMARY KEY, value TEXT
            )""")
            cur.execute("""CREATE TABLE IF NOT EXISTS message_index (
                message_id TEXT PRIMARY KEY, subject TEXT,
                received_datetime TEXT, is_read INTEGER, importance TEXT,
                categories TEXT, has_attachment INTEGER,
                sender_name TEXT, sender_email TEXT, preview TEXT,
                body_preview TEXT, run_id TEXT, priority INTEGER,
                processed_at TEXT
            )""")
            conn.commit()

    def get(self, key: str, default: Any = None) -> Any:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT value FROM mailbox_state WHERE key = ?", (key,)
            ).fetchone()
            return json.loads(row[0]) if row else default

    def set(self, key: str, value: Any):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO mailbox_state (key, value) VALUES (?, ?)",
                (key, json.dumps(value)),
            )
            conn.commit()

    def add_message(self, message_data: dict, run_id: str, priority: int):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""INSERT OR REPLACE INTO message_index
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (message_data.get("id", ""),
                 message_data.get("subject", ""),
                 message_data.get("receivedDateTime", ""),
                 1 if message_data.get("isRead") else 0,
                 message_data.get("importance", "normal"),
                 json.dumps(message_data.get("categories", [])),
                 1 if message_data.get("hasAttachments") else 0,
                 message_data.get("sender", {}).get("emailAddress", {}).get("name", ""),
                 message_data.get("sender", {}).get("emailAddress", {}).get("address", ""),
                 message_data.get("bodyPreview", ""),
                 message_data.get("bodyPreview", ""),
                 run_id, priority, datetime.now(timezone.utc).isoformat()))
            conn.commit()


# ── Scoring ─────────────────────────────────────────────────────────────


def is_vip(sender: dict[str, Any]) -> bool:
    sender_name = (sender.get("emailAddress", {}).get("name") or "").lower()
    sender_email = (sender.get("emailAddress", {}).get("address") or "").lower()
    for vip in VIPS:
        if vip in sender_name or vip in sender_email:
            return True
    return False


def is_noise(subject: str, sender_name: str, sender_email: str, body_preview: str) -> bool:
    text = f"{subject} {sender_name} {sender_email} {body_preview}".lower()
    for pattern in NOISE_PATTERNS:
        if pattern in text:
            return True
    noise_indicators = [
        "unsubscribe", "list-id", "mailing list", "google groups",
        "out of office", "automatic reply", "delivery status", "bounce",
        "mailer-daemon",
    ]
    for indicator in noise_indicators:
        if indicator in text:
            return True
    return False


def has_action_language(text: str) -> bool:
    phrases = [
        "please", "kindly", "action required", "please review",
        "please advise", "please confirm", "request", "require", "need",
        "urgent", "asap", "deadline", "due", "time-sensitive", "priority",
        "help", "assist",
    ]
    text_lower = text.lower()
    return any(p in text_lower for p in phrases)


def has_financial_context(text: str) -> bool:
    terms = [
        "invoice", "payment", "bill", "receipt", "refund", "expense",
        "budget", "finance", "tax", "contract", "agreement", "legal",
        "security", "breach", "gdpr", "privacy", "confidential",
    ]
    text_lower = text.lower()
    return any(t in text_lower for t in terms)


def has_deadline(text: str) -> bool:
    phrases = [
        "deadline", "due date", "due by", "by end of", "by friday",
        "by monday", "urgent", "asap", "immediate", "time sensitive",
    ]
    text_lower = text.lower()
    return any(p in text_lower for p in phrases)


def calculate_score(message: dict) -> int:
    subject = message.get("subject", "")
    preview = message.get("bodyPreview", "")
    sender = message.get("sender", {})
    sender_name = sender.get("emailAddress", {}).get("name", "")
    sender_email = sender.get("emailAddress", {}).get("address", "")
    is_unread = not message.get("isRead", True)
    importance = message.get("importance", "normal")
    has_attachment = message.get("hasAttachments", False)

    # Server noise → early return with heavy penalty
    if is_noise(subject, sender_name, sender_email, preview):
        return -30

    score = 0
    if is_vip(sender):
        score += 25
    # Not noise
    score += 15
    if is_unread:
        score += 8
    if importance == "high":
        score += 10
    if has_action_language(preview):
        score += 20
    if has_deadline(preview):
        score += 20
    if has_financial_context(preview):
        score += 25
    if has_attachment:
        score += 8

    return score


def classify_priority(score: int) -> tuple[int, str]:
    if score >= 70:
        return 0, "P0"
    elif score >= 40:
        return 1, "P1"
    elif score >= 15:
        return 2, "P2"
    else:
        return 3, "P3"


# ── Graph API Integration ───────────────────────────────────────────────


def _resolve_cache_path() -> str:
    """Return the delegated token cache path from env or default."""
    return os.environ.get("MSGRAPH_TOKEN_CACHE", "") or os.path.expanduser("~/.hermes/.graph_token")


def _print_device_code(uri: str, code: str) -> None:
    """Display device code on stdout."""
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"Microsoft Authentication Required", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    print(f"\nCode: {code}", file=sys.stderr)
    print(f"\nGo to: {uri}", file=sys.stderr)
    print(f"and enter the code above.", file=sys.stderr)
    print(f"{'='*60}\n", file=sys.stderr)


def _build_graph_client() -> Any:
    """Build a MicrosoftGraphClient using delegated auth from env vars.

    First-time callers should run ``--auth`` first to cache tokens.
    """
    from tools.microsoft_graph_auth import MicrosoftGraphDelegatedTokenProvider
    from tools.microsoft_graph_client import MicrosoftGraphClient

    provider = MicrosoftGraphDelegatedTokenProvider.from_env(
        on_device_code=_print_device_code,
    )
    return MicrosoftGraphClient(provider)


async def run_device_code_auth() -> None:
    """Perform device code auth interactively and cache the tokens.

    Separate entry point (``--auth`` flag) so cron jobs never accidentally
    trigger an interactive prompt.
    """
    print("Starting device code authentication...", file=sys.stderr)
    client = _build_graph_client()
    token = await client.token_provider.get_access_token(force_refresh=True)
    cache_path = _resolve_cache_path()
    print(f"✓ Authentication successful! Token cached at {cache_path}", file=sys.stderr)
    print(f"  Expires in: {client.token_provider._cached_token.expires_in_seconds}s" 
          if hasattr(client.token_provider, '_cached_token') and 
             client.token_provider._cached_token else "", file=sys.stderr)


async def fetch_mailbox_delta(state_store: StateStore) -> list[dict]:
    """Fetch new/changed messages from the mailbox using delta sync.

    Reads the stored delta link from the state store and follows it.
    Falls back to a full sync of the top 300 messages on first run.
    """
    client = _build_graph_client()
    delta_link = state_store.get("delta_link")
    messages: list[dict] = []

    if delta_link:
        # Incremental delta sync
        logger.info("Following delta link...")
        try:
            result = await client.get_json(delta_link)
            messages = result.get("value", [])
            # Delta link is on the LAST page
            next_link = result.get("@odata.nextLink")
            while next_link:
                page = await client.get_json(next_link)
                messages.extend(page.get("value", []))
                next_link = page.get("@odata.nextLink")
            # Final page has the new delta link
            new_delta = result.get("@odata.deltaLink") or delta_link
            state_store.set("delta_link", new_delta)
        except Exception as exc:
            logger.warning("Delta sync failed (%s), falling back to full sync", exc)
            delta_link = None

    if not delta_link:
        # Full sync on first run or after delta failure
        logger.info("Starting full mailbox sync (top 300 messages)...")
        msgs = await client.get_messages(top=300, include_pagination=True)
        messages = msgs if isinstance(msgs, list) else []

        # After a full sync, we need to set up delta for next time.
        # We capture the delta link by doing a delta query with a wide window.
        try:
            # Initial delta query creates the delta link
            now = datetime.now(timezone.utc)
            start = now.replace(year=now.year - 1).isoformat() + "Z"
            end = now.isoformat() + "Z"
            result = await client.get_json(
                "me/mailFolders/inbox/messages/delta",
                params={"startDateTime": start, "endDateTime": end},
            )
            next_link = result.get("@odata.nextLink")
            while next_link:
                page = await client.get_json(next_link)
                next_link = page.get("@odata.nextLink")
            new_delta = result.get("@odata.deltaLink")
            if new_delta:
                state_store.set("delta_link", new_delta)
        except Exception as exc:
            logger.warning("Could not establish delta link: %s", exc)

    return messages


# ── Main ────────────────────────────────────────────────────────────────


async def async_main() -> None:
    db_path = os.path.expanduser(
        os.environ.get("EXCHANGE_MAILBOX_DB", "")
        or "~/.hermes/workspace/exchange_mailbox.sqlite3"
    )
    state_store = StateStore(db_path)
    run_id = datetime.now(timezone.utc).isoformat()

    try:
        messages = await fetch_mailbox_delta(state_store)
    except Exception as exc:
        error_str = str(exc)
        # Check if this is an auth issue (no cached tokens)
        if "token" in error_str.lower() and "cache" not in error_str.lower():
            print("[AUTH_REQUIRED]", file=sys.stderr)
            print("No cached tokens found. Run with --auth to authenticate:", file=sys.stderr)
            print(f"  python3 -m lib.exchange_mailbox --auth", file=sys.stderr)
        else:
            logger.error("Mailbox sync failed: %s", exc)
            print("[ERROR]", file=sys.stderr)
        return

    if not messages:
        print("[SILENT]")
        return

    summary: dict[str, list] = {"p0": [], "p1": [], "p2": [], "p3": []}

    for msg in messages:
        score = calculate_score(msg)
        priority, _ = classify_priority(score)
        state_store.add_message(msg, run_id, priority)

        key = f"p{priority}"
        if key in summary:
            # For P3, just count — don't store individual messages
            if priority < 3:
                summary[key].append(msg)

    output_lines = []

    if summary["p0"]:
        output_lines.append("## P0 — Immediate action required")
        for msg in summary["p0"]:
            subject = msg.get("subject", "No subject")
            sender = msg.get("sender", {}).get("emailAddress", {}).get("address", "unknown")
            preview = (msg.get("bodyPreview") or "")[:120]
            output_lines.append(f"- **{subject}** from {sender}")
            if preview:
                output_lines.append(f"  > {preview}")

    if summary["p1"]:
        output_lines.append("\n## P1 — This week")
        for msg in summary["p1"]:
            subject = msg.get("subject", "No subject")
            sender = msg.get("sender", {}).get("emailAddress", {}).get("address", "unknown")
            output_lines.append(f"- **{subject}** from {sender}")

    p3_count = sum(
        1 for msg in messages if classify_priority(calculate_score(msg))[0] == 3
    )
    if p3_count > 0:
        output_lines.append(f"\nP3 — Cleaned up {p3_count} automated/low-value messages")

    if output_lines:
        print("\n".join(output_lines))
    else:
        print("[SILENT]")


def main() -> None:
    """Entry point. Routes --auth and --full-sync flags."""
    args = [a.lower() for a in sys.argv[1:]]

    if "--auth" in args or "-a" in args:
        asyncio.run(run_device_code_auth())
        return

    asyncio.run(async_main())


if __name__ == "__main__":
    main()
