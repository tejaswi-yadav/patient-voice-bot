"""μ-law audio helpers for Twilio Media Streams (8 kHz)."""

from __future__ import annotations

import audioop
import base64
import logging

logger = logging.getLogger(__name__)


def boost_mulaw_payload_b64(payload_b64: str, gain: float = 1.0) -> str:
    """Amplify a base64 μ-law payload before sending to Twilio.

    OpenAI Realtime pcmu output is often quiet on the outbound recording leg;
    boosting improves dual-channel MP3 quality for reviewers.
    """
    if gain <= 1.0:
        return payload_b64
    try:
        raw = base64.b64decode(payload_b64)
    except Exception:
        logger.warning("Invalid base64 audio payload")
        return payload_b64
    if not raw:
        return payload_b64

    linear = audioop.ulaw2lin(raw, 2)
    factor = max(1, min(int(round(gain)), 8))
    boosted = audioop.mul(linear, 2, factor)
    return base64.b64encode(audioop.lin2ulaw(boosted, 2)).decode("ascii")
