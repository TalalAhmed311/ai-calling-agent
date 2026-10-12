"""Campaign / CSV lead APIs."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from calling_agent import config, db, dialer, keyword_campaign

router = APIRouter()


def _auth(request: Request) -> None:
    if request.headers.get("x-endpoint-secret") != config.ENDPOINT_SECRET:
        raise HTTPException(status_code=401, detail="bad secret")


@router.get("/campaigns")
async def list_campaigns(request: Request):
    _auth(request)
    return {
        "campaigns": db.list_campaigns(),
        "max_concurrent": config.MAX_CONCURRENT_CALLS,
        "dialing": db.count_dialing(),
        "call_window_start": config.CALL_WINDOW_START,
        "call_window_end": config.CALL_WINDOW_END,
    }


@router.post("/campaigns")
async def create_campaign(request: Request):
    _auth(request)
    body = await request.json()
    phone_column = (body.get("phone_column") or "").strip()
    rows = body.get("leads") or []
    if not phone_column:
        raise HTTPException(status_code=400, detail="missing phone_column")
    if not rows:
        raise HTTPException(status_code=400, detail="no leads")
    # Enrich CSV rows with timezone when address is present in fields.
    from calling_agent.timezone_us import resolve_timezone

    enriched = []
    for row in rows:
        fields = dict(row.get("fields") or {})
        address = str(
            row.get("address")
            or fields.get("address")
            or fields.get("Address")
            or ""
        )
        phone = str(row.get("phone") or "")
        tz = row.get("timezone") or resolve_timezone(address=address, phone=phone)
        if address:
            fields.setdefault("address", address)
        if tz:
            fields["timezone"] = tz
        enriched.append(
            {
                "phone": phone,
                "name": row.get("name") or "",
                "address": address or None,
                "timezone": tz,
                "fields": fields,
            }
        )
    camp = db.create_campaign(
        name=(body.get("name") or "campaign").strip() or "campaign",
        phone_column=phone_column,
        rows=enriched,
        filename=body.get("filename") or "",
        source="csv",
        call_window_start=config.CALL_WINDOW_START,
        call_window_end=config.CALL_WINDOW_END,
    )
    return camp


@router.get("/campaigns/{campaign_id}")
async def get_campaign(campaign_id: int, request: Request):
    _auth(request)
    camp = db.get_campaign(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="campaign not found")
    camp["leads"] = db.list_leads(campaign_id)
    camp["timezone_buckets"] = db.timezone_bucket_stats(campaign_id)
    camp["max_concurrent"] = config.MAX_CONCURRENT_CALLS
    return camp


@router.post("/campaigns/from-keyword")
async def create_from_keyword(request: Request):
    """Start a keyword scrape then dial leads only in each lead's local window."""
    _auth(request)
    body = await request.json()
    query = (body.get("query") or body.get("keyword") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="missing query")
    try:
        limit = int(body.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 200))
    window_start = (
        (body.get("call_window_start") or config.CALL_WINDOW_START).strip() or "10:00"
    )
    window_end = (
        (body.get("call_window_end") or config.CALL_WINDOW_END).strip() or "14:00"
    )
    name = (body.get("name") or query).strip() or query

    camp = db.create_empty_keyword_campaign(
        name=name,
        query=query,
        call_window_start=window_start,
        call_window_end=window_end,
    )
    keyword_campaign.start_keyword_campaign_background(int(camp["id"]), limit=limit)
    camp = db.get_campaign(int(camp["id"])) or camp
    camp["message"] = (
        f"Scraping “{query}” then dialing each lead only during "
        f"{window_start}–{window_end} in that business's local timezone."
    )
    return camp


@router.post("/campaigns/{campaign_id}/start")
async def start_campaign(campaign_id: int, request: Request):
    _auth(request)
    camp = db.get_campaign(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="campaign not found")
    if camp.get("status") == "scraping":
        raise HTTPException(status_code=409, detail="campaign still scraping")
    camp = db.set_campaign_status(campaign_id, "running")
    dialer.kick()
    return camp


@router.post("/campaigns/{campaign_id}/pause")
async def pause_campaign(campaign_id: int, request: Request):
    _auth(request)
    if not db.get_campaign(campaign_id):
        raise HTTPException(status_code=404, detail="campaign not found")
    return db.set_campaign_status(campaign_id, "paused")
