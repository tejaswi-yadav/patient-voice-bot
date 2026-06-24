"""OpenAI Realtime API WebSocket connection with retry and timeout handling."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-realtime"
FALLBACK_MODELS = ("gpt-realtime", "gpt-realtime-2")
DEFAULT_OPEN_TIMEOUT = 60.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_DELAY = 2.0


def build_realtime_url(model: str) -> str:
    return f"wss://api.openai.com/v1/realtime?model={model}"


async def connect_realtime(
    api_key: str,
    model: str = DEFAULT_MODEL,
    *,
    open_timeout: float = DEFAULT_OPEN_TIMEOUT,
    ping_interval: float = 20.0,
    ping_timeout: float = 60.0,
    close_timeout: float = 10.0,
) -> ClientConnection:
    """Open a single Realtime API WebSocket connection."""
    url = build_realtime_url(model)
    logger.info("Connecting to OpenAI Realtime: model=%s timeout=%ss", model, open_timeout)

    return await websockets.connect(
        url,
        additional_headers={"Authorization": f"Bearer {api_key}"},
        open_timeout=open_timeout,
        close_timeout=close_timeout,
        ping_interval=ping_interval,
        ping_timeout=ping_timeout,
        max_size=16 * 1024 * 1024,
    )


async def connect_realtime_with_retry(
    api_key: str,
    model: str = DEFAULT_MODEL,
    *,
    open_timeout: float = DEFAULT_OPEN_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
    retry_delay: float = DEFAULT_RETRY_DELAY,
    fallback_models: tuple[str, ...] = FALLBACK_MODELS,
) -> ClientConnection:
    """Connect to OpenAI Realtime with retries and model fallbacks."""
    models_to_try: list[str] = []
    for candidate in (model, *fallback_models):
        if candidate not in models_to_try:
            models_to_try.append(candidate)

    last_error: Exception | None = None

    for attempt in range(1, max_retries + 1):
        for candidate_model in models_to_try:
            try:
                ws = await connect_realtime(
                    api_key,
                    candidate_model,
                    open_timeout=open_timeout,
                )
                logger.info(
                    "OpenAI Realtime connected (model=%s, attempt=%d)",
                    candidate_model,
                    attempt,
                )
                return ws
            except (
                TimeoutError,
                asyncio.TimeoutError,
                websockets.exceptions.InvalidStatus,
                websockets.exceptions.ConnectionClosedError,
                OSError,
            ) as exc:
                last_error = exc
                logger.warning(
                    "OpenAI connect failed (model=%s, attempt=%d/%d): %s: %s",
                    candidate_model,
                    attempt,
                    max_retries,
                    type(exc).__name__,
                    exc,
                )

        if attempt < max_retries:
            delay = retry_delay * attempt
            logger.info("Retrying OpenAI connection in %.1fs...", delay)
            await asyncio.sleep(delay)

    raise ConnectionError(
        f"Failed to connect to OpenAI Realtime after {max_retries} attempts"
    ) from last_error


def is_ws_open(ws: Any) -> bool:
    """Check if a websockets client connection is open."""
    try:
        return ws.state.name == "OPEN"
    except AttributeError:
        return not getattr(ws, "closed", True)
