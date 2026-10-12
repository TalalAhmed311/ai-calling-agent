"""HTTP client for the app1 Google Maps scraper (localhost)."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from calling_agent import config
from calling_agent.log import fail, log


async def start_scrape(
    keyword: str,
    *,
    limit: int = 50,
    version: str = "v2",
    headed: bool = False,
) -> str:
    url = f"{config.SCRAPER_URL.rstrip('/')}/scrape/start"
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            url,
            json={
                "keyword": keyword,
                "limit": limit,
                "version": version,
                "headed": headed,
            },
        )
        if r.is_error:
            raise RuntimeError(f"scrape start failed {r.status_code}: {r.text[:400]}")
        data = r.json()
    job_id = str(data.get("job_id") or "")
    if not job_id:
        raise RuntimeError(f"scrape start missing job_id: {data}")
    log(f"[scrape] started job={job_id} keyword={keyword!r} limit={limit}")
    return job_id


async def scrape_status(job_id: str) -> dict[str, Any]:
    url = f"{config.SCRAPER_URL.rstrip('/')}/scrape/status/{job_id}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(url)
        if r.is_error:
            raise RuntimeError(f"scrape status failed {r.status_code}: {r.text[:300]}")
        return r.json()


async def scrape_result(job_id: str) -> dict[str, Any]:
    url = f"{config.SCRAPER_URL.rstrip('/')}/scrape/result/{job_id}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url)
        if r.is_error:
            raise RuntimeError(f"scrape result failed {r.status_code}: {r.text[:400]}")
        return r.json()


async def wait_for_scrape(
    job_id: str,
    *,
    poll_s: float = 3.0,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """Poll until completed/stopped/failed; return result payload."""
    timeout_s = timeout_s if timeout_s is not None else float(config.SCRAPE_TIMEOUT_S)
    deadline = asyncio.get_event_loop().time() + timeout_s
    last_status = ""
    while True:
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError(f"scrape job {job_id} timed out after {timeout_s}s")
        st = await scrape_status(job_id)
        status = str(st.get("status") or "")
        if status != last_status:
            log(
                f"[scrape] job={job_id} status={status} "
                f"msg={st.get('message')!r} kept={st.get('kept_count')}"
            )
            last_status = status
        if status == "failed":
            raise RuntimeError(st.get("message") or f"scrape failed job={job_id}")
        if status in ("completed", "stopped"):
            return await scrape_result(job_id)
        await asyncio.sleep(poll_s)


def rows_from_scrape_result(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize scraper v1/v2 row shapes into phone/name/address fields."""
    data = result.get("data") or []
    out: list[dict[str, Any]] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        phone = (
            raw.get("phone_number")
            or raw.get("Phone")
            or raw.get("phone")
            or raw.get("phone_display")
            or ""
        )
        name = raw.get("name") or raw.get("Name") or ""
        address = raw.get("address") or raw.get("Address") or ""
        out.append(
            {
                "phone": str(phone),
                "name": str(name),
                "address": str(address),
                "fields": {
                    "name": str(name),
                    "address": str(address),
                    "website": str(raw.get("website") or raw.get("Website") or ""),
                    "category": str(raw.get("category") or ""),
                    "maps_url": str(raw.get("maps_url") or ""),
                    "source_query": str(
                        raw.get("source_query") or result.get("keyword") or ""
                    ),
                    "raw": {k: raw[k] for k in raw if k != "raw"},
                },
            }
        )
    return out
