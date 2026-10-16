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


def ai_enabled() -> bool:
    return bool(settings.ANTHROPIC_API_KEY)


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
    from anthropic import AsyncAnthropic  # lazy import

    client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
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
            if attempt < settings.AI_MAX_RETRIES:
                await asyncio.sleep(2**attempt)  # 1s, 2s, ...
    log.warning("AI completion failed after retries: %s", last_exc)
    return _UNAVAILABLE
