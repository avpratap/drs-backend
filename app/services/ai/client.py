"""Claude API wrapper.

No ANTHROPIC_API_KEY -> dev stub (returns a templated answer, no network call) so
the support bot / admin copilot are testable now; the moment the key is set in
.env the same calls hit Claude (Opus 4.8, adaptive thinking). The anthropic SDK is
imported lazily so it only loads when actually calling out.
"""
import logging

from app.core.config import settings

log = logging.getLogger("drs.ai")


def ai_enabled() -> bool:
    return bool(settings.ANTHROPIC_API_KEY)


async def complete(system: str, user_text: str, *, max_tokens: int = 1024) -> str:
    """One-shot Claude completion. Dev-stub when no key is configured."""
    if not ai_enabled():
        return (
            "[AI dev-stub — set ANTHROPIC_API_KEY to enable live answers] "
            + user_text.strip()[:200]
        )
    try:
        from anthropic import AsyncAnthropic  # lazy import

        client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        resp = await client.messages.create(
            model=settings.CLAUDE_MODEL,
            max_tokens=max_tokens,
            system=system,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": user_text}],
        )
        return "".join(b.text for b in resp.content if b.type == "text").strip()
    except Exception as exc:
        log.warning("AI completion failed: %s", exc)
        return "Sorry, the assistant is temporarily unavailable. Please try again."
