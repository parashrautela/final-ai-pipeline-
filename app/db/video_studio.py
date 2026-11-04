"""Owner-scoped storyboard persistence; mutations use atomic versioned RPCs."""
import asyncio

from app.db.repository import get_supabase
from app.db.videos import product, rpc


async def project(owner_id: str, project_id: str):
    rows = await asyncio.to_thread(lambda: get_supabase().table("video_studio_projects").select("*")
                                  .eq("owner_id",owner_id).eq("id",project_id).limit(1).execute().data)
    return rows[0] if rows else None


async def request(owner_id: str, key: str):
    rows = await asyncio.to_thread(lambda: get_supabase().table("video_studio_requests").select("*")
                                  .eq("owner_id",owner_id).eq("idempotency_key",key).limit(1).execute().data)
    return rows[0] if rows else None


async def history(owner_id: str, product_id: str):
    return await asyncio.to_thread(lambda: get_supabase().table("video_studio_projects").select("*")
                                  .eq("owner_id",owner_id).eq("product_id",product_id)
                                  .order("created_at",desc=True).limit(50).execute().data)
