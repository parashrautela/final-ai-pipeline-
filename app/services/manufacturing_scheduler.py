"""Durable reconciliation scheduler and outbox dispatcher for manufacturing requests.

Uses PostgreSQL row locks with SKIP LOCKED to prevent duplicate processing across
concurrent worker replicas, and dispatches push notifications via HTTP/2 APNs.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os
import time
from typing import Any, Optional
import uuid

import httpx
import jwt
from cryptography.hazmat.primitives import serialization

from app.config import settings
from app.db.repository import get_supabase
from app.logging import logger


class APNsService:
    """Apple Push Notification service dispatcher using HTTP/2 and token-based (p8) auth."""

    def __init__(self):
        self._cached_jwt: Optional[str] = None
        self._jwt_created_at: float = 0.0

    @property
    def is_configured(self) -> bool:
        """Check if all required APNs credentials are set."""
        return bool(
            settings.APNS_KEY_ID
            and settings.APNS_TEAM_ID
            and settings.APNS_PRIVATE_KEY
        )

    def _load_private_key(self) -> Any:
        """Load private key from file path, PEM string, or base64."""
        key_raw = settings.APNS_PRIVATE_KEY.strip()
        if not key_raw:
            raise ValueError("APNS_PRIVATE_KEY is empty.")

        # Check if it's a file path
        if os.path.exists(key_raw):
            with open(key_raw, "rb") as f:
                key_bytes = f.read()
        else:
            # If plain PEM string
            if "BEGIN PRIVATE KEY" in key_raw or "BEGIN EC PRIVATE KEY" in key_raw:
                key_bytes = key_raw.encode("utf-8")
            else:
                # May be base64 encoded
                import base64
                try:
                    key_bytes = base64.b64decode(key_raw)
                except Exception:
                    key_bytes = key_raw.encode("utf-8")

        return serialization.load_pem_private_key(key_bytes, password=None)

    def get_bearer_token(self) -> str:
        """Generate or retrieve a cached ES256 JWT for APNs."""
        now = time.time()
        # APNs tokens are valid for up to 60 minutes; refresh after 50 minutes (3000s)
        if self._cached_jwt and (now - self._jwt_created_at) < 3000:
            return self._cached_jwt

        private_key = self._load_private_key()
        headers = {
            "alg": "ES256",
            "kid": settings.APNS_KEY_ID,
        }
        payload = {
            "iss": settings.APNS_TEAM_ID,
            "iat": int(now),
        }

        token = jwt.encode(payload, private_key, algorithm="ES256", headers=headers)
        self._cached_jwt = token
        self._jwt_created_at = now
        return token

    def send_push(
        self,
        device_token: str,
        title: str,
        body: str,
        custom_payload: Optional[dict] = None,
        environment: str = "production",
    ) -> tuple[bool, int, str]:
        """Send a single push notification via APNs HTTP/2.

        Returns (success: bool, status_code: int, error_detail: str).
        """
        if not self.is_configured:
            return False, 0, "APNS_CONFIG_MISSING"

        host = (
            "api.sandbox.push.apple.com"
            if environment in ("development", "sandbox")
            else "api.push.apple.com"
        )
        url = f"https://{host}/3/device/{device_token}"

        bearer_token = self.get_bearer_token()
        headers = {
            "authorization": f"bearer {bearer_token}",
            "apns-topic": settings.APNS_BUNDLE_ID,
            "apns-push-type": "alert",
            "apns-priority": "10",
            "apns-expiration": "0",
        }

        apns_payload = {
            "aps": {
                "alert": {
                    "title": title,
                    "body": body,
                },
                "sound": "default",
                "badge": 1,
            },
            **(custom_payload or {}),
        }

        try:
            with httpx.Client(http2=True, timeout=10.0) as client:
                resp = client.post(url, headers=headers, json=apns_payload)
                if resp.status_code == 200:
                    return True, 200, ""
                else:
                    return False, resp.status_code, resp.text
        except Exception as exc:
            return False, 0, str(exc)


apns_service = APNsService()


def format_notification(kind: str, payload: dict) -> tuple[str, str]:
    """Return appropriate user-facing title and body for notification kinds."""
    if kind == "MANUFACTURING_ENQUIRY_CANCELLED":
        return ("Enquiry Cancelled", "The retailer cancelled this enquiry. No further response is needed.")
    if kind == "MANUFACTURING_QUOTE_RECEIVED":
        return ("New Supplier Quote", "A wholesaler submitted a quote. Compare it with your other quotes.")
    if kind == "MANUFACTURING_QUOTE_AWARDED":
        return ("Your Quote Was Selected", "The retailer selected your quote. Open the enquiry to review the agreed terms.")
    if kind == "MANUFACTURING_ENQUIRY_CLOSED":
        return ("Enquiry Closed", "The retailer has selected a supplier for this enquiry.")
    if kind == "MANUFACTURING_QUOTES_READY":
        return ("Quotation Window Closed", "Open your enquiry to review the supplier responses.")
    if kind == "NEW_MANUFACTURING_OFFER":
        return (
            "New Custom Jewellery Request",
            "A retailer is seeking quotes for custom jewellery. Tap to review the requirements."
        )
    elif kind == "MANUFACTURING_REQUEST_ASSIGNED":
        return (
            "Order Confirmed!",
            "Your manufacturing request has been accepted by a verified wholesaler."
        )
    elif kind == "MANUFACTURING_REQUEST_EXHAUSTED":
        return (
            "Broadcast Queue Notice",
            "All candidate wholesalers have reviewed your request or the response window closed."
        )
    elif kind == "MANUFACTURING_OFFER_EXPIRED":
        return (
            "Offer Window Expired",
            "An offer was not accepted in time and has moved to the next candidate."
        )
    return ("Jewel India Notification", "You have an update regarding your manufacturing request.")


class ManufacturingScheduler:
    """Reconciliation worker for offer timeouts and atomic notification outbox dispatch."""

    def __init__(self, sweep_interval_seconds: float = 6.0):
        self.sweep_interval_seconds = sweep_interval_seconds
        self.worker_id = f"worker_{uuid.uuid4().hex[:8]}"
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self.last_sweep_at: Optional[datetime] = None
        self.last_sweep_error: Optional[str] = None
        self.swept_count: int = 0
        self.advanced_count: int = 0

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Manufacturing queue reconciliation worker started.", extra={"worker_id": self.worker_id})

    async def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Manufacturing queue reconciliation worker stopped.", extra={"worker_id": self.worker_id})

    async def _loop(self):
        while self._running:
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.last_sweep_error = str(exc)
                logger.error("Error in manufacturing queue sweep", exc_info=exc)

            try:
                await asyncio.sleep(self.sweep_interval_seconds)
            except asyncio.CancelledError:
                break

    async def sweep_once(self) -> dict:
        """Find overdue active offers with SKIP LOCKED, advance them, and dispatch pending outbox."""
        now = datetime.now(timezone.utc)
        self.last_sweep_at = now
        advanced = 0

        def _reconcile():
            sb = get_supabase()
            adv_count = 0

            # Close parallel quotation windows without assigning a supplier.
            sb.rpc("manufacturing_close_quotation_windows", {"p_batch_size": 25}).execute()

            # 1. Sweep expired offers atomically via SKIP LOCKED database procedure
            try:
                sweep_res = sb.rpc(
                    "manufacturing_sweep_expired_offers",
                    {"p_batch_size": 25, "p_offer_duration_seconds": 1800}
                ).execute().data
                if sweep_res:
                    adv_count = sweep_res.get("advanced_count", 0)
                    if adv_count > 0:
                        logger.info("Advanced expired manufacturing offers", extra={"sweep_result": sweep_res})
            except Exception as e:
                logger.debug(f"Could not execute manufacturing_sweep_expired_offers: {e}")

            # 2. Claim pending outbox items atomically with lease and SKIP LOCKED
            try:
                claimed_batch = sb.rpc(
                    "manufacturing_claim_outbox_batch",
                    {
                        "p_worker_id": self.worker_id,
                        "p_batch_size": 20,
                        "p_lease_seconds": 60,
                    }
                ).execute().data or []

                for item in claimed_batch:
                    outbox_id = item["id"]
                    recipient_user_id = item["recipient_user_id"]
                    kind = item["kind"]
                    payload = item.get("payload") or {}
                    attempts = item.get("attempts", 1)

                    # Check APNs configuration
                    if not apns_service.is_configured:
                        logger.warning(
                            "APNs is not configured; recording outbox as pending_config",
                            extra={"outbox_id": outbox_id, "kind": kind}
                        )
                        sb.table("manufacturing_notification_outbox").update({
                            "status": "pending_config",
                            "last_error": "APNS_CONFIG_MISSING: APNS_KEY_ID, APNS_TEAM_ID, or APNS_PRIVATE_KEY is unconfigured.",
                        }).eq("id", outbox_id).execute()
                        continue

                    # Look up active device tokens for the recipient user
                    token_rows = (
                        sb.table("manufacturing_device_tokens")
                        .select("id, device_token, environment")
                        .eq("user_id", recipient_user_id)
                        .execute()
                        .data or []
                    )

                    if not token_rows:
                        # No tokens registered for this user — mark delivered with explanation
                        logger.info(
                            "No device tokens found for recipient; marked delivered",
                            extra={"outbox_id": outbox_id, "recipient": recipient_user_id}
                        )
                        sb.table("manufacturing_notification_outbox").update({
                            "status": "delivered",
                            "delivered_at": datetime.now(timezone.utc).isoformat(),
                            "last_error": "NO_DEVICE_TOKENS_REGISTERED",
                        }).eq("id", outbox_id).execute()
                        continue

                    title, body = format_notification(kind, payload)
                    any_delivered = False
                    last_send_error = ""

                    for d_tok in token_rows:
                        device_token = d_tok["device_token"]
                        env = d_tok.get("environment") or "production"

                        ok, status_code, err_msg = apns_service.send_push(
                            device_token=device_token,
                            title=title,
                            body=body,
                            custom_payload=payload,
                            environment=env,
                        )

                        if ok:
                            any_delivered = True
                        elif status_code == 410:
                            # 410 Unregistered: token is dead, clean up immediately
                            logger.info(
                                "Cleaning up unregistered APNs device token (410)",
                                extra={"token_id": d_tok["id"], "user_id": recipient_user_id}
                            )
                            sb.table("manufacturing_device_tokens").delete().eq("id", d_tok["id"]).execute()
                            last_send_error = "Device token unregistered (410)"
                        else:
                            last_send_error = f"APNs HTTP {status_code}: {err_msg}"

                    if any_delivered:
                        sb.table("manufacturing_notification_outbox").update({
                            "status": "delivered",
                            "delivered_at": datetime.now(timezone.utc).isoformat(),
                            "last_error": None,
                        }).eq("id", outbox_id).execute()
                    else:
                        # Not delivered to any token
                        new_status = "failed" if attempts >= 5 else "pending"
                        sb.table("manufacturing_notification_outbox").update({
                            "status": new_status,
                            "last_error": last_send_error[:500],
                        }).eq("id", outbox_id).execute()

            except Exception as e:
                logger.debug(f"Could not process outbox batch: {e}")

            return adv_count

        adv = await asyncio.to_thread(_reconcile)
        self.swept_count += 1
        self.advanced_count += adv
        return {"swept_at": now.isoformat(), "advanced": adv}


scheduler = ManufacturingScheduler()
