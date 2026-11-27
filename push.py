"""Sending app push notifications through Expo's push service (which delivers via FCM on Android and APNs on iOS).
Failures never break the caller: order updates still go through if a notification can't be sent."""
import logging
import os
from typing import Iterable, Optional

import httpx

from core import db

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
BATCH = 100  # Expo accepts up to 100 messages per request


async def send_push(tokens: Iterable[str], title: str, body: str, data: Optional[dict] = None) -> dict:
    """Sends one notification to each token. Returns {"sent": n, "failed": n}. Tokens Expo reports as no longer
    registered (app uninstalled / notifications revoked) are removed."""
    tokens = list(dict.fromkeys(t for t in tokens if t))
    sent = failed = 0
    if not tokens:
        return {"sent": 0, "failed": 0}
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if os.environ.get("EXPO_ACCESS_TOKEN"):  # only needed if "enhanced push security" is on in the Expo project
        headers["Authorization"] = f"Bearer {os.environ['EXPO_ACCESS_TOKEN']}"
    dead = []
    async with httpx.AsyncClient(timeout=20) as http:
        for i in range(0, len(tokens), BATCH):
            chunk = tokens[i : i + BATCH]
            msgs = [{"to": t, "title": title, "body": body, "data": data or {}, "sound": "default", "channelId": "default"} for t in chunk]
            try:
                r = await http.post(EXPO_PUSH_URL, json=msgs, headers=headers)
                r.raise_for_status()
                tickets = r.json().get("data", [])
            except Exception as e:
                logger.warning("push batch failed: %s", e)
                failed += len(chunk)
                continue
            for tok, t in zip(chunk, tickets):
                if t.get("status") == "ok":
                    sent += 1
                else:
                    failed += 1
                    if (t.get("details") or {}).get("error") == "DeviceNotRegistered":
                        dead.append(tok)
    if dead:
        await db.push_tokens.delete_many({"token": {"$in": dead}})
    return {"sent": sent, "failed": failed}


async def notify_user(user_id: Optional[str], title: str, body: str, data: Optional[dict] = None) -> int:
    """Order updates etc. to every device of one customer. Returns how many devices it reached (0 on any failure)."""
    if not user_id:
        return 0
    try:
        tokens = [t["token"] async for t in db.push_tokens.find({"user_id": user_id}, {"token": 1})]
        return (await send_push(tokens, title, body, data))["sent"]
    except Exception as e:  # a notification must never break the order flow
        logger.warning("notify_user failed: %s", e)
        return 0
