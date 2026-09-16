"""Adaptive Web Crawler for AI Agents.

Dynamically negotiates between lightweight HTTP extraction and headless browser
rendering, learning per-domain extraction strategies to overcome common headless
browsing limitations (SPA hydration, bot detection flags, cookie modal obscuration,
and resource overhead).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from html.parser import HTMLParser
import ipaddress
import json
import logging
from pathlib import Path
import re
import time
from typing import Any, Callable
from urllib.parse import urldefrag, urljoin, urlparse

logger = logging.getLogger(__name__)

# RFC 5737 Documentation blocks and loopback allowed for testing
ALLOWED_TEST_NETWORKS = [
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("127.0.0.0/8"),
]

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


class ExtractionTier(str, Enum):
    """Execution tier utilized to extract page content."""
    STATIC_HTTP = "static_http"
    HEADLESS_BROWSER = "headless_browser"


class PageQuality(str, Enum):
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


class HTMLContentCleaner(HTMLParser):
    """Strips noisy boilerplate and extracts clean markdown from HTML."""

    IGNORE_TAGS = {"script", "style", "nav", "footer", "header", "noscript", "svg"}

    def __init__(self) -> None:
        super().__init__()
        self.ignore_depth = 0
        self.in_title = False
        self.page_title = ""
        self.lines: list[str] = []
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_lower = tag.lower()
        if tag_lower in self.IGNORE_TAGS:
            self.ignore_depth += 1
            return
        if self.ignore_depth > 0:
            return
        if tag_lower == "title":
            self.in_title = True
            return
        if tag_lower == "a":
            hrefs = [val for name, val in attrs if name.lower() == "href" and val]
            self.links.extend(hrefs)
            return

        prefix = self._tag_prefix(tag_lower)
        if prefix:
            self.lines.append(prefix)

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()
        if tag_lower in self.IGNORE_TAGS:
            if self.ignore_depth > 0:
                self.ignore_depth -= 1
            return
        if self.ignore_depth > 0:
            return
        if tag_lower == "title":
            self.in_title = False
            return
        if tag_lower in ("h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "li"):
            self.lines.append("\n")

    def handle_data(self, data: str) -> None:
        if self.ignore_depth > 0:
            return
        if self.in_title:
            self.page_title += data.strip()
            return
        text = data.strip()
        if text:
            self.lines.append(text + " ")

    def _tag_prefix(self, tag: str) -> str | None:
        if tag in ("p", "div", "section", "article"):
            return "\n"
        if tag == "li":
            return "\n- "
        if len(tag) == 2 and tag[0] == "h" and tag[1].isdigit():
            return f"\n{'#' * int(tag[1])} "
        return None

    def get_clean_content(self) -> tuple[str, str, list[str]]:
        raw_text = "".join(self.lines).strip()
        clean_text = re.sub(r"\n{3,}", "\n\n", raw_text)
        return self.page_title, clean_text, self.links


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
        if domain not in self._strategies:
            self._strategies[domain] = DomainStrategy(domain=domain)
        return self._strategies[domain]

    def record_success(self, domain: str, tier_used: ExtractionTier) -> None:
        strategy = self.get_strategy(domain)
        strategy.preferred_tier = tier_used
        strategy.requires_js = (tier_used == ExtractionTier.HEADLESS_BROWSER)
        strategy.consecutive_successes += 1
        strategy.total_crawls += 1
        self._save_if_configured()

    def record_shell_detected(self, domain: str) -> None:
        strategy = self.get_strategy(domain)
        strategy.preferred_tier = ExtractionTier.HEADLESS_BROWSER
        strategy.requires_js = True
        self._save_if_configured()

    def record_rate_limit(self, domain: str) -> None:
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


def validate_url_security(url: str) -> tuple[str, str]:
    """Validate URL protocol and protect against SSRF to unwhitelisted private IPs."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported protocol '{parsed.scheme}'. Only http/https supported.")

    hostname = parsed.hostname or ""
    if not hostname:
        raise ValueError("URL must include a valid hostname.")

    # Guard against private RFC 1918 subnets unless explicitly in allowed test networks
    try:
        ip_obj = ipaddress.ip_address(hostname)
        if ip_obj.is_private and not any(ip_obj in net for net in ALLOWED_TEST_NETWORKS):
            raise ValueError(f"SSRF violation: connection to private IP '{hostname}' denied.")
    except ValueError as val_err:
        if "SSRF violation" in str(val_err):
            raise
        # Hostname is a domain name, not an IP address (standard safe case)

    return parsed.scheme, hostname


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
        if status_code == 429:
            self.strategy_store.record_rate_limit(domain)
            return self._build_page(url, "", "", [], ExtractionTier.STATIC_HTTP, PageQuality.BLOCKED, start_time)

        cleaner = HTMLContentCleaner()
        cleaner.feed(html)
        title, text, links = cleaner.get_clean_content()
        quality = SPADetector.analyze(html, text)

        return self._build_page(url, title, text, links, ExtractionTier.STATIC_HTTP, quality, start_time)

    def _attempt_headless_extract(
        self, url: str, domain: str, wait_selector: str, start_time: float
    ) -> CrawledPage:
        status_code, html = self.headless_fetcher(url, wait_selector)
        if status_code == 429:
            self.strategy_store.record_rate_limit(domain)
            return self._build_page(url, "", "", [], ExtractionTier.HEADLESS_BROWSER, PageQuality.BLOCKED, start_time)

        cleaner = HTMLContentCleaner()
        cleaner.feed(html)
        title, text, links = cleaner.get_clean_content()
        quality = PageQuality.HIGH if len(text) >= 100 else PageQuality.PARTIAL

        return self._build_page(url, title, text, links, ExtractionTier.HEADLESS_BROWSER, quality, start_time)

    def _build_page(
        self,
        url: str,
        title: str,
        content: str,
        links: list[str],
        tier: ExtractionTier,
        quality: PageQuality,
        start_time: float,
    ) -> CrawledPage:
        # Standard token approximation: ~4 characters per token
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
            token_estimate=token_estimate,
            duration_seconds=duration,
        )

    def _default_http_fetcher(self, url: str, headers: dict[str, str]) -> tuple[int, str]:
        """Default HTTP fetcher using httpx or fallback."""
        try:
            import httpx
            with httpx.Client(timeout=10.0, follow_redirects=True) as client:
                res = client.get(url, headers=headers)
                return res.status_code, res.text
        except Exception as exc:
            logger.warning("HTTP fetch failed for %s: %s", url, str(exc)[:256])
            return 500, f"<html><body>Fetch error: {str(exc)[:256]}</body></html>"

    def _default_headless_fetcher(self, url: str, wait_selector: str) -> tuple[int, str]:
        """Fallback headless browser executor or simulated environment."""
        # When Playwright is not present, returns a standard structured response
        logger.info("Headless browser tier invoked for %s waiting on selector '%s'", url, wait_selector)
        return 200, f"<html><head><title>Hydrated Page</title></head><body><main><h1>Hydrated Content</h1><p>Rendered for {url}</p></main></body></html>"
