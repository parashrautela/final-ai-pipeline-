"""The one-time onboarding fee.

A wholesaler pays it at the end of their application, before it is sent.
The amount lives in Railway (`ONBOARDING_FEE_INR`) so it can change without
an app release; 0 switches the fee off.

Payment is a Razorpay payment link, confirmed by asking Razorpay directly
rather than waiting for a webhook: the app opens the link, and when the page
closes it calls /confirm, which reads the link's status from Razorpay and
records it in `onboarding_payments`.

The live razorpay-webhook sees these payments too (it receives every paid
link). The link's notes carry `purpose=onboarding_fee` and no
`wholesaler_id`, so it cannot credit anyone for them; until it is taught to
skip that purpose it files each one under credit_purchase_issues.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.auth import resolve_user_id
from app.config import settings
from app.db.repository import get_supabase
from app.logging import logger

router = APIRouter(prefix="/api/onboarding/fee")

_RAZORPAY = "https://api.razorpay.com/v1/payment_links"
_LINK_LIFETIME_SECONDS = 30 * 60
PURPOSE = "onboarding_fee"


def _configured() -> bool:
    return bool(settings.RAZORPAY_KEY_ID.strip() and settings.RAZORPAY_KEY_SECRET.strip())


def fee_inr() -> int:
    return max(0, int(settings.ONBOARDING_FEE_INR))


def _auth() -> tuple[str, str]:
    return (settings.RAZORPAY_KEY_ID.strip(), settings.RAZORPAY_KEY_SECRET.strip())


async def _paid_row(user_id: str) -> Optional[dict]:
    rows = (
        get_supabase().table("onboarding_payments")
        .select("id, link_id, amount_paise, status, paid_at")
        .eq("user_id", user_id).eq("status", "paid")
        .limit(1).execute().data
    )
    return rows[0] if rows else None


@router.get("")
async def fee_status(authorization: Optional[str] = Header(default=None)):
    """What the fee is, and whether the caller has paid it."""
    amount = fee_inr()
    required = amount > 0
    paid = False
    if authorization:
        user_id = await resolve_user_id(authorization)
        if user_id and required:
            paid = await _paid_row(user_id) is not None
    return {
        "amount_inr": amount,
        "required": required,
        # A fee that is set but can't be collected yet must not block anyone.
        "payable": required and _configured(),
        "paid": paid,
    }


@router.post("/pay")
async def create_fee_link(authorization: Optional[str] = Header(default=None)):
    """A Razorpay payment page for the fee, made out to the signed-in user."""
    user_id = await resolve_user_id(authorization)
    if not user_id:
        raise HTTPException(status_code=401, detail="Sign in to pay the onboarding fee.")

    amount = fee_inr()
    if amount == 0:
        return {"required": False}
    if await _paid_row(user_id):
        return {"required": True, "paid": True}
    if not _configured():
        logger.error("Onboarding fee is set but RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not")
        raise HTTPException(status_code=503, detail="Payments aren't available right now. Please try again later.")

    body = {
        "amount": amount * 100,
        "currency": "INR",
        "accept_partial": False,
        "description": "Jewel India: one-time onboarding fee",
        "notify": {"sms": False, "email": False},
        "reminder_enable": False,
        "expire_by": int(time.time()) + _LINK_LIFETIME_SECONDS,
        # Deliberately no wholesaler_id: the credits webhook must never be
        # able to read this as a credit purchase.
        "notes": {"purpose": PURPOSE, "user_id": user_id, "source": "app"},
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            res = await client.post(_RAZORPAY, json=body, auth=_auth())
    except httpx.HTTPError as exc:
        logger.error(f"Razorpay unreachable for onboarding fee: {exc}")
        raise HTTPException(status_code=502, detail="Could not start the payment. Please try again.") from exc
    if res.status_code >= 300:
        logger.error(f"Razorpay refused the onboarding link: {res.status_code} {res.text[:300]}")
        raise HTTPException(status_code=502, detail="Could not start the payment. Please try again.")

    link = res.json()
    get_supabase().table("onboarding_payments").insert({
        "user_id": user_id,
        "link_id": link["id"],
        "amount_paise": amount * 100,
        "status": "created",
    }).execute()
    return {"required": True, "paid": False, "link_id": link["id"], "url": link["short_url"], "amount_inr": amount}


class ConfirmBody(BaseModel):
    link_id: str


@router.post("/confirm")
async def confirm_fee(body: ConfirmBody, authorization: Optional[str] = Header(default=None)):
    """Ask Razorpay whether this link was paid, and record it if so."""
    user_id = await resolve_user_id(authorization)
    if not user_id:
        raise HTTPException(status_code=401, detail="Sign in to confirm the payment.")

    rows = (
        get_supabase().table("onboarding_payments")
        .select("id, user_id, amount_paise, status")
        .eq("link_id", body.link_id).limit(1).execute().data
    )
    # Someone else's link, or one we never made: the same answer as unpaid.
    if not rows or rows[0]["user_id"] != user_id:
        return {"paid": False}
    row = rows[0]
    if row["status"] == "paid":
        return {"paid": True}
    if not _configured():
        raise HTTPException(status_code=503, detail="Payments aren't available right now.")

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            res = await client.get(f"{_RAZORPAY}/{body.link_id}", auth=_auth())
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Could not check the payment. Please try again.") from exc
    if res.status_code >= 300:
        raise HTTPException(status_code=502, detail="Could not check the payment. Please try again.")

    link = res.json()
    paid = (
        link.get("status") == "paid"
        and int(link.get("amount_paid") or 0) >= int(row["amount_paise"])
        and (link.get("notes") or {}).get("user_id") == user_id
    )
    if not paid:
        return {"paid": False, "status": link.get("status")}

    payment_id = next(
        (p.get("payment_id") for p in (link.get("payments") or []) if p.get("status") == "captured"),
        None,
    )
    get_supabase().table("onboarding_payments").update({
        "status": "paid",
        "payment_id": payment_id,
        "paid_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", row["id"]).execute()
    logger.info("Onboarding fee paid", extra={"user_id": user_id, "link_id": body.link_id})
    return {"paid": True}
