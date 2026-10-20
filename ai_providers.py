"""
AI providers shared by the practice farm and the CI quota gate.

Each exercise needs two kinds of call, and the providers are ranked separately for each:
  read  - a request that carries a screenshot (identify the exercise, copy the question)
  solve - a text-only request (arrange the words, pick the option, fill the blank)
Rankings come from benchmarks on real Duolingo screenshots; see READ_ORDER / SOLVE_ORDER.
"""

import os
import re
import time

from dotenv import load_dotenv
from openai import OpenAI

# Providers are built at import time, so the .env must be loaded first.
load_dotenv()

# ---------------------------------------------------------------------------
# AI providers: OpenAI, Gemini models, Groq (text only), Cloudflare, Mistral, Z.AI
# ---------------------------------------------------------------------------
# A provider is only used when its API key is set. When one runs out of daily
# quota it is skipped for the rest of the run; per-minute limits and transient
# errors only put it on a short cooldown. Text-only providers (Groq's gpt-oss)
# are skipped for requests that carry a screenshot.
# Quota is the scarce resource, so the two roles are ranked differently:
#  - read:  Gemini 3.1 flash-lite reads best; Mistral 8b is fast, good enough and has the biggest
#           quota; Z.AI reads well but is slow and often overloaded; Cloudflare scout reads worst.
#  - solve: Groq gpt-oss-120b is fast and as accurate as the others, and using it first keeps
#           Gemini's quota for the read calls that only a few providers can do well.
# Override with READ_ORDER / SOLVE_ORDER (comma-separated provider-name prefixes, most preferred
# first). A provider matching no prefix goes last.

OPENAI_CHAT_MODEL = os.getenv("OPENAI_CHAT_MODEL", "gpt-5-nano")
# Gemini quotas are per project *per model*, so each model is its own link in the chain.
GEMINI_MODELS = [
    m.strip() for m in os.getenv(
        "GEMINI_MODELS",
        "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-3.5-flash,gemini-2.5-flash",
    ).split(",") if m.strip()
]
MISTRAL_MODEL = os.getenv("MISTRAL_MODEL", "ministral-8b-latest")
def _csv_env(name, default):
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


READ_ORDER = _csv_env("READ_ORDER", "openai,gemini/gemini-3.1-flash-lite,gemini,mistral,zai,cloudflare")
SOLVE_ORDER = _csv_env("SOLVE_ORDER", "openai,groq,gemini/gemini-3.1-flash-lite,gemini,cloudflare,mistral,zai")
GROQ_TEXT_MODEL = os.getenv("GROQ_TEXT_MODEL", "openai/gpt-oss-120b")
CLOUDFLARE_MODEL = os.getenv("CLOUDFLARE_MODEL", "@cf/meta/llama-4-scout-17b-16e-instruct")
ZAI_BASE_URL = os.getenv("ZAI_BASE_URL", "https://api.z.ai/api/paas/v4/")
ZAI_MODEL = os.getenv("ZAI_MODEL", "glm-4.6v-flash")

DAILY_QUOTA_MARKERS = (
    "perday", "insufficient_quota", "no credits", "spend limit",
    "insufficient balance",  # Z.AI code 1113 on paid models without a resource package
    "daily free allocation",  # Cloudflare Workers AI: 10,000 free neurons/day used up
    "requests per day", "tokens per day",  # Groq RPD / TPD limits
    "budget exhausted",  # Mistral 402 billing_api_budget_exhausted
)
OVERLOAD_MARKERS = ("overloaded", "high demand")  # 429/503 meaning "try again shortly"
TRANSIENT_COOLDOWN_S = 15
MAX_TRANSIENT_FAILURES = 20  # this many overloads/errors in a row without a success = out for this run
DEFAULT_RATE_LIMIT_COOLDOWN_S = 60
MAX_COOLDOWN_S = 600  # a longer retry hint than this is effectively "out for today"


class AllProvidersExhausted(BaseException):
    """No AI provider is left. Derives from BaseException so the many
    `except Exception` blocks around ask_model() cannot swallow it and keep looping."""


class Provider:
    def __init__(self, name, model, client, api="chat", vision=True):
        self.name = name
        self.vision = vision  # False: text-only model, never sent a screenshot
        self.model = model
        self.client = client
        self.api = api  # "responses" (OpenAI) or "chat" (OpenAI-compatible)
        self.dead_reason = None
        self.cooldown_until = 0.0
        self.calls = 0
        self.transient_failures = 0  # consecutive, reset on success

    def usable(self):
        return self.dead_reason is None and time.time() >= self.cooldown_until

    def ask(self, messages):
        if self.api == "responses":
            responses_input = []
            for message in messages:
                role = "developer" if message["role"] == "system" else message["role"]
                content = message["content"]
                if isinstance(content, list):
                    converted = []
                    for part in content:
                        if part["type"] == "text":
                            converted.append({"type": "input_text", "text": part["text"]})
                        elif part["type"] == "image_url":
                            converted.append({
                                "type": "input_image",
                                "image_url": part["image_url"]["url"],
                            })
                    content = converted
                responses_input.append({"role": role, "content": content})
            response = self.client.responses.create(model=self.model, input=responses_input)
            text = response.output_text
        else:
            response = self.client.chat.completions.create(
                model=self.model, messages=messages,
            )
            text = response.choices[0].message.content
        if not text or not text.strip():
            raise RuntimeError("empty response")
        return text.strip()


def _build_providers():
    providers = []
    key = os.getenv("OPENAI_API_KEY")
    if key:
        providers.append(Provider("openai", OPENAI_CHAT_MODEL, OpenAI(api_key=key), api="responses"))
    key = os.getenv("GEMINI_API_KEY")
    if key:
        gemini = OpenAI(
            api_key=key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        )
        for model in GEMINI_MODELS:
            providers.append(Provider(f"gemini/{model}", model, gemini))
    key = os.getenv("GROQ_API_KEY")
    if key:
        providers.append(Provider(
            f"groq/{GROQ_TEXT_MODEL}", GROQ_TEXT_MODEL,
            OpenAI(api_key=key, base_url="https://api.groq.com/openai/v1"),
            vision=False,
        ))
    key = os.getenv("CLOUDFLARE_API_KEY")
    account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID")
    if key and not account_id:
        print("  ⚠ CLOUDFLARE_API_KEY is set but CLOUDFLARE_ACCOUNT_ID is missing; skipping Cloudflare")
    elif key:
        providers.append(Provider(
            f"cloudflare/{CLOUDFLARE_MODEL}", CLOUDFLARE_MODEL,
            OpenAI(
                api_key=key,
                base_url=f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1",
            ),
        ))
    key = os.getenv("MISTRAL_API_KEY")
    if key:
        providers.append(Provider(
            f"mistral/{MISTRAL_MODEL}", MISTRAL_MODEL,
            OpenAI(api_key=key, base_url="https://api.mistral.ai/v1"),
        ))
    key = os.getenv("ZAI_API_KEY")
    if key:
        providers.append(Provider(
            f"zai/{ZAI_MODEL}", ZAI_MODEL, OpenAI(api_key=key, base_url=ZAI_BASE_URL),
        ))
    return providers


PROVIDERS = _build_providers()


def _retry_delay_s(text):
    """Seconds from a 'retry in 1h2m3.4s' / 'try again in 850ms' / "retryDelay': '17s'" hint, or None."""
    m = re.search(r"(?:retry|try again) in ([\d.]+)ms", text)
    if m:
        return float(m.group(1)) / 1000
    m = re.search(r"(?:retry|try again) in (?:(\d+)h)?(?:(\d+)m)?([\d.]+)s", text)
    if m:
        h, mi, sec = m.groups()
        return int(h or 0) * 3600 + int(mi or 0) * 60 + float(sec)
    m = re.search(r"retrydelay\W+(\d+)s", text)
    return float(m.group(1)) if m else None


def _handle_provider_error(provider, error):
    """Mark the provider dead, or put it on cooldown, depending on the error."""
    text = str(error).lower()
    status = getattr(error, "status_code", None)
    brief = str(error).replace("\n", " ")[:160]

    if status == 402 or any(marker in text for marker in DAILY_QUOTA_MARKERS):
        provider.dead_reason = "daily quota exhausted"
    elif status == 429:
        if any(marker in text for marker in OVERLOAD_MARKERS):
            _transient_failure(provider, "overloaded")
            return
        else:
            delay = _retry_delay_s(text) or DEFAULT_RATE_LIMIT_COOLDOWN_S
            if delay > MAX_COOLDOWN_S:
                provider.dead_reason = f"rate limited for {delay:.0f}s (treated as exhausted)"
            else:
                provider.cooldown_until = time.time() + delay + 1
                print(f"  ⏸ {provider.name}: rate limited, cooling down {delay:.0f}s")
                return
    elif status in (401, 403, 404):
        provider.dead_reason = f"HTTP {status} (bad key or model unavailable)"
    else:
        _transient_failure(provider, f"failed ({brief})")
        return
    print(f"  🚫 {provider.name} disabled for this run: {provider.dead_reason}")


def _transient_failure(provider, what):
    """Cool down briefly, but give up on a provider that keeps failing without ever answering."""
    provider.transient_failures += 1
    if provider.transient_failures >= MAX_TRANSIENT_FAILURES:
        provider.dead_reason = f"{what} {provider.transient_failures}x in a row"
        print(f"  🚫 {provider.name} disabled for this run: {provider.dead_reason}")
        return
    provider.cooldown_until = time.time() + TRANSIENT_COOLDOWN_S
    print(f"  ⚠ {provider.name} {what}; trying next provider")


def _ordered(providers, order):
    """Stable sort by the first (most preferred) prefix in `order` matching the provider's name."""
    def rank(provider):
        hits = [i for i, prefix in enumerate(order) if provider.name.startswith(prefix)]
        return min(hits) if hits else len(order)
    return sorted(providers, key=rank)


def describe_chain():
    reading = _ordered([p for p in PROVIDERS if p.vision], READ_ORDER)
    solving = _ordered(PROVIDERS, SOLVE_ORDER)
    return ("read:  " + " → ".join(p.name for p in reading)
            + "\nsolve: " + " → ".join(p.name for p in solving))


def _has_image(messages):
    return any(
        isinstance(m["content"], list) and any(part.get("type") == "image_url" for part in m["content"])
        for m in messages
    )


def ask_model(messages, require_vision=False, max_wait_s=None):
    """Ask the first usable provider for this kind of call; fall through on failure.

    A request with a screenshot is a "read" call (READ_ORDER, vision providers only); a
    text-only request is a "solve" call (SOLVE_ORDER).

    require_vision: only use providers that can read screenshots, even when `messages`
        has none (the quota gate uses this to probe the providers the farm really needs).
    max_wait_s: give up with AllProvidersExhausted instead of waiting longer than this
        for providers that are cooling down.
    """
    needs_vision = require_vision or _has_image(messages)
    candidates = _ordered(
        [p for p in PROVIDERS if p.vision or not needs_vision],
        READ_ORDER if needs_vision else SOLVE_ORDER,
    )
    deadline = time.time() + max_wait_s if max_wait_s else None
    while True:
        for provider in candidates:
            if not provider.usable():
                continue
            try:
                text = provider.ask(messages)
                provider.calls += 1
                provider.transient_failures = 0
                return text
            except Exception as e:
                _handle_provider_error(provider, e)

        alive = [p for p in candidates if p.dead_reason is None]
        if not alive:
            raise AllProvidersExhausted("All AI providers are exhausted or unavailable.")
        # Everything left is cooling down: wait for the soonest one.
        wait = max(min(p.cooldown_until for p in alive) - time.time(), 1)
        if deadline and time.time() + wait > deadline:
            raise AllProvidersExhausted("No provider became available in time.")
        print(f"  ⏳ All providers cooling down, waiting {wait:.0f}s")
        time.sleep(wait)


def print_provider_summary():
    print("\n🤖 AI providers:")
    if not PROVIDERS:
        print("  (none configured)")
    for p in PROVIDERS:
        status = f"disabled: {p.dead_reason}" if p.dead_reason else "ok"
        print(f"  {p.name}: {p.calls} calls ({status})")
