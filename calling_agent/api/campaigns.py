"""Campaign / CSV lead APIs."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from calling_agent import config, db, dialer

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
    camp = db.create_campaign(
        name=(body.get("name") or "campaign").strip() or "campaign",
        phone_column=phone_column,
        rows=rows,
        filename=body.get("filename") or "",
    )
    return camp


@router.get("/campaigns/{campaign_id}")
async def get_campaign(campaign_id: int, request: Request):
    _auth(request)
    camp = db.get_campaign(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="campaign not found")
    camp["leads"] = db.list_leads(campaign_id)
    camp["max_concurrent"] = config.MAX_CONCURRENT_CALLS
    return camp


@router.post("/campaigns/{campaign_id}/start")
async def start_campaign(campaign_id: int, request: Request):
    _auth(request)
    if not db.get_campaign(campaign_id):
        raise HTTPException(status_code=404, detail="campaign not found")
    camp = db.set_campaign_status(campaign_id, "running")
    dialer.kick()
    return camp


@router.post("/campaigns/{campaign_id}/pause")
async def pause_campaign(campaign_id: int, request: Request):
    _auth(request)
    if not db.get_campaign(campaign_id):
        raise HTTPException(status_code=404, detail="campaign not found")
    return db.set_campaign_status(campaign_id, "paused")
