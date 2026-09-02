"""Startup credential reporting.

Prints, at boot, exactly which credentials were picked up and which were not.
Without this the only way to discover that a ``.env`` was ignored is to click
into the UI and notice a badge - a bad way to find out minutes before a demo.

Nothing here ever prints a secret. Values are reported as found/missing, and a
key that is present is shown only as a masked prefix, which is enough to tell
two keys apart without putting either in a log or a screen share.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .config import PROJECT_ROOT, Settings

logger = logging.getLogger(__name__)

ENV_PATH: Path = PROJECT_ROOT / ".env"


def mask(value: str) -> str:
    """Render a secret as a recognisable but non-usable fragment."""
    if not value:
        return "(missing)"
    if len(value) <= 12:
        return f"{value[:3]}…{'*' * 6}"
    return f"{value[:10]}…{'*' * 6} ({len(value)} chars)"


def credential_report(settings: Settings) -> list[str]:
    """Build the boot report as lines, so tests can assert on it."""
    lines: list[str] = []
    add = lines.append

    add("=" * 68)
    add("  SettleTrace - CREDENTIAL CHECK")
    add("=" * 68)

    if ENV_PATH.exists():
        add(f"  .env file .......... found at {ENV_PATH}")
    else:
        add(f"  .env file .......... NOT FOUND at {ENV_PATH}")
        add("                       (copy .env.example to .env to add keys)")

    razorpay_ok = settings.razorpay_configured
    add("")
    add(f"  RAZORPAY_KEY_ID .... {mask(settings.razorpay_key_id)}")
    add(f"  RAZORPAY_KEY_SECRET  {mask(settings.razorpay_key_secret)}")
    add(f"  Razorpay ........... {'FOUND' if razorpay_ok else 'MISSING'}")

    add("")
    add(f"  GEMINI_API_KEY ..... {mask(settings.gemini_api_key)}")
    add(f"  GROQ_API_KEY ....... {mask(settings.groq_api_key)}")
    add(f"  ANTHROPIC_API_KEY .. {mask(settings.anthropic_api_key)}")
    add(f"  LLM ................ {'FOUND' if settings.llm_configured else 'MISSING'}")
    if settings.llm_configured:
        add(f"  Active provider .... {settings.active_llm_provider}")
        add(f"  Model .............. {settings.llm_model_name}")
    else:
        add("                       (any one of the three keys above enables")
        add("                        live explanations; none is required)")

    add("")
    add(f"  SETTLETRACE_USE_SANDBOX = {str(settings.settletrace_use_sandbox).lower()}")

    # The most confusing state to be in, so it is called out explicitly rather
    # than left to be inferred from two separate lines above.
    if settings.settletrace_use_sandbox and not razorpay_ok:
        add("")
        add("  >> Sandbox mode is ON but Razorpay credentials are missing.")
        add("     The app will run on GENERATED data and show a banner saying so.")
    elif razorpay_ok and not settings.settletrace_use_sandbox:
        add("")
        add("  >> Razorpay keys found, but sandbox mode is OFF.")
        add("     Set SETTLETRACE_USE_SANDBOX=true in .env to use live data.")

    add("")
    if settings.settletrace_use_sandbox and razorpay_ok:
        add("  DATA:  live Razorpay sandbox (verify with 'Test connection')")
    else:
        add("  DATA:  generated sample data")

    add(
        "  AI:    "
        + (
            f"live explanations via {settings.active_llm_provider}"
            if settings.llm_configured
            else "template explanations (fallback mode)"
        )
    )
    add("=" * 68)
    return lines


def log_credential_report(settings: Settings | None = None) -> None:
    """Print the report to the console at startup."""
    from .config import get_settings

    for line in credential_report(settings or get_settings()):
        logger.info(line)
