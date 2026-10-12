"""Keyword → scrape → timezone-enrich → run campaign."""

from __future__ import annotations

import asyncio
from typing import Any

from calling_agent import config, db, dialer, scrape_client
from calling_agent.log import fail, log
from calling_agent.timezone_us import resolve_timezone

_jobs: dict[int, asyncio.Task] = {}


def enrich_scrape_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for row in rows:
        address = str(row.get("address") or "")
        phone = str(row.get("phone") or "")
        tz = resolve_timezone(address=address, phone=phone)
        fields = dict(row.get("fields") or {})
        fields["timezone"] = tz
        fields["address"] = address
        enriched.append(
            {
                "phone": phone,
                "name": row.get("name") or "",
                "address": address or None,
                "timezone": tz,
                "fields": fields,
            }
        )
    return enriched


async def run_keyword_campaign(campaign_id: int, *, limit: int = 50) -> None:
    camp = db.get_campaign(campaign_id)
    if not camp:
        fail("keyword_campaign_missing", f"id={campaign_id}")
        return
    query = (camp.get("query") or camp.get("filename") or "").strip()
    if not query:
        _set_campaign_error(campaign_id, "missing query")
        db.set_campaign_status(campaign_id, "failed")
        return

    try:
        db.set_campaign_status(campaign_id, "scraping")
        _set_scrape_status(campaign_id, "starting")
        job_id = await scrape_client.start_scrape(
            query,
            limit=limit,
            version=config.SCRAPER_VERSION,
        )
        _set_scrape_job(campaign_id, job_id, "running")

        result = await scrape_client.wait_for_scrape(job_id)
        raw_rows = scrape_client.rows_from_scrape_result(result)
        rows = enrich_scrape_rows(raw_rows)
        stats = db.add_leads_to_campaign(campaign_id, rows)
        log(
            f"[keyword] campaign={campaign_id} scrape done "
            f"rows={len(raw_rows)} inserted={stats['inserted']} "
            f"skipped={stats['skipped']}"
        )
        _set_scrape_status(campaign_id, "completed")

        if stats["inserted"] <= 0:
            _set_campaign_error(campaign_id, "scrape returned no dialable phones")
            db.set_campaign_status(campaign_id, "done")
            return

        db.set_campaign_status(campaign_id, "running")
        dialer.kick()
        log(f"[keyword] campaign={campaign_id} now running (timezone windows active)")
    except Exception as exc:
        fail("keyword_campaign_failed", str(exc), campaign_id=campaign_id)
        _set_campaign_error(campaign_id, str(exc)[:500])
        db.set_campaign_status(campaign_id, "failed")


def start_keyword_campaign_background(
    campaign_id: int,
    *,
    limit: int = 50,
) -> None:
    existing = _jobs.get(campaign_id)
    if existing and not existing.done():
        return
    _jobs[campaign_id] = asyncio.create_task(
        run_keyword_campaign(campaign_id, limit=limit)
    )


def _set_scrape_job(campaign_id: int, job_id: str, scrape_status: str) -> None:
    with db._LOCK:
        conn = db.connect()
        try:
            conn.execute(
                "UPDATE campaigns SET scrape_job_id=?, scrape_status=?, updated_at=? WHERE id=?",
                (job_id, scrape_status, db._now(), campaign_id),
            )
            conn.commit()
        finally:
            conn.close()


def _set_scrape_status(campaign_id: int, scrape_status: str) -> None:
    with db._LOCK:
        conn = db.connect()
        try:
            conn.execute(
                "UPDATE campaigns SET scrape_status=?, updated_at=? WHERE id=?",
                (scrape_status, db._now(), campaign_id),
            )
            conn.commit()
        finally:
            conn.close()


def _set_campaign_error(campaign_id: int, error: str) -> None:
    with db._LOCK:
        conn = db.connect()
        try:
            conn.execute(
                "UPDATE campaigns SET error=?, scrape_status=?, updated_at=? WHERE id=?",
                (error, "failed", db._now(), campaign_id),
            )
            conn.commit()
        finally:
            conn.close()
