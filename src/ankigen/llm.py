"""LLM client: any OpenAI-compatible provider, plus Gemini's native SDK.

Ported from the v1 `core/agents.py`, reshaped so every call reports the model
and token usage — the run report needs both.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass

from ankigen.config import ensure_free, settings

logger = logging.getLogger(__name__)

_gemini_client = None
# Keyed by (base_url, api_key) so switching providers mid-process is safe.
_openai_clients: dict[tuple[str, str], object] = {}
# Models observed to reject response_format; skip JSON mode for them thereafter.
_no_json_mode: set[str] = set()


class TransientProviderError(RuntimeError):
    """A provider hiccup worth retrying (overloaded, rate limited, 5xx)."""


# A 500 belongs here too: Gemma answered one long prompt with "500 INTERNAL.
# Internal error encountered.", and read as a real error that ended the whole
# chain instead of moving on to the next model.
_RETRYABLE = ("429", "rate limit", "overload", "temporarily", "timeout",
              "500 internal", "internal error", "502", "503", "504", "resource_exhausted")

# A *daily* cap, as opposed to a per-minute one. Both arrive as 429s and both
# carry a "retry in 47s" hint, but waiting out a daily quota inside one run is
# hopeless: it cost a CI job 23 minutes of sleeping before the job timed out.
_DAILY_QUOTA = re.compile(r"per[-_ ]?day", re.IGNORECASE)


# Models whose daily allowance ran out during this run; asking again just
# spends the retry budget to be told the same thing.
_spent: set[str] = set()

# Models that just failed to answer because they were overloaded, and until
# when (time.monotonic()) they go to the back of the chain. Without this every
# request started again at the top and sat through the same minutes of 503s:
# a CI run spent four minutes per request finding out that 3.8 and 3.7 were
# busy before 3.6 answered in seconds, and ran into the job timeout.
_busy_until: dict[str, float] = {}
BUSY_COOLDOWN = 600
# While another model is left to try, a busy one gets this many attempts
# rather than the full budget. One, because a 503 is not free: gemini-3.8-flash
# answered nothing but 503s one morning and was still "out of quota for today"
# by eleven, so every retry of a busy model spends its daily allowance.
FALLTHROUGH_ATTEMPTS = 1


class QuotaExhausted(RuntimeError):
    """The provider's allowance for the day is gone. Nothing to wait for."""


class BudgetExhausted(QuotaExhausted):
    """This run's share of the allowance (MAX_LLM_CALLS) is spent. A quota
    like any other to the stages: each stops asking and keeps what it has."""


# Calls this process has made, against MAX_LLM_CALLS. Pipelines share one
# subscription and the same free tiers, and a cap per run is how they share.
# Locked: the images stage checks pictures on several threads at once.
_calls = 0
_calls_lock = threading.Lock()


def calls_made() -> int:
    return _calls


def _spend() -> None:
    global _calls
    cap = settings.max_llm_calls
    with _calls_lock:
        if cap and _calls >= cap:
            raise BudgetExhausted(f"this run's budget of {cap} model call(s) is spent "
                                  "(MAX_LLM_CALLS, the pipeline's budget)")
        _calls += 1


class ProviderUnavailable(RuntimeError):
    """The provider cannot answer this run at all: a usage limit, no login,
    no CLI. Not retried, and not tried on the next model either, since every
    model behind one subscription shares its limit."""


# Providers that failed outright this run, with why. Once Claude has said its
# usage limit is reached, every later request goes straight to the fallback
# instead of asking again and waiting to be told the same thing.
_down: dict[str, str] = {}

# The Claude Code CLI, told to be a plain JSON endpoint: no tools, none of this
# machine's settings or MCP servers, no CLAUDE.md from a working directory, and
# a one-line system prompt in place of Claude Code's own, which is far longer
# than any card prompt and would be spent from the usage limit on every call.
CLAUDE_SYSTEM = ("You write and check flashcards for a learning pipeline. Follow the "
                 "user's instructions exactly and reply with JSON only, no prose.")
CLAUDE_TIMEOUT = 600
# Credentials that would make the CLI bill an API account instead of using the
# subscription login. Removed from its environment, so that a stray key on a
# machine or in CI can never turn a free call into a paid one.
_API_CREDENTIALS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                    "CLAUDE_CODE_USE_FOUNDRY")
_LIMIT = re.compile(r"usage limit|hit your limit|limit reached|out of (?:extra )?usage|"
                    r"credit balance|not logged in|please run /login|invalid api key|"
                    r"oauth token|authentication", re.IGNORECASE)


def _claude_json(model: str, prompt: str) -> tuple[str, int, int]:
    """One answer from Claude through the Claude Code CLI, on the subscription."""
    cli = shutil.which("claude")
    if not cli:
        raise ProviderUnavailable("the Claude Code CLI is not installed")
    env = {k: v for k, v in os.environ.items() if k not in _API_CREDENTIALS}
    try:
        proc = subprocess.run(
            [cli, "-p", "--output-format", "json", "--model", model, "--tools", "",
             "--system-prompt", CLAUDE_SYSTEM, "--setting-sources", "",
             "--strict-mcp-config", "--no-session-persistence"],
            input=prompt, capture_output=True, text=True, encoding="utf-8",
            env=env, cwd=tempfile.gettempdir(), timeout=CLAUDE_TIMEOUT,
        )
    except subprocess.TimeoutExpired as e:
        raise TransientProviderError(f"claude timeout after {CLAUDE_TIMEOUT}s") from e
    try:
        out = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        out = {}
    if proc.returncode != 0 or out.get("is_error") or out.get("subtype") != "success":
        problem = str(out.get("result") or proc.stderr or proc.stdout or "no output").strip()
        problem = f"claude ({model}): {problem[:300]}"
        if _LIMIT.search(problem):
            raise ProviderUnavailable(problem)
        raise RuntimeError(problem)
    usage = out.get("usage") or {}
    read = (usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0))
    return extract_json(out.get("result", "")), read, usage.get("output_tokens", 0)


def _is_retryable(message: str) -> bool:
    msg = message.lower()
    return any(token in msg for token in _RETRYABLE)


def _is_daily_quota(message: str) -> bool:
    return bool(_DAILY_QUOTA.search(message))


def _provider_error(response) -> str | None:
    """Aggregators can report failures *inside* a 200 response.

    OpenRouter returns {"error": {...}} with no `choices`, which the OpenAI SDK
    surfaces as choices=None. Without this check that became a TypeError deep in
    the parsing code instead of a retry.
    """
    if getattr(response, "choices", None):
        return None                      # a usable reply; nothing to report

    err = getattr(response, "error", None)
    extra = getattr(response, "model_extra", None)
    if err is None and isinstance(extra, dict):
        err = extra.get("error")
    if isinstance(err, dict) and err.get("message"):
        return str(err["message"])
    return "provider returned no choices"


@dataclass
class LLMResult:
    data: dict
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


def _get_gemini_client():
    global _gemini_client
    if not settings.google_api_key:
        return None
    if _gemini_client is None:
        try:
            from google import genai
        except ImportError as e:      # pragma: no cover - env-specific
            raise RuntimeError(
                "LLM_PROVIDER=gemini needs the google-genai package, which is an "
                "optional dependency: pip install 'ankigen[gemini]' (or "
                "pip install google-genai)."
            ) from e
        _gemini_client = genai.Client(api_key=settings.google_api_key)
    return _gemini_client


def _get_openai_client(base_url: str, api_key: str):
    cache_key = (base_url, api_key)
    if cache_key not in _openai_clients:
        from openai import OpenAI
        # Local servers ignore the key, but the SDK requires a non-empty string.
        _openai_clients[cache_key] = OpenAI(base_url=base_url, api_key=api_key or "none")
    return _openai_clients[cache_key]


def check_connection() -> str | None:
    """Error message if the configured provider is unreachable, else None."""
    cfg = settings.resolve_llm()
    provider = cfg["provider"]

    if provider == "gemini":
        return None if _get_gemini_client() else "No GOOGLE_API_KEY configured."

    if cfg["needs_key"] and not cfg["api_key"]:
        return f"No API key for '{provider}'. Set LLM_API_KEY in .env."

    base = (cfg["base_url"] or "").rstrip("/")
    try:
        import requests
        if provider == "ollama":
            resp = requests.get(f"{base.removesuffix('/v1')}/api/tags", timeout=5)
        else:
            headers = {"Authorization": f"Bearer {cfg['api_key']}"} if cfg["api_key"] else {}
            resp = requests.get(f"{base}/models", headers=headers, timeout=10)
        resp.raise_for_status()
        return None
    except Exception as e:
        return f"Cannot connect to {provider} at {base}: {e}"


def extract_json(text: str) -> str:
    """Pull a JSON object out of a reply that may be fenced or wrapped in prose."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t).strip()
    if t.startswith("{"):
        return t
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end > start:
        return t[start:end + 1]
    return t


def _looks_like_json_mode_rejection(err: Exception) -> bool:
    msg = str(err).lower()
    return any(tok in msg for tok in ("response_format", "json_object", "json mode", "json_schema",
                                      "response_mime_type"))


def _usage(response) -> tuple[int, int]:
    u = getattr(response, "usage", None)
    return (getattr(u, "prompt_tokens", 0) or 0, getattr(u, "completion_tokens", 0) or 0)


def _chat_json(client, model: str, prompt: str) -> tuple[str, int, int]:
    """Request JSON, degrading to text extraction where JSON mode is unsupported."""
    messages = [{"role": "user", "content": prompt}]
    if model not in _no_json_mode:
        try:
            response = client.chat.completions.create(
                model=model, messages=messages, response_format={"type": "json_object"},
            )
            if (problem := _provider_error(response)):
                raise TransientProviderError(problem)
            return (response.choices[0].message.content or "", *_usage(response))
        except TransientProviderError:
            raise
        except Exception as e:
            if not _looks_like_json_mode_rejection(e):
                raise
            logger.info("%s rejects JSON mode; parsing JSON out of plain text.", model)
            _no_json_mode.add(model)

    response = client.chat.completions.create(model=model, messages=messages)
    if (problem := _provider_error(response)):
        raise TransientProviderError(problem)
    return (extract_json(response.choices[0].message.content or ""), *_usage(response))


def preflight() -> None:
    """Check the configured providers could work, before any work is done.

    Makes no network call, so it costs nothing against a metered free tier. It
    exists because a missing optional dependency was only discovered after
    ingest and targeting had run, and then reported once per request.
    """
    for label, cfg in (("LLM_PROVIDER", settings.resolve_llm()),
                       ("the card checker (VERIFY_PROVIDER)", settings.resolve_verify())):
        _preflight_one(label, cfg)


def _preflight_one(label: str, cfg: dict) -> None:
    provider, model = cfg["provider"], cfg["model"]
    if not model:
        raise RuntimeError(f"{label} is {provider} but no model is configured.")
    fallback = cfg.get("fallback")
    if fallback:
        _preflight_one(f"{label}'s fallback", fallback)
    if provider == "claude":
        if not shutil.which("claude"):
            if not fallback:
                raise RuntimeError(f"{label} is claude but the Claude Code CLI is not "
                                   "installed (npm install -g @anthropic-ai/claude-code).")
            logger.warning("The Claude Code CLI is not installed; using %s for this run.",
                           fallback["provider"])
            _down[provider] = "the Claude Code CLI is not installed"
        return
    if provider == "gemini":
        if not settings.google_api_key:
            raise RuntimeError(f"{label} is gemini but GOOGLE_API_KEY is empty.")
        _get_gemini_client()                      # raises if google-genai is missing
    elif cfg.get("needs_key") and not cfg.get("api_key"):
        raise RuntimeError(f"{label} is {provider} but LLM_API_KEY is empty.")


def _call_one_model(prompt: str, cfg: dict, model: str, max_retries: int) -> LLMResult:
    """One model, retried. Raises QuotaExhausted when its day is done."""
    ensure_free(cfg["provider"], model, settings.allow_paid_models)
    for attempt in range(max_retries):
        try:
            if cfg["provider"] == "claude":
                text, p_tok, c_tok = _claude_json(model, prompt)
                return LLMResult(json.loads(text), model, p_tok, c_tok)
            if cfg["provider"] == "gemini":
                from google.genai import types
                client = _get_gemini_client()
                if not client:
                    raise RuntimeError("No GOOGLE_API_KEY configured")
                json_mode = model not in _no_json_mode
                try:
                    response = client.models.generate_content(
                        model=model, contents=prompt,
                        config=types.GenerateContentConfig(response_mime_type="application/json")
                        if json_mode else None,
                    )
                except Exception as e:
                    # Some models on the same API (Gemma, at times) refuse JSON
                    # mode with a 400, which would otherwise end the whole chain.
                    if not (json_mode and _looks_like_json_mode_rejection(e)):
                        raise
                    logger.info("%s rejects JSON mode; parsing JSON out of plain text.", model)
                    _no_json_mode.add(model)
                    json_mode = False
                    response = client.models.generate_content(model=model, contents=prompt)
                meta = getattr(response, "usage_metadata", None)
                text = response.text if json_mode else extract_json(response.text)
                return LLMResult(
                    json.loads(text), model,
                    getattr(meta, "prompt_token_count", 0) or 0,
                    getattr(meta, "candidates_token_count", 0) or 0,
                )
            client = _get_openai_client(cfg["base_url"], cfg["api_key"])
            text, p_tok, c_tok = _chat_json(client, model, prompt)
            return LLMResult(json.loads(text), model, p_tok, c_tok)
        except json.JSONDecodeError as e:
            logger.warning("JSON parse failed (attempt %d/%d): %s", attempt + 1, max_retries, e)
            if attempt == max_retries - 1:
                raise
        except ProviderUnavailable:
            raise
        except Exception as e:
            msg = str(e)
            if _is_daily_quota(msg):
                raise QuotaExhausted(msg) from e
            if _is_retryable(msg) and attempt < max_retries - 1:
                match = re.search(r"(?:retryDelay|try again in)\D*?(\d+(?:\.\d+)?)s", msg)
                # The provider's own hint wins; otherwise back off exponentially
                # rather than linearly, since an overloaded model stays that way
                # for longer than the 5s and 10s a linear ramp waited.
                wait = min(90.0, float(match.group(1)) + 2) if match else min(60, 5 * 2 ** attempt)
                logger.info("%s busy (%s); waiting %.0fs (attempt %d/%d)",
                            model, msg[:60], wait, attempt + 1, max_retries)
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("Max retries exceeded")


def call_json(prompt: str, max_retries: int = 5, cfg: dict | None = None) -> LLMResult:
    """One JSON completion from the configured provider, or from its fallback.

    The fallback takes over when the provider fails outright, whatever the
    reason, and keeps every later request for the rest of the run: a Claude
    subscription that has reached its usage limit stays there for hours, and
    the answer to that is the free tier, never a purchase.
    """
    _spend()
    cfg = cfg or settings.resolve_llm()
    fallback = cfg.get("fallback")
    if fallback and cfg["provider"] in _down:
        return _call_chain(prompt, max_retries, fallback)
    try:
        return _call_chain(prompt, max_retries, cfg)
    except Exception as e:
        if not fallback:
            raise
        if not isinstance(e, json.JSONDecodeError):
            # One garbled answer is that prompt's problem, not the provider's.
            _down[cfg["provider"]] = str(e)
        logger.warning("%s could not answer (%s); using %s instead",
                       cfg["provider"], str(e)[:120], fallback["provider"])
        return _call_chain(prompt, max_retries, fallback)


def _call_chain(prompt: str, max_retries: int, cfg: dict) -> LLMResult:
    """One JSON completion, working down the configured chain of models.

    Two things make a chain worth having rather than a single model. Free tiers
    meter per model and give about twenty requests a day each, so the best
    model runs out mid-run; and the newest models are the busiest, answering
    503 for minutes at a time while the previous one replies instantly. Either
    way the answer is the same: move down the list and keep going.

    A model whose allowance is gone is skipped for the rest of the process
    rather than asked again once per request. One that was overloaded goes to
    the back of the chain for a while, and while there is somewhere else to
    go a busy model gets a short retry, not the full one: only the last model
    left is waited on patiently.
    """
    cfg = cfg or settings.resolve_llm()
    chain = [m for m in (cfg.get("models") or [cfg["model"]]) if m]
    usable = [m for m in chain if m not in _spent]
    if not usable:
        raise QuotaExhausted(
            f"every configured model is out of quota for today: {', '.join(chain)}"
        )
    now = time.monotonic()
    # Stable, so the preference order holds within the rested and the busy.
    usable.sort(key=lambda m: _busy_until.get(m, 0) > now)

    last: Exception | None = None
    for i, model in enumerate(usable):
        retries = max_retries if i == len(usable) - 1 else min(max_retries, FALLTHROUGH_ATTEMPTS)
        try:
            result = _call_one_model(prompt, cfg, model, retries)
            _busy_until.pop(model, None)
            return result
        except QuotaExhausted as e:
            logger.warning("%s is out of quota for today; falling back", model)
            _spent.add(model)
            last = e
        except ProviderUnavailable:
            raise                           # the next model shares the same limit
        except Exception as e:
            if not _is_retryable(str(e)):
                raise                       # a real error: bad key, bad request
            logger.warning("%s did not answer (%s); falling back", model, str(e)[:60])
            _busy_until[model] = time.monotonic() + BUSY_COOLDOWN
            last = e

    if isinstance(last, QuotaExhausted):
        raise QuotaExhausted(
            f"every configured model is out of quota for today: {', '.join(usable)}"
        ) from last
    raise last


IMAGE_CHECK_PROMPT = (
    "You are checking whether a picture belongs on a flashcard.\n\n"
    "The card asks:\n{card}\n\n"
    "It was illustrated by searching the web for: {query}\n\n"
    "Look at the picture. Answer JSON only:\n"
    '{{"shows": "<what the picture actually depicts, in a few words>", '
    '"helps": true or false}}\n\n'
    "\"helps\" is true only if the picture illustrates the card's subject — a "
    "diagram, a chart, a screenshot, a photograph of the thing itself. It is "
    "false for a stock photo of people, a company logo, an unrelated scene, or "
    "an image so cluttered or watermarked that it teaches nothing. Web search "
    "returns plausible-looking results from pages whose text matched but whose "
    "picture did not, so judge the picture, never the words around it."
)


def check_image(image: bytes, card: str, query: str, cfg: dict | None = None) -> tuple[bool, str]:
    """Does this picture actually illustrate the card? Returns (verdict, what it shows).

    Reading the page's title is not enough: a card about S3's flat namespace
    was illustrated with a stock photograph of a basketball player, served by
    an SEO page whose title matched the query exactly. Only looking at the
    image catches that.
    """
    _spend()
    cfg = cfg or settings.resolve_llm()
    if cfg["provider"] == "claude":
        # The CLI takes text on stdin; a picture would need its file tools,
        # which are switched off. Its fallback looks at pictures natively.
        if not cfg.get("fallback"):
            raise RuntimeError("Pictures are checked by FALLBACK_PROVIDER when the "
                               "checker is claude, and none is set.")
        cfg = cfg["fallback"]
    prompt = IMAGE_CHECK_PROMPT.format(card=card[:400], query=query)
    chain = [m for m in (cfg.get("models") or [cfg["model"]]) if m and m not in _spent]
    if not chain:
        raise QuotaExhausted("no model left to check images with")
    now = time.monotonic()
    chain.sort(key=lambda m: _busy_until.get(m, 0) > now)

    if cfg["provider"] == "gemini":
        from google.genai import types
        client = _get_gemini_client()
        if not client:
            # Without this the failure surfaced as "'NoneType' object has no
            # attribute 'models'", which sends you looking in the wrong place.
            raise RuntimeError("No GOOGLE_API_KEY configured, so images cannot be checked.")
        last: Exception | None = None
        for model in chain:
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=[types.Part.from_bytes(data=image, mime_type="image/jpeg"), prompt],
                    config=types.GenerateContentConfig(response_mime_type="application/json"),
                )
                return _verdict(json.loads(response.text))
            except Exception as e:
                if _is_daily_quota(str(e)):
                    _spent.add(model)
                elif _is_retryable(str(e)):
                    _busy_until[model] = time.monotonic() + BUSY_COOLDOWN
                else:
                    raise
                last = e
        raise last

    # OpenAI protocol: images ride along as a data URI.
    client = _get_openai_client(cfg["base_url"], cfg["api_key"])
    data_uri = "data:image/jpeg;base64," + base64.b64encode(image).decode()
    response = client.chat.completions.create(
        model=chain[0],
        messages=[{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": data_uri}},
        ]}],
    )
    return _verdict(json.loads(extract_json(response.choices[0].message.content or "")))


def _verdict(data) -> tuple[bool, str]:
    """The checker's answer, which is asked for as an object but sometimes
    arrives wrapped in a list — once as `'list' object has no attribute
    'get'`, which kept an unchecked picture on a card."""
    if isinstance(data, list):
        data = next((d for d in data if isinstance(d, dict)), {})
    if not isinstance(data, dict):
        raise ValueError(f"the checker answered with {type(data).__name__}, not an object")
    return bool(data.get("helps")), str(data.get("shows", ""))[:120]
