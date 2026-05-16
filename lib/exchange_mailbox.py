#!/usr/bin/env python3
"""
Exchange Mailbox Executive Assistant Agent
"""

import os
import sys
import json
import time
import sqlite3
import hashlib
import logging
import textwrap
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple, Any

# VIPs - high priority senders
VIPS = {
    "mike hancock", "gar diu", "steve shepherd", "les gray", "mark harman",
    "recipero.com", "recipero.net"
}

NOISE_PATTERNS = [
    "newsletter", "marketing", "promotion", "digest", "alert", "notification",
    "cron", "security update", "system update", "automated message", "noreply"
]

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

class StateStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.init_db()
    
    def init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""CREATE TABLE IF NOT EXISTS mailbox_state (key TEXT PRIMARY KEY, value TEXT)""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS message_index (
                message_id TEXT PRIMARY KEY, subject TEXT, received_datetime TEXT,
                is_read INTEGER, importance TEXT, categories TEXT, has_attachment INTEGER,
                sender_name TEXT, sender_email TEXT, preview TEXT, body_preview TEXT,
                run_id TEXT, priority INTEGER, processed_at TEXT)""")
            conn.commit()
    
    def get(self, key: str, default: Any = None) -> Any:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT value FROM mailbox_state WHERE key = ?", (key,))
            row = cursor.fetchone()
            return json.loads(row[0]) if row else default
    
    def set(self, key: str, value: Any):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT OR REPLACE INTO mailbox_state (key, value) VALUES (?, ?)",
                        (key, json.dumps(value)))
            conn.commit()
    
    def add_message(self, message_data: Dict, run_id: str, priority: int):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""INSERT OR REPLACE INTO message_index VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (message_data.get("id", ""), message_data.get("subject", ""),
                 message_data.get("receivedDateTime", ""), message_data.get("isRead", 0),
                 message_data.get("importance", "normal"), json.dumps(message_data.get("categories", [])),
                 message_data.get("hasAttachment", 0),
                 message_data.get("sender", {}).get("name", ""),
                 message_data.get("sender", {}).get("emailAddress", {}).get("address", ""),
                 message_data.get("preview", ""), message_data.get("bodyPreview", ""),
                 run_id, priority, datetime.utcnow().isoformat()))
            conn.commit()

def is_vips(sender_name: str, sender_email: str) -> bool:
    sender_lower = (sender_name or "").lower()
    email_lower = (sender_email or "").lower()
    for vip in VIPS:
        if vip in sender_lower or vip in email_lower:
            return True
    return False

def is_noise(subject: str, sender_name: str, sender_email: str, body_preview: str) -> bool:
    text = f"{subject} {sender_name} {sender_email} {body_preview}".lower()
    for pattern in NOISE_PATTERNS:
        if pattern in text:
            return True
    noise_indicators = ["unsubscribe", "list-id", "mailing list", "google groups", "slack",
                        "teams", "calendar invite", "out of office", "automatic reply",
                        "delivery status", "bounce", "mailer-daemon"]
    for indicator in noise_indicators:
        if indicator in text:
            return True
    return False

def has_action_language(text: str) -> bool:
    action_phrases = ["please", "kindly", "action required", "please review", "please consider",
                      "please advise", "please confirm", "request", "require", "need", "urgent",
                      "asap", "deadline", "due", "time-sensitive", "priority", "help", "assist"]
    text_lower = text.lower()
    return any(phrase in text_lower for phrase in action_phrases)

def has_financial_context(text: str) -> bool:
    financial_terms = ["invoice", "payment", "bill", "receipt", "refund", "expense",
                       "budget", "finance", "tax", "contract", "agreement", "legal",
                       "security", "breach", "gdpr", "privacy", "confidential"]
    text_lower = text.lower()
    return any(term in text_lower for term in financial_terms)

def has_deadline(text: str) -> bool:
    deadline_phrases = ["deadline", "due date", "due by", "by end of", "by friday",
                        "by monday", "urgent", "asap", "immediate", "time sensitive"]
    text_lower = text.lower()
    return any(phrase in text_lower for phrase in deadline_phrases)

def calculate_score(message: Dict) -> int:
    subject = message.get("subject", "")
    preview = message.get("preview", "")
    body_preview = message.get("bodyPreview", "")
    sender_name = message.get("sender", {}).get("name", "")
    sender_email = message.get("sender", {}).get("emailAddress", {}).get("address", "")
    is_unread = message.get("isRead", False)
    importance = message.get("importance", "normal")
    has_attachment = message.get("hasAttachment", False)
    
    # Early return for server notifications
    if is_noise(subject, sender_name, sender_email, body_preview):
        return -30
    
    score = 0
    if is_vips(sender_name, sender_email):
        score += 25
    if not is_noise(subject, sender_name, sender_email, body_preview):
        score += 15
    if is_unread:
        score += 8
    if importance == "high":
        score += 10
    if has_action_language(preview) or has_action_language(body_preview):
        score += 20
    if has_deadline(preview) or has_deadline(body_preview):
        score += 20
    if has_financial_context(preview) or has_financial_context(body_preview):
        score += 25
    if has_attachment:
        score += 8
    
    return score

def classify_priority(score: int) -> Tuple[int, str]:
    if score >= 70:
        return 0, "P0"
    elif score >= 40:
        return 1, "P1"
    elif score >= 15:
        return 2, "P2"
    else:
        return 3, "P3"

def fetch_mailbox_delta() -> List[Dict]:
    """Fetch messages from mailbox - simulated for now."""
    # In production, this would call Microsoft Graph API
    # For read-only pilot mode, return empty to indicate no new messages
    logger.info("Mailbox sync (read-only pilot mode)")
    return []

def main():
    db_path = os.path.expanduser("~/.hermes/workspace/exchange_mailbox.sqlite3")
    state_store = StateStore(db_path)
    run_id = datetime.utcnow().isoformat()
    
    messages = fetch_mailbox_delta()
    
    if not messages:
        print("[SILENT]")
        return
    
    summary = {"p0": [], "p1": [], "p2": [], "p3": []}
    
    for msg in messages:
        score = calculate_score(msg)
        priority, _ = classify_priority(score)
        state_store.add_message(msg, run_id, priority)
        
        if priority == 0:
            summary["p0"].append(msg)
        elif priority == 1:
            summary["p1"].append(msg)
        elif priority == 2:
            summary["p2"].append(msg)
        else:
            summary["p3"].append(msg)
    
    output_lines = []
    
    if summary["p0"]:
        output_lines.append("P0 - Immediate action required:")
        for msg in summary["p0"]:
            output_lines.append(f"  {msg.get('subject', 'No subject')}")
    
    if summary["p1"]:
        output_lines.append("P1 - This week:")
        for msg in summary["p1"]:
            output_lines.append(f"  {msg.get('subject', 'No subject')}")
    
    if summary["p2"]:
        output_lines.append(f"P2 - Track ({len(summary['p2'])} messages)")
    
    if summary["p3"]:
        output_lines.append(f"Cleaned up {len(summary['p3'])} automated messages")
    
    if output_lines:
        print("\n".join(output_lines))
    else:
        print("[SILENT]")

if __name__ == "__main__":
    main()
