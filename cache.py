"""Redis helpers: JSON cache and fixed-window rate limiter. Everything fails open if Redis is unavailable."""
import json
import logging
import os
from typing import Any, Optional

import redis.asyncio as aredis

logger = logging.getLogger(__name__)

REDIS_URL = (os.environ.get("REDIS_URL") or "").strip()
_opts = dict(socket_connect_timeout=1, socket_timeout=1, health_check_interval=30)

aio: Optional[aredis.Redis] = aredis.from_url(REDIS_URL, decode_responses=True, **_opts) if REDIS_URL else None

PREFIX = "df:"


async def ping() -> bool:
    if not aio:
        return False
    try:
        return bool(await aio.ping())
    except Exception:
        return False


async def cache_get(key: str) -> Optional[Any]:
    if not aio:
        return None
    try:
        raw = await aio.get(PREFIX + key)
        return json.loads(raw) if raw else None
    except Exception as e:
        logger.warning("cache get failed: %s", e)
        return None


async def cache_set(key: str, value: Any, ttl: int = 60) -> None:
    if not aio:
        return
    try:
        await aio.set(PREFIX + key, json.dumps(value), ex=ttl)
    except Exception as e:
        logger.warning("cache set failed: %s", e)


async def cache_delete(*keys: str) -> None:
    if not aio:
        return
    try:
        await aio.delete(*(PREFIX + k for k in keys))
    except Exception as e:
        logger.warning("cache delete failed: %s", e)


async def hit(key: str, window: int) -> Optional[tuple]:
    """Count one hit in a fixed window. Returns (count, seconds_left), or None if Redis is down."""
    if not aio:
        return None
    k = f"{PREFIX}rl:{key}"
    try:
        pipe = aio.pipeline()
        pipe.incr(k)
        pipe.expire(k, window, nx=True)
        pipe.ttl(k)
        count, _, ttl = await pipe.execute()
        return int(count), max(int(ttl), 1)
    except Exception as e:
        logger.warning("rate limit unavailable: %s", e)
        return None
