"""Keyless web extraction — plain HTTP fetch + HTML-to-text.

Rationale: every other extract backend (firecrawl, tavily, exa, parallel)
requires a paid API key. With none configured, ``web_extract`` degrades to
whatever ``web.backend`` names — typically a search-only provider — and the
tool is simply dead. For ordinary pages the paid services mostly buy
convenience: this provider covers that case with ``httpx`` + ``lxml``,
which are already core deps.

What it deliberately does NOT do: render JavaScript or defeat bot walls.
Those pages return HTTP 200 with an empty shell, which would look like
success. :meth:`_looks_unrendered` detects that and returns a typed error
pointing at ``browser_navigate`` instead of handing back 37 bytes of nothing.

Security note: unlike firecrawl (a remote service), this runs in-process on
the host, so a redirect to an internal address would be a genuine SSRF hole.
Redirects are therefore followed manually with every hop re-checked against
:func:`is_safe_url` and the website-access policy.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

import lxml.html

from agent.web_search_provider import WebSearchProvider
from tools.url_safety import is_safe_url
from tools.website_policy import check_website_access

logger = logging.getLogger(__name__)

# Browser-ish UA: many sites serve a degraded or empty page to obvious bots.
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_FETCH_TIMEOUT = 25
_MAX_REDIRECTS = 5
_MAX_BYTES = 5 * 1024 * 1024

# Non-content elements dropped before text extraction.
_STRIP_XPATH = (
    "//script|//style|//noscript|//svg|//nav|//footer|//header|//aside|//form|//iframe"
)

# Only these end a line. Naively joining every itertext() fragment shreds prose
# at each inline <a>/<b> — a Wikipedia article came out as 978 lines averaging
# 19 characters, which reads as a navigation dump rather than an article and
# got reported as "the body is missing". Inline tags must not break the line.
_BLOCK_TAGS = frozenset(
    {
        "address", "article", "blockquote", "br", "dd", "div", "dl", "dt",
        "fieldset", "figcaption", "figure", "footer", "h1", "h2", "h3", "h4",
        "h5", "h6", "hr", "li", "main", "ol", "p", "pre", "section", "table",
        "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
    }
)

# An unrendered SPA shell / bot wall yields almost no text for the markup it
# ships. Raw size alone is a bad discriminator — Reddit's challenge page is
# only ~8KB — so compare text density instead: a genuinely small page is
# text-dense (example.com: 142B of 1.2KB = 11%), a wall is not (37B of
# 8.4KB = 0.4%). Density is only consulted when the text is already tiny.
_MIN_TEXT = 500
_MIN_TEXT_DENSITY = 0.03

_CHALLENGE_MARKERS = (
    "just a moment",
    "enable javascript",
    "checking your browser",
    "attention required",
    "verify you are human",
    "please wait for verification",
    "cf-browser-verification",
)


def _block_text(root: Any) -> str:
    """Serialize an element tree to text, breaking lines only at block tags.

    Inline fragments are concatenated so a sentence interrupted by links stays
    one line; whitespace inside a line is collapsed and blank lines dropped.
    """
    parts: List[str] = []

    def walk(node: Any) -> None:
        tag = node.tag if isinstance(node.tag, str) else ""
        is_block = tag in _BLOCK_TAGS
        if is_block:
            parts.append("\n")
        if node.text:
            parts.append(node.text)
        for child in node:
            walk(child)
            if child.tail:
                parts.append(child.tail)
        if is_block:
            parts.append("\n")

    walk(root)
    lines = (" ".join(line.split()) for line in "".join(parts).split("\n"))
    return "\n".join(line for line in lines if line)


class CurlExtractProvider(WebSearchProvider):
    """Extract-only provider backed by a direct HTTP GET.

    Pairs with any search provider (e.g. ddgs) — it supplies the extract
    half that keyless setups otherwise lack.
    """

    @property
    def name(self) -> str:
        return "curl"

    @property
    def display_name(self) -> str:
        return "Direct fetch (httpx + lxml)"

    def is_available(self) -> bool:
        """Always True — httpx and lxml are core deps, and no key is needed."""
        return True

    def supports_search(self) -> bool:
        return False

    def supports_extract(self) -> bool:
        return True

    async def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        """Fetch each URL and return readable text.

        Per-URL failures (policy block, SSRF, HTTP error, unrendered page)
        become items with an ``error`` field rather than raising, matching
        the contract in :class:`WebSearchProvider`.
        """
        from tools.interrupt import is_interrupted as _is_interrupted

        want_html = kwargs.get("format") == "html"
        results: List[Dict[str, Any]] = []

        for url in urls:
            if _is_interrupted():
                results.append({"url": url, "title": "", "error": "Interrupted"})
                continue
            try:
                results.append(
                    await asyncio.wait_for(
                        self._fetch_one(url, want_html), timeout=_FETCH_TIMEOUT + 5
                    )
                )
            except asyncio.TimeoutError:
                results.append(
                    {
                        "url": url,
                        "title": "",
                        "content": "",
                        "error": (
                            f"Fetch timed out after {_FETCH_TIMEOUT}s — try "
                            "browser_navigate instead."
                        ),
                    }
                )
            except Exception as exc:  # noqa: BLE001 — never break the batch
                logger.warning("curl extract failed for %s: %s", url, exc)
                results.append(
                    {
                        "url": url,
                        "title": "",
                        "content": "",
                        "error": f"Fetch failed: {type(exc).__name__}: {exc}",
                    }
                )
        return results

    async def _fetch_one(self, url: str, want_html: bool) -> Dict[str, Any]:
        import httpx

        gate = self._gate(url)
        if gate:
            return gate

        async with httpx.AsyncClient(
            follow_redirects=False, timeout=_FETCH_TIMEOUT, headers={"User-Agent": _UA}
        ) as client:
            current = url
            for _ in range(_MAX_REDIRECTS + 1):
                resp = await client.get(current)
                if resp.status_code not in (301, 302, 303, 307, 308):
                    break
                location = resp.headers.get("location")
                if not location:
                    break
                # Re-gate every hop: a redirect into the internal network is
                # the whole reason this loop is manual.
                current = str(httpx.URL(current).join(location))
                gate = self._gate(current)
                if gate:
                    return gate
            else:
                return {
                    "url": current,
                    "title": "",
                    "content": "",
                    "error": f"Too many redirects (>{_MAX_REDIRECTS})",
                }

        if resp.status_code >= 400:
            return {
                "url": current,
                "title": "",
                "content": "",
                "error": f"HTTP {resp.status_code} {resp.reason_phrase}",
            }

        raw = resp.text[:_MAX_BYTES]
        ctype = resp.headers.get("content-type", "").lower()

        # Plain-text endpoints (.md/.txt/.json/raw.githubusercontent) need no
        # parsing — this is the case the browser stack is most overkill for.
        if "html" not in ctype and ctype:
            return {
                "url": current,
                "title": current.rsplit("/", 1)[-1],
                "content": raw,
                "raw_content": raw,
                "metadata": {"content_type": ctype, "status_code": resp.status_code},
            }
        # From here on `raw` is HTML *source* and must never reach the caller:
        # web_extract_tool's summarizer reads `raw_content` in preference to
        # `content`, so putting markup there feeds the LLM tags instead of prose.

        title, text = self._to_text(raw)

        if self._looks_unrendered(raw, title, text):
            return {
                "url": current,
                "title": title,
                "content": "",
                "error": (
                    "Page returned no readable text — it is JavaScript-rendered "
                    "or behind a bot/login wall. Use browser_navigate for this URL."
                ),
            }

        content = raw if want_html else text
        return {
            "url": current,
            "title": title,
            "content": content,
            "raw_content": content,
            "metadata": {"content_type": ctype, "status_code": resp.status_code},
        }

    def _gate(self, url: str) -> Dict[str, Any] | None:
        """Return an error item when the URL is policy-blocked or unsafe."""
        if not is_safe_url(url):
            return {
                "url": url,
                "title": "",
                "content": "",
                "error": "Blocked: URL targets a private or internal network address",
            }
        blocked = check_website_access(url)
        if blocked:
            logger.info(
                "Blocked web_extract for %s by rule %s", blocked["host"], blocked["rule"]
            )
            return {
                "url": url,
                "title": "",
                "content": "",
                "error": blocked["message"],
                "blocked_by_policy": {
                    "host": blocked["host"],
                    "rule": blocked["rule"],
                    "source": blocked["source"],
                },
            }
        return None

    @staticmethod
    def _to_text(raw: str) -> tuple[str, str]:
        """Reduce an HTML document to (title, readable text)."""
        try:
            doc = lxml.html.fromstring(raw)
        except Exception:  # noqa: BLE001 — malformed markup is not our problem
            return "", ""
        title_nodes = doc.xpath("//title/text()")
        title = str(title_nodes[0]).strip() if title_nodes else ""
        for node in doc.xpath(_STRIP_XPATH):
            parent = node.getparent()
            if parent is not None:
                parent.remove(node)
        return title, _block_text(doc)

    @staticmethod
    def _looks_unrendered(raw: str, title: str, text: str) -> bool:
        """True when HTTP 200 hides an empty SPA shell or a bot challenge.

        The title carries the tell as often as the body does — Reddit's wall
        renders 37 bytes of text under "Please wait for verification".
        """
        if any(m in f"{title} {text[:2000]}".lower() for m in _CHALLENGE_MARKERS):
            return True
        if len(text) >= _MIN_TEXT:
            return False
        return len(text) / max(len(raw), 1) < _MIN_TEXT_DENSITY

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Direct fetch (curl)",
            "badge": "free · no key · extract only",
            "tag": (
                "Plain HTTP fetch + HTML-to-text — no API key. Ordinary pages only; "
                "JS/bot-walled sites fall back to browser_navigate."
            ),
            "env_vars": [],
        }
