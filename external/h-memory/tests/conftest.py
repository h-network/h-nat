"""Test configuration and fixtures for h-memory tests."""
import os
import sys
import uuid
from pathlib import Path
from typing import AsyncGenerator

import pytest
import redis.asyncio as aioredis

# Ensure module source is on sys.path
_MODULE_ROOT = Path(__file__).resolve().parent.parent / "src"
if str(_MODULE_ROOT) not in sys.path:
    sys.path.insert(0, str(_MODULE_ROOT))

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")


@pytest.fixture
async def redis_client() -> AsyncGenerator[aioredis.Redis, None]:
    client = aioredis.Redis.from_url(REDIS_URL, decode_responses=True)
    await client.ping()
    yield client
    await client.aclose()


@pytest.fixture
def unique_pod_agent() -> tuple[str, str]:
    uid = uuid.uuid4().hex[:8]
    return f"pod-{uid}", f"agent-{uid}"
