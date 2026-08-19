from __future__ import annotations

import asyncio
import hashlib
import hmac
from typing import Any

import httpx

from app.config import settings


def verify_webhook_signature(raw_payload: bytes, signature: str | None) -> bool:
    if signature is None:
        return False
    expected = "sha256=" + hmac.new(settings.META_APP_SECRET.encode(), raw_payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


async def send_whatsapp_message(to: str, text: str) -> dict[str, Any]:
    url = f"https://graph.facebook.com/v21.0/{settings.PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }

    for attempt in range(1, 5):
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError:
            if attempt == 4:
                raise
            await asyncio.sleep(2 ** attempt)

    raise RuntimeError("Failed to send WhatsApp message")
