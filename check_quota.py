"""
CI quota gate: is there any AI provider left that can read a screenshot?

Sends one tiny request down the provider chain (see ai_providers.py).
Exit codes:
  0  at least one provider answered, so the farm can run
  3  no provider has quota left (or none answered in time), so skip the farm
  2  nothing is configured (no API keys), which is a setup error
Any other failure (a crash) exits non-zero, so a bug fails the job instead of silently skipping the farm.

GATE_IGNORE: optional comma-separated provider name prefixes to leave out of the check
(e.g. "mistral,zai" so low-accuracy providers with huge quotas don't keep the gate open).
"""

import os
import sys

from ai_providers import AllProvidersExhausted, PROVIDERS, ask_model, print_provider_summary

EXIT_OK = 0
EXIT_NOT_CONFIGURED = 2
EXIT_NO_QUOTA = 3
PROBE_TIMEOUT_S = 120


def main():
    if not PROVIDERS:
        print("No AI provider configured: set OPENAI_API_KEY, GEMINI_API_KEY, GROQ_API_KEY, "
              "CLOUDFLARE_API_KEY (+ CLOUDFLARE_ACCOUNT_ID), MISTRAL_API_KEY or ZAI_API_KEY.")
        return EXIT_NOT_CONFIGURED

    ignore = tuple(x.strip() for x in os.getenv("GATE_IGNORE", "").split(",") if x.strip())
    for provider in PROVIDERS:
        if ignore and provider.name.startswith(ignore):
            provider.dead_reason = "ignored by GATE_IGNORE"

    try:
        ask_model(
            [{"role": "user", "content": "Reply with the single word OK."}],
            require_vision=True,
            max_wait_s=PROBE_TIMEOUT_S,
        )
    except AllProvidersExhausted as e:
        print(f"\nNo provider with quota left: {e}")
        print_provider_summary()
        return EXIT_NO_QUOTA

    answered = next(p for p in PROVIDERS if p.calls)
    print(f"\nQuota available, first provider that answered: {answered.name}")
    print_provider_summary()
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
