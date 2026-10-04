"""Keyless web-extract plugin — bundled, auto-loaded.

Fetches pages over plain HTTP (``httpx``) and reduces the HTML to readable
text (``lxml``). Both are core deps, so this provider is always available:
it is the no-key fallback that keeps ``web_extract`` working when no paid
extract backend (firecrawl / tavily / exa / parallel) is configured.
"""

from __future__ import annotations

from plugins.web.curl.provider import CurlExtractProvider


def register(ctx) -> None:
    """Register the keyless fetch provider with the plugin context."""
    ctx.register_web_search_provider(CurlExtractProvider())
