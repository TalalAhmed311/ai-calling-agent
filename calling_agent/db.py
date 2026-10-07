"""SQLite tables for CSV campaigns and leads."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from calling_agent import config

_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.LEADS_DB, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init() -> None:
    with _LOCK:
        conn = connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS campaigns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    filename TEXT,
                    phone_column TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'idle',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
                    phone TEXT NOT NULL,
                    name TEXT,
                    fields TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'pending',
                    call_control_id TEXT,
                    disposition TEXT,
                    hangup_cause TEXT,
                    error TEXT,
                    recording TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_leads_campaign ON leads(campaign_id, status);
                CREATE INDEX IF NOT EXISTS idx_leads_ccid ON leads(call_control_id);

                CREATE TABLE IF NOT EXISTS situations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    node TEXT NOT NULL,
                    situation_key TEXT NOT NULL,
                    user_text TEXT NOT NULL,
                    reply_text TEXT NOT NULL,
                    clip_id TEXT NOT NULL,
                    hit_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(node, situation_key)
                );
                CREATE INDEX IF NOT EXISTS idx_situations_node_key
                    ON situations(node, situation_key);
                CREATE INDEX IF NOT EXISTS idx_situations_clip
                    ON situations(clip_id);
                """
            )
            try:
                conn.execute("ALTER TABLE leads ADD COLUMN recording TEXT")
            except sqlite3.OperationalError:
                pass
            conn.commit()
        finally:
            conn.close()


def get_situation(node: str, situation_key: str) -> dict[str, Any] | None:
    with _LOCK:
        conn = connect()
        try:
            row = conn.execute(
                "SELECT * FROM situations WHERE node = ? AND situation_key = ?",
                (node, situation_key),
            ).fetchone()
            return _row(row)
        finally:
            conn.close()


def find_situation_fuzzy(node: str, situation_key: str, limit: int = 40) -> dict[str, Any] | None:
    """Exact key first, then substring / containment match within the node."""
    exact = get_situation(node, situation_key)
    if exact:
        return exact
    if len(situation_key) < 8:
        return None
    with _LOCK:
        conn = connect()
        try:
            rows = conn.execute(
                "SELECT * FROM situations WHERE node = ? ORDER BY hit_count DESC, id DESC LIMIT ?",
                (node, limit),
            ).fetchall()
        finally:
            conn.close()
    for row in rows:
        d = _row(row)
        if not d:
            continue
        key = str(d.get("situation_key") or "")
        if not key:
            continue
        if key in situation_key or situation_key in key:
            return d
    return None


def upsert_situation(
    *,
    node: str,
    situation_key: str,
    user_text: str,
    reply_text: str,
    clip_id: str,
) -> dict[str, Any]:
    now = _now()
    with _LOCK:
        conn = connect()
        try:
            conn.execute(
                """
                INSERT INTO situations
                    (node, situation_key, user_text, reply_text, clip_id,
                     hit_count, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(node, situation_key) DO UPDATE SET
                    user_text = excluded.user_text,
                    reply_text = excluded.reply_text,
                    clip_id = excluded.clip_id,
                    updated_at = excluded.updated_at
                """,
                (node, situation_key, user_text, reply_text, clip_id, now, now),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM situations WHERE node = ? AND situation_key = ?",
                (node, situation_key),
            ).fetchone()
            return _row(row) or {}
        finally:
            conn.close()


def bump_situation_hit(situation_id: int) -> None:
    with _LOCK:
        conn = connect()
        try:
            conn.execute(
                """
                UPDATE situations
                SET hit_count = hit_count + 1, updated_at = ?
                WHERE id = ?
                """,
                (_now(), situation_id),
            )
            conn.commit()
        finally:
            conn.close()


def list_situations(limit: int = 500) -> list[dict[str, Any]]:
    with _LOCK:
        conn = connect()
        try:
            rows = conn.execute(
                "SELECT * FROM situations ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [_row(r) for r in rows if r is not None]
        finally:
            conn.close()


def normalize_phone(raw: str) -> str | None:
    """Turn a spreadsheet cell into E.164, or None if unusable."""
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("+"):
        digits = re.sub(r"\D", "", text)
        return f"+{digits}" if len(digits) >= 8 else None
    digits = re.sub(r"\D", "", text)
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if 8 <= len(digits) <= 15:
        return f"+{digits}"
    return None


def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    if r is None:
        return None
    d = dict(r)
    if "fields" in d and isinstance(d["fields"], str):
        try:
            d["fields"] = json.loads(d["fields"] or "{}")
        except json.JSONDecodeError:
            d["fields"] = {}
    return d


def create_campaign(
    name: str,
    phone_column: str,
    rows: list[dict[str, Any]],
    *,
    filename: str = "",
) -> dict[str, Any]:
    init()
    now = _now()
    with _LOCK:
        conn = connect()
        try:
            cur = conn.execute(
                "INSERT INTO campaigns (name, filename, phone_column, status, created_at, updated_at) "
                "VALUES (?, ?, ?, 'idle', ?, ?)",
                (name, filename, phone_column, now, now),
            )
            cid = int(cur.lastrowid)
            inserted = 0
            skipped = 0
            for row in rows:
                phone = normalize_phone(str(row.get("phone") or ""))
                if not phone:
                    skipped += 1
                    continue
                conn.execute(
                    "INSERT INTO leads (campaign_id, phone, name, fields, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                    (
                        cid,
                        phone,
                        (row.get("name") or "")[:120] or None,
                        json.dumps(row.get("fields") or {}, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
                inserted += 1
            conn.commit()
        finally:
            conn.close()
    camp = get_campaign(cid)
    assert camp is not None
    camp["inserted"] = inserted
    camp["skipped"] = skipped
    return camp


def list_campaigns() -> list[dict[str, Any]]:
    init()
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT c.*,
                   COUNT(l.id) AS total,
                   SUM(CASE WHEN l.status='pending' THEN 1 ELSE 0 END) AS pending,
                   SUM(CASE WHEN l.status='dialing' THEN 1 ELSE 0 END) AS dialing,
                   SUM(CASE WHEN l.status='completed' THEN 1 ELSE 0 END) AS completed,
                   SUM(CASE WHEN l.status='failed' THEN 1 ELSE 0 END) AS failed
            FROM campaigns c
            LEFT JOIN leads l ON l.campaign_id = c.id
            GROUP BY c.id
            ORDER BY c.id DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_campaign(campaign_id: int) -> dict[str, Any] | None:
    init()
    conn = connect()
    try:
        r = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        camp = _row(r)
        if not camp:
            return None
        stats = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
                   SUM(CASE WHEN status='dialing' THEN 1 ELSE 0 END) AS dialing,
                   SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
                   SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed
            FROM leads WHERE campaign_id=?
            """,
            (campaign_id,),
        ).fetchone()
        camp.update(dict(stats) if stats else {})
        return camp
    finally:
        conn.close()


def set_campaign_status(campaign_id: int, status: str) -> dict[str, Any] | None:
    with _LOCK:
        conn = connect()
        try:
            conn.execute(
                "UPDATE campaigns SET status=?, updated_at=? WHERE id=?",
                (status, _now(), campaign_id),
            )
            conn.commit()
        finally:
            conn.close()
    return get_campaign(campaign_id)


def list_leads(campaign_id: int, limit: int = 500) -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM leads WHERE campaign_id=? ORDER BY id ASC LIMIT ?",
            (campaign_id, limit),
        ).fetchall()
        return [_row(r) or {} for r in rows]
    finally:
        conn.close()


def claim_next_lead() -> dict[str, Any] | None:
    """Atomically take one pending lead from a running campaign."""
    now = _now()
    with _LOCK:
        conn = connect()
        try:
            row = conn.execute(
                """
                SELECT l.* FROM leads l
                JOIN campaigns c ON c.id = l.campaign_id
                WHERE c.status='running' AND l.status='pending'
                ORDER BY l.id ASC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE leads SET status='dialing', attempts=attempts+1, updated_at=? WHERE id=?",
                (now, row["id"]),
            )
            conn.commit()
            claimed = conn.execute("SELECT * FROM leads WHERE id=?", (row["id"],)).fetchone()
            return _row(claimed)
        finally:
            conn.close()


def count_dialing() -> int:
    conn = connect()
    try:
        n = conn.execute("SELECT COUNT(*) FROM leads WHERE status='dialing'").fetchone()[0]
        return int(n or 0)
    finally:
        conn.close()


def running_campaigns() -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM campaigns WHERE status='running'").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mark_campaigns_done() -> None:
    """If a running campaign has no pending/dialing leads, mark it done."""
    with _LOCK:
        conn = connect()
        try:
            conn.execute(
                """
                UPDATE campaigns SET status='done', updated_at=?
                WHERE status='running' AND id NOT IN (
                    SELECT DISTINCT campaign_id FROM leads
                    WHERE status IN ('pending', 'dialing')
                )
                """,
                (_now(),),
            )
            conn.commit()
        finally:
            conn.close()


def update_lead(lead_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [lead_id]
    with _LOCK:
        conn = connect()
        try:
            conn.execute(f"UPDATE leads SET {cols} WHERE id=?", vals)
            conn.commit()
        finally:
            conn.close()


def update_lead_by_ccid(ccid: str, **fields: Any) -> None:
    if not ccid:
        return
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [ccid]
    with _LOCK:
        conn = connect()
        try:
            conn.execute(f"UPDATE leads SET {cols} WHERE call_control_id=?", vals)
            conn.commit()
        finally:
            conn.close()
