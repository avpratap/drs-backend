"""Claude API wrapper.

No ANTHROPIC_API_KEY -> dev stub (returns a templated answer, no network call) so
the support bot / admin copilot are testable now; the moment the key is set in
.env the same calls hit Claude (Opus 4.8, adaptive thinking). The anthropic SDK is
imported lazily so it only loads when actually calling out.
"""
import asyncio
import logging

from app.core.config import settings

log = logging.getLogger("drs.ai")

_UNAVAILABLE = "Sorry, the assistant is temporarily unavailable. Please try again."


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError, ConnectionError)):
        return True
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        # SDK connection/timeout errors carry no status; anything else unknown: no retry.
        name = type(exc).__name__
        return name in ("APIConnectionError", "APITimeoutError", "InternalServerError")
    return status_code == 429 or status_code >= 500
_client = None  # lazily-built AsyncAnthropic singleton (reuse its httpx connection pool)


def ai_enabled() -> bool:
    return bool(settings.ANTHROPIC_API_KEY)


def _get_client():
    global _client
    if _client is None:
        from anthropic import AsyncAnthropic  # lazy import

        _client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    return _client


async def complete(system: str, user_text: str, *, max_tokens: int | None = None) -> str:
    """One-shot Claude completion. Dev-stub when no key is configured.

    Hardened: output capped at AI_MAX_TOKENS, each call bounded by AI_TIMEOUT_SECONDS,
    and transient failures retried with exponential backoff (AI_MAX_RETRIES). Always
    fail-soft — never leaks an exception or PII to the caller.
    """
    if not ai_enabled():
        return (
            "[AI dev-stub — set ANTHROPIC_API_KEY to enable live answers] "
            + user_text.strip()[:200]
        )
    tokens = min(max_tokens or settings.AI_MAX_TOKENS, settings.AI_MAX_TOKENS)
    client = _get_client()
    last_exc: Exception | None = None
    for attempt in range(settings.AI_MAX_RETRIES + 1):
        try:
            async with asyncio.timeout(settings.AI_TIMEOUT_SECONDS):
                resp = await client.messages.create(
                    model=settings.CLAUDE_MODEL,
                    max_tokens=tokens,
                    system=system,
                    thinking={"type": "adaptive"},
                    messages=[{"role": "user", "content": user_text}],
                )
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            # Output validation: never return an empty answer or an unbounded blob.
            return text[:8000] if text else _UNAVAILABLE
        except Exception as exc:
            last_exc = exc
            # Retry only what can succeed on retry (timeouts, 429, 5xx, connection
            # errors). A 400/401/403/404 just burns quota/latency on every attempt.
            if attempt < settings.AI_MAX_RETRIES and _is_transient(exc):
                await asyncio.sleep(2**attempt)  # 1s, 2s, ...
                continue
            break
    log.warning("AI completion failed after retries: %s", last_exc)
    return _UNAVAILABLE
