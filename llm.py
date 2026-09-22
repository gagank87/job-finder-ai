"""
llm.py — one text-in/text-out call with automatic multi-provider fallback.

CV tailoring (cvtailor) and the optional JD analyzer (jdfit_claude) need just
one thing from a model: send a system + user prompt, get text back. complete()
provides that, trying a CONFIGURED CHAIN of providers in order and returning the
first success — so if your primary backend is down, rate-limited, or out of
quota, the tool automatically falls through to the next FREE provider instead of
failing the job.

Providers (all optional; one is skipped when its credential/SDK is missing):
  * anthropic — Claude via the Anthropic SDK. Two auth modes, auto-selected:
      Amazon Bedrock (AWS_BEARER_TOKEN_BEDROCK) or direct Anthropic
      (ANTHROPIC_API_KEY). See config.use_bedrock / active_model_id.
  * groq      — Groq's free API (OpenAI-compatible). GROQ_API_KEY.
  * gemini    — Google Gemini via its OpenAI-compatible endpoint. GEMINI_API_KEY.

Order is config.LLM_PROVIDER_ORDER; models are config.GROQ_MODEL / GEMINI_MODEL.
Credentials are read at runtime from env vars or gitignored secrets files
(config.get_*) — NEVER stored in the repo, NEVER printed or logged. This module
never fabricates: a provider that errors is skipped, and if all fail complete()
returns ok=False with the collected reasons so the caller degrades cleanly.
"""

import json
import os

import config
import utils

try:
    import anthropic
    _ANTHROPIC_SDK = True
except Exception:  # noqa: BLE001
    anthropic = None
    _ANTHROPIC_SDK = False


class LLMError(Exception):
    """A single provider failed (skipped so the chain can try the next)."""


def sdk_available():
    """True if the Anthropic SDK is importable (for the anthropic provider)."""
    return _ANTHROPIC_SDK


# ---------------------------------------------------------------------------
# anthropic provider (Claude — Bedrock or direct Anthropic)
# ---------------------------------------------------------------------------
def anthropic_client():
    """
    (client, reason). A ready anthropic client on success, else None + reason.
    Reuses config's Bedrock-vs-direct auto-selection. Never logs the credential.
    """
    if not _ANTHROPIC_SDK:
        return None, "the 'anthropic' package isn't installed (pip install anthropic)"
    if config.use_bedrock():
        # The SDK reads the bearer token from AWS_BEARER_TOKEN_BEDROCK. If it
        # only lives in the gitignored file, expose it to THIS process (never
        # persisted, never logged).
        token = config.get_bedrock_token()
        if token and not os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
            os.environ["AWS_BEARER_TOKEN_BEDROCK"] = token
        try:
            return anthropic.AnthropicBedrock(aws_region=config.AWS_REGION), ""
        except Exception as e:  # noqa: BLE001
            return None, f"couldn't initialize the Bedrock client: {e}"
    key = config.get_api_key()
    if not key:
        return None, "no Claude credentials (ANTHROPIC_API_KEY / Bedrock token)"
    try:
        return anthropic.Anthropic(api_key=key), ""
    except Exception as e:  # noqa: BLE001
        return None, f"couldn't initialize the Anthropic client: {e}"


def _anthropic_available():
    if not _ANTHROPIC_SDK:
        return False, "anthropic SDK not installed"
    if config.use_bedrock():
        return True, ""
    return (True, "") if config.get_api_key() else (False, "no Claude credentials")


def _anthropic_complete(system, prompt, max_tokens):
    client, reason = anthropic_client()
    if client is None:
        raise LLMError(reason)
    resp = client.messages.create(
        model=config.active_model_id(),
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(getattr(b, "text", "") for b in getattr(resp, "content", []))
    if not text.strip():
        raise LLMError("empty response")
    return text


# ---------------------------------------------------------------------------
# OpenAI-compatible providers (Groq, Gemini) — one code path via /chat/completions
# ---------------------------------------------------------------------------
def _openai_chat(base_url, key, model, system, prompt, max_tokens):
    """Call an OpenAI-compatible chat endpoint. Raises LLMError on any problem."""
    s = utils.get_session()
    try:
        r = s.post(
            base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            data=json.dumps({
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.3,
            }),
            timeout=config.HTTP_TIMEOUT * 3,  # generation can take a while
        )
    except Exception as e:  # noqa: BLE001
        raise LLMError(f"request failed: {e}")
    if r.status_code != 200:
        # Truncated body only (may echo the prompt, never the key/header).
        raise LLMError(f"HTTP {r.status_code}: {r.text[:160]}")
    try:
        msg = r.json()["choices"][0]["message"]
    except (KeyError, IndexError, TypeError, ValueError):
        raise LLMError(f"unexpected response shape: {r.text[:160]}")
    # Some reasoning models (e.g. Gemini Flash) can spend the whole token budget
    # on internal thinking and return a message with no `content`. Treat that as
    # an empty response so the chain falls through, not as a malformed reply.
    text = msg.get("content") if isinstance(msg, dict) else None
    if not text or not str(text).strip():
        raise LLMError("empty response (model returned no content — "
                       "raise max_tokens if this recurs)")
    return text


def _groq_complete(system, prompt, max_tokens):
    key = config.get_groq_key()
    if not key:
        raise LLMError("no GROQ_API_KEY")
    return _openai_chat(config.GROQ_BASE_URL, key, config.GROQ_MODEL,
                        system, prompt, max_tokens)


def _gemini_complete(system, prompt, max_tokens):
    key = config.get_gemini_key()
    if not key:
        raise LLMError("no GEMINI_API_KEY")
    return _openai_chat(config.GEMINI_BASE_URL, key, config.GEMINI_MODEL,
                        system, prompt, max_tokens)


# ---------------------------------------------------------------------------
# Registry + chain
# ---------------------------------------------------------------------------
_REGISTRY = {
    "anthropic": {
        "complete": _anthropic_complete,
        "available": _anthropic_available,
        "label": lambda: (f"Claude ({config.active_model_id()} via "
                          f"{'Bedrock' if config.use_bedrock() else 'Anthropic'})"),
    },
    "groq": {
        "complete": _groq_complete,
        "available": lambda: ((True, "") if config.get_groq_key()
                              else (False, "no GROQ_API_KEY")),
        "label": lambda: f"Groq ({config.GROQ_MODEL})",
    },
    "gemini": {
        "complete": _gemini_complete,
        "available": lambda: ((True, "") if config.get_gemini_key()
                              else (False, "no GEMINI_API_KEY")),
        "label": lambda: f"Gemini ({config.GEMINI_MODEL})",
    },
}


def _ordered_names():
    """Provider names in configured order, de-duplicated, known-only."""
    out = []
    for name in config.LLM_PROVIDER_ORDER:
        if name in _REGISTRY and name not in out:
            out.append(name)
    return out


def available_providers():
    """[(name, label)] for providers whose credentials are ready, in order."""
    ready = []
    for name in _ordered_names():
        ok, _ = _REGISTRY[name]["available"]()
        if ok:
            ready.append((name, _REGISTRY[name]["label"]()))
    return ready


def available():
    """
    (ok, reason, labels). ok is True if at least one provider is ready; labels
    is the ordered list of ready provider labels. On failure, reason explains
    every provider's missing credential so the user knows exactly what to set.
    """
    ready = available_providers()
    if ready:
        return True, "", [label for _, label in ready]
    why = "; ".join(f"{name}: {_REGISTRY[name]['available']()[1]}"
                    for name in _ordered_names())
    return (False,
            "No LLM provider is configured. Set ANY ONE of: a Claude key "
            "(ANTHROPIC_API_KEY) or Bedrock token (AWS_BEARER_TOKEN_BEDROCK), a "
            "free GROQ_API_KEY, or a free GEMINI_API_KEY (env var or the matching "
            f"secrets/*.txt file). [{why}]",
            [])


def active_label():
    """Human string describing the provider chain, e.g. 'Groq (...) → Gemini (...)'."""
    labels = [label for _, label in available_providers()]
    return " -> ".join(labels) if labels else "no provider configured"


def complete(system, prompt, max_tokens=2000):
    """
    Send system+prompt to the first READY provider; on any error, fall through
    to the next. Returns {ok, text, provider, error} and NEVER raises.

    provider is the label of whichever provider produced the text. On total
    failure, ok is False and error collects each provider's reason.
    """
    ready = available_providers()
    if not ready:
        return {"ok": False, "text": "", "provider": "", "error": available()[1]}

    errors = []
    for name, label in ready:
        try:
            text = _REGISTRY[name]["complete"](system, prompt, max_tokens)
            if errors:  # we fell through to a backup — say which one worked
                utils.info(f"LLM: used fallback provider '{name}'.")
            return {"ok": True, "text": text, "provider": label, "error": ""}
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")
            utils.info(f"LLM provider '{name}' unavailable ({e}); trying next...")
    return {"ok": False, "text": "", "provider": "",
            "error": "all providers failed - " + "; ".join(errors)}
