"""
llm.py — provider-agnostic LLM text/JSON generation.

One entry point, `generate()`, backs both the call-generation engine and the
position-review feature. The backend is chosen by LLM_PROVIDER:

    azure   → Azure OpenAI   (default; uses the openai SDK's AzureOpenAI client)
    openai  → OpenAI direct
    gemini  → Google Gemini  (legacy/fallback)

`json_mode=True` asks the provider to emit a strict JSON object (OpenAI/Azure
JSON mode, Gemini response_mime_type), so callers can json.loads() the result
without scraping it out of markdown.

Errors are normalised to:
    LLMQuotaError — a quota/rate wall that survived the SDK's own retries
    LLMError      — any other provider failure
"""

import random
import re
import time

from config.logger import get_logger
from config.settings import (
    LLM_PROVIDER,
    AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY,
    AZURE_OPENAI_DEPLOYMENT, AZURE_OPENAI_API_VERSION,
    OPENAI_API_KEY, OPENAI_MODEL,
    GEMINI_API_KEY, GEMINI_MODEL,
)

log = get_logger("llm")


class LLMError(Exception):
    """Generic LLM provider failure."""


class LLMQuotaError(LLMError):
    """Quota / rate wall that retrying won't fix soon (surface to the user)."""


# ── lazy, cached clients ─────────────────────────────────────────────────────
_azure_client = None
_openai_client = None
_gemini_client = None


def _get_azure():
    global _azure_client
    if _azure_client is None:
        if not (AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY and AZURE_OPENAI_DEPLOYMENT):
            raise LLMError(
                "Azure OpenAI not configured — set AZURE_OPENAI_ENDPOINT, "
                "AZURE_OPENAI_API_KEY and AZURE_OPENAI_DEPLOYMENT in .env."
            )
        from openai import AzureOpenAI
        _azure_client = AzureOpenAI(
            azure_endpoint=AZURE_OPENAI_ENDPOINT,
            api_key=AZURE_OPENAI_API_KEY,
            api_version=AZURE_OPENAI_API_VERSION,
            max_retries=4,  # SDK handles 429/5xx backoff (honours Retry-After)
        )
    return _azure_client


def _get_openai():
    global _openai_client
    if _openai_client is None:
        if not OPENAI_API_KEY:
            raise LLMError("OpenAI not configured — set OPENAI_API_KEY in .env.")
        from openai import OpenAI
        _openai_client = OpenAI(api_key=OPENAI_API_KEY, max_retries=4)
    return _openai_client


def _get_gemini():
    global _gemini_client
    if _gemini_client is None:
        if not GEMINI_API_KEY:
            raise LLMError("Gemini not configured — set GEMINI_API_KEY in .env.")
        from google import genai
        _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    return _gemini_client


# ── OpenAI / Azure (shared chat-completions path) ────────────────────────────

def _chat_completion(client, model: str, system, prompt, json_mode, max_tokens, temperature) -> str:
    import openai

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    kwargs = dict(model=model, messages=messages, temperature=temperature, max_completion_tokens=max_tokens)
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    try:
        resp = client.chat.completions.create(**kwargs)
    except openai.RateLimitError as e:
        raise LLMQuotaError(f"LLM rate/quota limit: {str(e)[:160]}") from e
    except openai.APIError as e:  # APIStatusError, APIConnectionError, APITimeoutError, ...
        raise LLMError(f"LLM API error: {str(e)[:160]}") from e
    return (resp.choices[0].message.content or "").strip()


# ── Gemini path (with backoff + daily-quota fail-fast) ───────────────────────

_GEM_TRANSIENT = {429, 500, 502, 503, 504}
_GEM_TRANSIENT_MARKERS = ("UNAVAILABLE", "RESOURCE_EXHAUSTED", "high demand", "overloaded",
                          "try again", "deadline")
_GEM_PER_DAY = ("PerDay", "per day", "GenerateRequestsPerDay", "_per_day")
_RETRY_HINT_RE = re.compile(r"(?:retry in |retryDelay'?:?\s*'?)([\d.]+)s")


def _gemini_generate(system, prompt, json_mode, max_tokens, temperature) -> str:
    from google import genai

    client = _get_gemini()
    full = (system + "\n\n" + prompt) if system else prompt
    cfg = dict(max_output_tokens=max_tokens, temperature=temperature)
    if json_mode:
        cfg["response_mime_type"] = "application/json"

    delay = 2.0
    last = None
    for attempt in range(1, 5):
        try:
            resp = client.models.generate_content(
                model=GEMINI_MODEL, contents=full,
                config=genai.types.GenerateContentConfig(**cfg),
            )
            return (resp.text or "").strip()
        except Exception as e:  # noqa: BLE001
            last = e
            msg = str(e)
            is_429 = getattr(e, "code", None) == 429 or "429" in msg or "RESOURCE_EXHAUSTED" in msg
            if is_429 and any(m in msg for m in _GEM_PER_DAY):
                raise LLMQuotaError("Gemini daily free-tier quota exhausted") from e
            transient = getattr(e, "code", None) in _GEM_TRANSIENT or any(
                m in msg for m in _GEM_TRANSIENT_MARKERS)
            if attempt == 4 or not transient:
                raise LLMError(f"Gemini error: {msg[:160]}") from e
            hint = _RETRY_HINT_RE.search(msg)
            sleep_s = min(float(hint.group(1)) if hint else delay, 60.0) + random.uniform(0, 0.75)
            log.warning("Gemini transient (try %s/4, retry %.1fs): %s", attempt, sleep_s, msg[:120])
            time.sleep(sleep_s)
            delay *= 2
    raise LLMError(f"Gemini error: {str(last)[:160]}")


# ── public API ───────────────────────────────────────────────────────────────

def generate(prompt: str, *, system: str | None = None, json_mode: bool = False,
             max_tokens: int = 1024, temperature: float = 0.3,
             model: str | None = None) -> str:
    """Generate text (or a JSON string when json_mode) from the active provider.

    `model` overrides the default model/deployment for this one call (e.g. a
    heavier deployment for /review vs. a cheap mini for high-frequency generation).
    """
    if LLM_PROVIDER == "azure":
        return _chat_completion(_get_azure(), model or AZURE_OPENAI_DEPLOYMENT, system, prompt,
                                json_mode, max_tokens, temperature)
    if LLM_PROVIDER == "openai":
        return _chat_completion(_get_openai(), model or OPENAI_MODEL, system, prompt,
                                json_mode, max_tokens, temperature)
    if LLM_PROVIDER == "gemini":
        return _gemini_generate(system, prompt, json_mode, max_tokens, temperature)
    raise LLMError(f"Unknown LLM_PROVIDER: {LLM_PROVIDER!r} (use azure|openai|gemini)")
