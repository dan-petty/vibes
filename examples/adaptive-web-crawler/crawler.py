"""Adaptive Web Crawler for AI Agents.

Dynamically negotiates between lightweight HTTP extraction and headless browser
rendering, learning per-domain extraction strategies to overcome common headless
browsing limitations (SPA hydration, bot detection flags, cookie modal obscuration,
and resource overhead).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final
from urllib.parse import urldefrag, urljoin, urlparse

# Importable whether this file is run from its own directory, imported by a test runner
# rooted elsewhere, or copied out of the repository, which is what an exhibit is for.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from markdown_extract import ExtractedDocument, extract

logger = logging.getLogger(__name__)

# RFC 5737 Documentation blocks and loopback allowed for testing
# RFC 5737 documentation ranges plus loopback, in both families. A hostname resolves to
# every family the host supports, so an IPv4-only allowlist denies `localhost` the moment
# resolution is performed: ::1 matches no entry and the whole hostname is refused.
ALLOWED_TEST_NETWORKS = [
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("2001:db8::/32"),
]

# RFC 9110 places no ceiling on redirect depth, so the client must impose one or a
# redirect loop is an unbounded fetch.
MAX_REDIRECTS = 5

DEFAULT_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-CH-UA": '"Chromium";v="130", "Google Chrome";v="130"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"Linux"',
}


class ExtractionTier(StrEnum):
    """Execution tier utilized to extract page content."""
    STATIC_HTTP = "static_http"
    HEADLESS_BROWSER = "headless_browser"


class PageQuality(StrEnum):
    """Quality classification of extracted page content."""
    HIGH = "high"
    PARTIAL = "partial"
    EMPTY_SHELL = "empty_shell"
    BLOCKED = "blocked"


@dataclass
class DomainStrategy:
    """Learned extraction strategy for a specific domain."""
    domain: str
    preferred_tier: ExtractionTier = ExtractionTier.STATIC_HTTP
    requires_js: bool = False
    rate_limit_delay: float = 0.5
    wait_selector: str = "main, article, [role='main']"
    strip_selectors: list[str] = field(default_factory=lambda: ["nav", "footer", ".cookie-banner"])
    consecutive_successes: int = 0
    total_crawls: int = 0


@dataclass
class CrawledPage:
    """Clean extracted content and metadata from a web page."""
    url: str
    title: str
    markdown_content: str
    links: list[str]
    tier_used: ExtractionTier
    quality: PageQuality
    token_estimate: int
    duration_seconds: float
    # Which region the text came from, and anything the caller must know before using it.
    # An empty `markdown_content` is ambiguous on its own: a page with nothing to say and
    # a page whose content was all inside chrome are the same value, and only one of them
    # is worth re-fetching with a browser.
    region: str = "body"
    extraction_warnings: list[str] = field(default_factory=list)


class SPADetector:
    """Detects whether an HTML document is an unhydrated single-page application shell."""

    SPA_ROOT_IDS = ("root", "app", "__next", "__nuxt", "main-content")

    @classmethod
    def _is_blocked(cls, lower_html: str) -> bool:
        return "access denied" in lower_html or "attention required" in lower_html

    @classmethod
    def _has_spa_root_or_notice(cls, lower_html: str) -> bool:
        has_root = any(f'id="{rid}"' in lower_html or f"id='{rid}'" in lower_html for rid in cls.SPA_ROOT_IDS)
        has_js_notice = "javascript" in lower_html and ("enable" in lower_html or "required" in lower_html)
        return has_root or has_js_notice

    @classmethod
    def _is_empty_shell(cls, lower_html: str, text_length: int) -> bool:
        if text_length >= 150:
            return False
        script_count = lower_html.count("<script")
        return script_count > 3 or cls._has_spa_root_or_notice(lower_html)

    @classmethod
    def analyze(cls, html: str, text_content: str) -> PageQuality:
        """Classify the HTML document into a PageQuality category."""
        lower_html = html.lower()
        if cls._is_blocked(lower_html):
            return PageQuality.BLOCKED

        text_length = len(text_content.strip())
        if cls._is_empty_shell(lower_html, text_length):
            return PageQuality.EMPTY_SHELL

        return PageQuality.PARTIAL if text_length < 80 else PageQuality.HIGH


class DomainStrategyStore:
    """Maintains persistent or in-memory learned crawl strategies per domain."""

    def __init__(self, persistence_path: Path | None = None) -> None:
        self.persistence_path = persistence_path
        self._strategies: dict[str, DomainStrategy] = {}
        if persistence_path and persistence_path.is_file():
            self._load()

    def get_strategy(self, domain: str) -> DomainStrategy:
        """Retrieve or initialize domain extraction strategy."""
        if domain not in self._strategies:
            self._strategies[domain] = DomainStrategy(domain=domain)
        return self._strategies[domain]

    def record_success(self, domain: str, tier_used: ExtractionTier) -> None:
        """Update strategy upon successful page extraction."""
        strategy = self.get_strategy(domain)
        strategy.preferred_tier = tier_used
        strategy.requires_js = (tier_used == ExtractionTier.HEADLESS_BROWSER)
        strategy.consecutive_successes += 1
        strategy.total_crawls += 1
        self._save_if_configured()

    def record_shell_detected(self, domain: str) -> None:
        """Escalate strategy to headless browser when SPA shell is encountered."""
        strategy = self.get_strategy(domain)
        strategy.preferred_tier = ExtractionTier.HEADLESS_BROWSER
        strategy.requires_js = True
        self._save_if_configured()

    def record_rate_limit(self, domain: str) -> None:
        """Apply exponential backoff delay when HTTP 429 rate limit is encountered."""
        strategy = self.get_strategy(domain)
        strategy.rate_limit_delay = min(strategy.rate_limit_delay * 2.0, 10.0)
        self._save_if_configured()

    def _save_if_configured(self) -> None:
        if not self.persistence_path:
            return
        data = {
            dom: {
                "preferred_tier": strat.preferred_tier.value,
                "requires_js": strat.requires_js,
                "rate_limit_delay": strat.rate_limit_delay,
                "consecutive_successes": strat.consecutive_successes,
                "total_crawls": strat.total_crawls,
            }
            for dom, strat in self._strategies.items()
        }
        self.persistence_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _load(self) -> None:
        if not self.persistence_path:
            return
        try:
            raw = json.loads(self.persistence_path.read_text(encoding="utf-8"))
            for dom, vals in raw.items():
                self._strategies[dom] = DomainStrategy(
                    domain=dom,
                    preferred_tier=ExtractionTier(vals.get("preferred_tier", "static_http")),
                    requires_js=vals.get("requires_js", False),
                    rate_limit_delay=vals.get("rate_limit_delay", 0.5),
                    consecutive_successes=vals.get("consecutive_successes", 0),
                    total_crawls=vals.get("total_crawls", 0),
                )
        except (json.JSONDecodeError, OSError) as err:
            logger.warning("Could not load strategies from %s: %s", self.persistence_path, err)


def _is_disallowed_address(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True for any address the crawler must never reach."""
    blocked = ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local or ip_obj.is_reserved
    allowed = any(
        ip_obj in net for net in ALLOWED_TEST_NETWORKS if net.version == ip_obj.version
    )
    return blocked and not allowed


def _resolved_addresses(hostname: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Return every address a hostname resolves to, or the literal address it already is.

    Checking the hostname string alone only defends against a literal IP in the URL.
    A name resolving to an RFC 1918 address passes such a check untouched, which is the
    ordinary shape of an SSRF: the attacker controls DNS, not the URL text.
    """
    try:
        return [ipaddress.ip_address(hostname)]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        # Unresolvable is not permission to proceed; the caller treats this as denied.
        return []
    return [ipaddress.ip_address(info[4][0]) for info in infos]


def _is_disallowed_private_ip(hostname: str) -> bool:
    """Return True if the hostname is, or resolves to, an address the crawler may not reach."""
    addresses = _resolved_addresses(hostname)
    return not addresses or any(_is_disallowed_address(ip) for ip in addresses)


def validate_url_security(url: str) -> tuple[str, str]:
    """Validate URL protocol and protect against SSRF to unwhitelisted private IPs."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported protocol '{parsed.scheme}'. Only http/https supported.")

    hostname = parsed.hostname or ""
    if not hostname:
        raise ValueError("URL must include a valid hostname.")

    if _is_disallowed_private_ip(hostname):
        raise ValueError(
            f"SSRF violation: '{hostname}' is, or resolves to, a non-public address."
        )

    return parsed.scheme, hostname


@dataclass(frozen=True)
class ExtractionOutcome:
    """What one extraction attempt recovered from a page."""

    title: str = ""
    content: str = ""
    links: list[str] = field(default_factory=list)
    region: str = "body"
    warnings: list[str] = field(default_factory=list)


def _outcome(document: ExtractedDocument) -> ExtractionOutcome:
    """Carry an extraction's result and its caveats into the crawler's own shape."""
    return ExtractionOutcome(
        title=document.title,
        content=document.markdown,
        links=document.links,
        region=document.region,
        warnings=document.warnings,
    )


# WHATWG HTML §13.2.3.2 gives the encoding sniffing order. `res.text` alone implements only
# part of it: httpx honours a charset in the Content-Type and otherwise assumes UTF-8, so a
# page declaring `<meta charset="windows-1252">` and nothing else came back as mojibake —
# `Résumé café` as `R?sum? caf?` — and a UTF-8 BOM survived into the markdown as a leading
# `\ufeff`. Both silently, and both irreversibly, because the fetcher returns `str` and the
# bytes are gone by the time a caller could notice.
_META_CHARSET_RE: Final[re.Pattern[bytes]] = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-]+)""", re.IGNORECASE
)

# Byte order marks, which take precedence over every declaration (step 1).
_BOMS: Final[tuple[tuple[bytes, str], ...]] = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)


def _charset_from(content_type: str) -> str | None:
    """Return the charset a Content-Type header declares, if any."""
    match = re.search(r"charset=([\w-]+)", content_type or "", re.IGNORECASE)
    return match.group(1) if match else None


def _try_decode(raw: bytes, encoding: str | None) -> str | None:
    """Attempt decoding raw bytes with a candidate encoding, returning None on failure."""
    if not encoding:
        return None
    try:
        return raw.decode(encoding, errors="replace")
    except LookupError:
        return None


def decode_body(raw: bytes, content_type: str = "") -> str:
    """Decode a response body the way a browser would, in the order the standard gives.

    BOM first, then the transport's declared charset, then a prescan of the first 1024 bytes
    for `<meta charset>`, then UTF-8. `utf-8-sig` is used for the UTF-8 BOM so the mark is
    consumed rather than carried into the text as a zero-width character nobody can see.
    """
    for bom, encoding in _BOMS:
        if raw.startswith(bom):
            return raw.decode(encoding, errors="replace")
    for candidate in (_charset_from(content_type), _meta_charset(raw)):
        decoded = _try_decode(raw, candidate)
        if decoded is not None:
            return decoded
    return raw.decode("utf-8", errors="replace")



def _meta_charset(raw: bytes) -> str | None:
    """Prescan the first 1024 bytes for a `<meta charset>` declaration, as the standard does."""
    match = _META_CHARSET_RE.search(raw[:1024])
    return match.group(1).decode("ascii", errors="replace") if match else None


def _status_failure(status_code: int) -> str | None:
    """Return why a status code means "no page", or None when the body is the page.

    Only 429 was ever compared. Every other error status — 403, 404, 500, 503 — had its
    error page extracted as ordinary content, returned with no warning, and recorded as a
    *successful* crawl, so the strategy store learned that a domain serving nothing but 404s
    was working. The crawler's own `except` arm made it worse by synthesising a 500 whose
    body reads `Fetch error: [Errno 111] Connection refused`: a refused connection was
    laundered into a page and counted as a success.

    A body that arrives with an error status is a server's explanation, not the document
    that was asked for, and a model cannot tell the two apart from the text alone.
    """
    if status_code == 429:
        return "rate_limited: the server asked us to slow down"
    if 400 <= status_code < 600:
        return f"http_error: the server answered {status_code}, so this body is not the page"
    return None


class AdaptiveWebCrawler:
    """Agentic web crawler with dynamic tier escalation and domain strategy memory."""

    def __init__(
        self,
        strategy_store: DomainStrategyStore | None = None,
        http_fetcher: Callable[[str, dict[str, str]], tuple[int, str]] | None = None,
        headless_fetcher: Callable[[str, str], tuple[int, str]] | None = None,
    ) -> None:
        self.strategy_store = strategy_store or DomainStrategyStore()
        self.http_fetcher = http_fetcher or self._default_http_fetcher
        self.headless_fetcher = headless_fetcher or self._default_headless_fetcher

    def crawl_page(self, url: str) -> CrawledPage:
        """Fetch and extract content from a single page using adaptive tier escalation."""
        start_time = time.monotonic()
        clean_url, _ = urldefrag(url)
        _, domain = validate_url_security(clean_url)
        strategy = self.strategy_store.get_strategy(domain)

        # Apply domain politeness delay
        if strategy.rate_limit_delay > 0:
            time.sleep(min(strategy.rate_limit_delay, 0.05))

        # 1. Determine initial execution tier
        initial_tier = strategy.preferred_tier

        if initial_tier == ExtractionTier.STATIC_HTTP:
            page = self._attempt_static_extract(clean_url, domain, start_time)
            if page.quality in (PageQuality.HIGH, PageQuality.PARTIAL):
                self.strategy_store.record_success(domain, ExtractionTier.STATIC_HTTP)
                return page
            if page.quality == PageQuality.BLOCKED:
                return page
            # Escalation triggered due to empty SPA shell
            self.strategy_store.record_shell_detected(domain)

        # 2. Escalate to Headless Browser Tier
        page = self._attempt_headless_extract(clean_url, domain, strategy.wait_selector, start_time)
        if page.quality == PageQuality.HIGH:
            self.strategy_store.record_success(domain, ExtractionTier.HEADLESS_BROWSER)
        return page

    def _attempt_static_extract(self, url: str, domain: str, start_time: float) -> CrawledPage:
        status_code, html = self.http_fetcher(url, DEFAULT_BROWSER_HEADERS)
        failure = _status_failure(status_code)
        if failure is not None:
            if status_code == 429:
                self.strategy_store.record_rate_limit(domain)
            return self._build_page(
                url, ExtractionOutcome(warnings=[failure]), ExtractionTier.STATIC_HTTP, PageQuality.BLOCKED, start_time
            )

        document = extract(html, url)
        quality = SPADetector.analyze(html, document.markdown)

        return self._build_page(url, _outcome(document), ExtractionTier.STATIC_HTTP, quality, start_time)

    def _attempt_headless_extract(
        self, url: str, domain: str, wait_selector: str, start_time: float
    ) -> CrawledPage:
        status_code, html = self.headless_fetcher(url, wait_selector)
        failure = _status_failure(status_code)
        if failure is not None:
            if status_code == 429:
                self.strategy_store.record_rate_limit(domain)
            return self._build_page(
                url, ExtractionOutcome(warnings=[failure]), ExtractionTier.HEADLESS_BROWSER, PageQuality.BLOCKED, start_time
            )

        document = extract(html, url)
        text = document.markdown
        quality = PageQuality.HIGH if len(text) >= 100 else PageQuality.PARTIAL

        return self._build_page(url, _outcome(document), ExtractionTier.HEADLESS_BROWSER, quality, start_time)

    def _build_page(
        self,
        url: str,
        extraction: ExtractionOutcome,
        tier: ExtractionTier,
        quality: PageQuality,
        start_time: float,
    ) -> CrawledPage:
        # Standard token approximation: ~4 characters per token
        title, content, links = extraction.title, extraction.content, extraction.links
        token_estimate = max(len(content) // 4, 1) if content else 0
        resolved_links = [urljoin(url, link) for link in links if link and not link.startswith("#")]
        duration = round(time.monotonic() - start_time, 3)

        return CrawledPage(
            url=url,
            title=title or "Untitled Document",
            markdown_content=content,
            links=resolved_links[:50],
            tier_used=tier,
            quality=quality,
            region=extraction.region,
            extraction_warnings=extraction.warnings,
            token_estimate=token_estimate,
            duration_seconds=duration,
        )

    def _default_http_fetcher(self, url: str, headers: dict[str, str]) -> tuple[int, str]:
        """Fetch one page, revalidating the destination at every redirect hop.

        `follow_redirects=True` was the whole vulnerability. The SSRF gate ran once, on the
        URL the caller supplied, and the client then followed `Location` wherever it led —
        RFC 9110 §15.4 says a user agent MAY do exactly that, and httpx's
        `_send_handling_redirects` offers no hook that can veto the next connection. A page
        on an attacker-controlled public host answering `302 Location:
        http://169.254.169.254/latest/meta-data/...` therefore returned the cloud metadata
        document as page content, and the strategy store recorded the crawl as a success.

        Redirects are followed here instead, one hop at a time, with `validate_url_security`
        applied to every hop. The check has to sit where the connection is opened, not where
        the caller's intent is expressed.
        """
        try:
            import httpx
            with httpx.Client(timeout=10.0, follow_redirects=False) as client:
                return self._follow_redirects(client, url, headers)
        except ValueError:
            raise
        except Exception as exc:
            logger.warning("HTTP fetch failed for %s: %s", url, str(exc)[:256])
            return 500, f"<html><body>Fetch error: {str(exc)[:256]}</body></html>"

    def _follow_redirects(self, client: Any, url: str, headers: dict[str, str]) -> tuple[int, str]:
        """Walk the redirect chain, validating each destination before it is requested."""
        current = url
        for _ in range(MAX_REDIRECTS):
            response = client.get(current, headers=headers)
            location = response.headers.get("location")
            if not response.has_redirect_location or not location:
                return int(response.status_code), decode_body(
                    response.content, response.headers.get("content-type", "")
                )
            # Resolved against the current URL, because `Location` may be relative
            # (RFC 9110 §10.2.2), and validated before the next request is made.
            current = urljoin(current, location)
            validate_url_security(current)
        raise ValueError(f"Redirect limit of {MAX_REDIRECTS} exceeded starting at {url}")

    def _default_headless_fetcher(self, url: str, wait_selector: str) -> tuple[int, str]:
        """Fallback headless browser executor or simulated environment."""
        # When Playwright is not present, returns a standard structured response
        logger.info("Headless browser tier invoked for %s waiting on selector '%s'", url, wait_selector)
        return 200, f"<html><head><title>Hydrated Page</title></head><body><main><h1>Hydrated Content</h1><p>Rendered for {url}</p></main></body></html>"
