"""Persistence for the video pilot. Every SDK operation runs off the event loop."""
import asyncio

from app.config import settings
from app.db.repository import get_supabase


async def rpc(name: str, params: dict):
    return await asyncio.to_thread(lambda: get_supabase().rpc(name, params).execute().data)


async def product(product_id: str, owner_id: str):
    rows = await asyncio.to_thread(lambda: get_supabase().table("products").select("*").eq("id", product_id).eq("wholesaler_id", owner_id).limit(1).execute().data)
    return rows[0] if rows else None


async def find(owner_id: str, *, job_id: str = None, key: str = None):
    def query():
        q = get_supabase().table("video_generations").select("*").eq("owner_id", owner_id)
        return q.eq("id", job_id).limit(1).execute().data if job_id else q.eq("idempotency_key", key).limit(1).execute().data
    rows = await asyncio.to_thread(query)
    return rows[0] if rows else None


async def history(owner_id: str, product_id: str):
    return await asyncio.to_thread(lambda: get_supabase().table("video_generations").select("*").eq("owner_id", owner_id).eq("product_id", product_id).order("created_at", desc=True).limit(50).execute().data)


async def budget():
    return await rpc("video_budget", {"p_key": settings.VIDEO_BUDGET_KEY, "p_limit": settings.VIDEO_BUDGET_UNITS})


async def update(job: dict, patch: dict, *, release: bool = True):
    result = await rpc("video_update", {"p_id": job["id"], "p_token": job["lease_token"], "p_patch": patch, "p_release": release})
    if not result:
        raise RuntimeError("Video job lease expired or belongs to another worker.")
    return result
