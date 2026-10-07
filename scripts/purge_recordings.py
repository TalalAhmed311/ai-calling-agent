#!/usr/bin/env python3
"""Delete call recordings older than RETENTION_HOURS.

Constraint: if a campaign has any "kept" lead (real sales outcome /
compliance), do not delete any recording that belongs to that campaign.

Also skips individual leads whose own disposition is in KEEP_DISPOSITIONS.

Clears the leads.recording path after a successful delete.
Orphan mp3s under data/recordings/ with no DB row are deleted by age alone.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
LEADS_DB = APP_DIR / "data" / "leads.db"
RECORDINGS_DIR = APP_DIR / "data" / "recordings"
LOG_DIR = APP_DIR / "logs"
DEFAULT_HOURS = 30

# Keep audio when the call produced a real lead / compliance outcome.
KEEP_DISPOSITIONS = frozenset({
    "callback_requested",
    "owner_manager_interested",
    "do_not_call",
})

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("purge_recordings")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(LEADS_DB)
    conn.row_factory = sqlite3.Row
    return conn


def _campaigns_with_kept_leads(conn: sqlite3.Connection) -> set[int]:
    placeholders = ",".join("?" for _ in KEEP_DISPOSITIONS)
    rows = conn.execute(
        f"""
        SELECT DISTINCT campaign_id
        FROM leads
        WHERE disposition IN ({placeholders})
        """,
        tuple(KEEP_DISPOSITIONS),
    ).fetchall()
    return {int(r["campaign_id"]) for r in rows}


def _age_hours(path: Path, now: datetime) -> float:
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return (now - mtime).total_seconds() / 3600.0


def purge(*, hours: float = DEFAULT_HOURS, dry_run: bool = False) -> dict:
    now = datetime.now(timezone.utc)
    cutoff_h = float(hours)
    stats = {
        "scanned": 0,
        "deleted": 0,
        "kept_young": 0,
        "kept_campaign_lead": 0,
        "kept_lead_disposition": 0,
        "missing_file": 0,
        "errors": 0,
        "dry_run": dry_run,
        "hours": cutoff_h,
    }

    if not LEADS_DB.exists():
        log.error("leads db missing: %s", LEADS_DB)
        stats["errors"] += 1
        return stats

    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    conn = _connect()
    try:
        protected_campaigns = _campaigns_with_kept_leads(conn)
        if protected_campaigns:
            log.info(
                "campaigns with kept leads (skip all their audio): %s",
                sorted(protected_campaigns),
            )

        leads = conn.execute(
            """
            SELECT id, campaign_id, disposition, recording, call_control_id, phone, name
            FROM leads
            WHERE recording IS NOT NULL AND recording != ''
            """
        ).fetchall()

        seen_paths: set[Path] = set()

        for row in leads:
            stats["scanned"] += 1
            path = Path(row["recording"])
            seen_paths.add(path.resolve() if path.exists() else path)

            if not path.exists():
                stats["missing_file"] += 1
                if not dry_run:
                    conn.execute(
                        "UPDATE leads SET recording=NULL, updated_at=? WHERE id=?",
                        (now.isoformat(), row["id"]),
                    )
                continue

            age = _age_hours(path, now)
            if age < cutoff_h:
                stats["kept_young"] += 1
                continue

            campaign_id = int(row["campaign_id"])
            disposition = (row["disposition"] or "").strip()

            if campaign_id in protected_campaigns:
                stats["kept_campaign_lead"] += 1
                log.info(
                    "keep campaign#%s lead#%s %s — campaign has kept lead(s)",
                    campaign_id,
                    row["id"],
                    path.name,
                )
                continue

            if disposition in KEEP_DISPOSITIONS:
                stats["kept_lead_disposition"] += 1
                log.info(
                    "keep lead#%s disposition=%s %s",
                    row["id"],
                    disposition,
                    path.name,
                )
                continue

            log.info(
                "%s %s age=%.1fh lead#%s campaign#%s disp=%r",
                "would delete" if dry_run else "delete",
                path.name,
                age,
                row["id"],
                campaign_id,
                disposition or None,
            )
            if dry_run:
                stats["deleted"] += 1
                continue
            try:
                path.unlink()
                conn.execute(
                    "UPDATE leads SET recording=NULL, updated_at=? WHERE id=?",
                    (now.isoformat(), row["id"]),
                )
                stats["deleted"] += 1
            except OSError as exc:
                stats["errors"] += 1
                log.error("failed to delete %s: %s", path, exc)

        # Orphan mp3s not referenced by any lead row.
        for path in RECORDINGS_DIR.glob("*.mp3"):
            resolved = path.resolve()
            if resolved in seen_paths or path in seen_paths:
                continue
            stats["scanned"] += 1
            age = _age_hours(path, now)
            if age < cutoff_h:
                stats["kept_young"] += 1
                continue
            log.info(
                "%s orphan %s age=%.1fh",
                "would delete" if dry_run else "delete",
                path.name,
                age,
            )
            if dry_run:
                stats["deleted"] += 1
                continue
            try:
                path.unlink()
                stats["deleted"] += 1
            except OSError as exc:
                stats["errors"] += 1
                log.error("failed to delete orphan %s: %s", path, exc)

        if not dry_run:
            conn.commit()
    finally:
        conn.close()

    log.info("purge done %s", stats)
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hours",
        type=float,
        default=DEFAULT_HOURS,
        help=f"delete recordings older than this many hours (default {DEFAULT_HOURS})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="log what would be deleted without removing files",
    )
    args = parser.parse_args(argv)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(LOG_DIR / "purge_recordings.log")
    fh.setFormatter(logging.Formatter("%(asctime)sZ %(levelname)s %(message)s"))
    logging.getLogger().addHandler(fh)

    stats = purge(hours=args.hours, dry_run=args.dry_run)
    return 1 if stats["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
