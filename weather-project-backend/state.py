from __future__ import annotations

import asyncio
from threading import Lock
from typing import Any


WEATHER_CACHE: dict[str, dict[str, Any]] = {}
PUSH_STORE_LOCK = Lock()
PUSH_LOOP_TASK: asyncio.Task | None = None
PUSH_STORE: dict[str, Any] = {
    "subscriptions": {},
    "alerts": [],
}
