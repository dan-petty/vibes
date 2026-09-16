"""Unit tests for the Adaptive Web Crawler reference application."""

from pathlib import Path
import pytest
from crawler import (
    AdaptiveWebCrawler,
    DomainStrategyStore,
    ExtractionTier,
    HTMLContentCleaner,
    PageQuality,
    SPADetector,
    validate_url_security,
)


def test_clean_content_extractor_markdown() -> None:
    """Ensure HTMLContentCleaner strips boilerplate and formats clean markdown."""
    sample_html = """
    <!DOCTYPE html>
    <html>
    <head><title>Documentation Guide</title></head>
    <body>
        <nav><a href="/home">Home</a><a href="/docs">Docs</a></nav>
        <header><h1>Header Title</h1></header>
        <main>
            <h1>Getting Started</h1>
            <p>Welcome to the framework documentation.</p>
            <h2>Installation</h2>
            <ul>
                <li>Run pip install framework</li>
                <li>Verify version</li>
            </ul>
            <a href="http://example.com/guide">Next Steps</a>
        </main>
        <footer><p>Copyright 2026</p></footer>
        <script>console.log("analytics");</script>
    </body>
    </html>
    """
    cleaner = HTMLContentCleaner()
    cleaner.feed(sample_html)
    title, content, links = cleaner.get_clean_content()

    assert title == "Documentation Guide"
    assert "http://example.com/guide" in links

    expected_present = ["Getting Started", "Installation", "- Run pip install framework"]
    expected_absent = ["Header Title", "Copyright 2026", "analytics"]
    assert all(item in content for item in expected_present)
    assert not any(item in content for item in expected_absent)


def test_spa_detector_identifies_empty_shell() -> None:
    """Ensure SPADetector flags unhydrated SPA shells with minimal text."""
    empty_spa_html = """
    <html>
    <head><title>App</title><script src="/bundle1.js"></script><script src="/bundle2.js"></script></head>
    <body>
        <div id="root"></div>
        <script src="/runtime.js"></script>
        <script src="/main.js"></script>
        <noscript>You need to enable JavaScript to run this app.</noscript>
    </body>
    </html>
    """
    quality = SPADetector.analyze(empty_spa_html, "You need to enable JavaScript to run this app.")
    assert quality == PageQuality.EMPTY_SHELL


def test_spa_detector_identifies_rich_content() -> None:
    """Ensure SPADetector flags rich text documents as HIGH quality."""
    rich_html = "<html><body><article><h1>Architecture</h1><p>" + ("Solid documentation text. " * 30) + "</p></article></body></html>"
    quality = SPADetector.analyze(rich_html, "Solid documentation text. " * 30)
    assert quality == PageQuality.HIGH


def test_crawler_static_http_success() -> None:
    """Ensure crawler successfully extracts static page in Tier 1 without headless escalation."""
    static_html = """
    <html><head><title>Static Docs</title></head>
    <body><main><h1>Static Page</h1><p>""" + ("Rich text for static rendering. " * 20) + """</p></main></body></html>
    """
    headless_called = False

    def mock_http(url: str, headers: dict[str, str]) -> tuple[int, str]:
        return 200, static_html

    def mock_headless(url: str, selector: str) -> tuple[int, str]:
        nonlocal headless_called
        headless_called = True
        return 200, "<html><body>Hydrated</body></html>"

    crawler = AdaptiveWebCrawler(http_fetcher=mock_http, headless_fetcher=mock_headless)
    page = crawler.crawl_page("http://example.com/docs")

    assert page.tier_used == ExtractionTier.STATIC_HTTP
    assert page.quality == PageQuality.HIGH
    assert not headless_called
    assert "Static Page" in page.markdown_content


def test_crawler_escalates_to_headless_on_spa_shell() -> None:
    """Ensure crawler escalates to headless browser when static tier encounters an unhydrated SPA shell."""
    empty_spa_html = """
    <html><head><title>Client App</title></head>
    <body><div id="app"></div><script src="/a.js"></script><script src="/b.js"></script><script src="/c.js"></script><script src="/d.js"></script></body></html>
    """
    hydrated_html = """
    <html><head><title>Client App</title></head>
    <body><main><h1>Hydrated Dashboard</h1><p>""" + ("Fully hydrated dynamic content from API. " * 15) + """</p></main></body></html>
    """
    headless_invoked = False

    def mock_http(url: str, headers: dict[str, str]) -> tuple[int, str]:
        return 200, empty_spa_html

    def mock_headless(url: str, selector: str) -> tuple[int, str]:
        nonlocal headless_invoked
        headless_invoked = True
        return 200, hydrated_html

    store = DomainStrategyStore()
    crawler = AdaptiveWebCrawler(strategy_store=store, http_fetcher=mock_http, headless_fetcher=mock_headless)
    page = crawler.crawl_page("http://example.com/app")

    assert (headless_invoked, page.tier_used, page.quality) == (
        True,
        ExtractionTier.HEADLESS_BROWSER,
        PageQuality.HIGH,
    )
    assert "Hydrated Dashboard" in page.markdown_content

    strategy = store.get_strategy("example.com")
    assert (strategy.preferred_tier, strategy.requires_js) == (
        ExtractionTier.HEADLESS_BROWSER,
        True,
    )


def test_crawler_reuses_learned_headless_strategy() -> None:
    """Ensure subsequent crawls to a learned domain immediately start on headless tier."""
    hydrated_html = """
    <html><head><title>Second Page</title></head>
    <body><main><h1>Page Two</h1><p>""" + ("Rich text for page two. " * 20) + """</p></main></body></html>
    """
    http_called = False

    def mock_http(url: str, headers: dict[str, str]) -> tuple[int, str]:
        nonlocal http_called
        http_called = True
        return 200, "<html><body>empty</body></html>"

    def mock_headless(url: str, selector: str) -> tuple[int, str]:
        return 200, hydrated_html

    store = DomainStrategyStore()
    # Pre-record that example.com requires headless browser
    store.record_shell_detected("example.com")

    crawler = AdaptiveWebCrawler(strategy_store=store, http_fetcher=mock_http, headless_fetcher=mock_headless)
    page = crawler.crawl_page("http://example.com/page-two")

    # Static HTTP fetcher should have been completely bypassed!
    assert http_called is False
    assert page.tier_used == ExtractionTier.HEADLESS_BROWSER
    assert "Page Two" in page.markdown_content


def test_crawler_rate_limiting_backoff() -> None:
    """Ensure HTTP 429 increases rate limit delay for the domain."""
    def mock_http_429(url: str, headers: dict[str, str]) -> tuple[int, str]:
        return 429, "Too Many Requests"

    store = DomainStrategyStore()
    initial_delay = store.get_strategy("example.com").rate_limit_delay

    crawler = AdaptiveWebCrawler(strategy_store=store, http_fetcher=mock_http_429)
    page = crawler.crawl_page("http://example.com/throttled")

    assert page.quality == PageQuality.BLOCKED
    updated_delay = store.get_strategy("example.com").rate_limit_delay
    assert updated_delay > initial_delay


def test_security_ssrf_protection_blocks_rfc1918() -> None:
    """Ensure validate_url_security blocks private RFC 1918 IPs."""
    with pytest.raises(ValueError, match="SSRF violation"):
        validate_url_security("http://10.0.0.5/admin")

    with pytest.raises(ValueError, match="SSRF violation"):
        validate_url_security("http://172.16.50.1/status")

    # RFC 5737 and standard domain names pass validation
    scheme, host = validate_url_security("http://192.0.2.1/docs")
    assert host == "192.0.2.1"

    scheme, host = validate_url_security("http://example.com/guide")
    assert host == "example.com"


def test_domain_strategy_persistence(tmp_path: Path) -> None:
    """Ensure domain strategies persist across store instances via JSON."""
    json_file = tmp_path / "strategies.json"
    store1 = DomainStrategyStore(persistence_path=json_file)
    store1.record_shell_detected("example.com")
    store1.record_rate_limit("example.com")

    # Re-instantiate from disk
    store2 = DomainStrategyStore(persistence_path=json_file)
    strat = store2.get_strategy("example.com")
    assert strat.preferred_tier == ExtractionTier.HEADLESS_BROWSER
    assert strat.requires_js is True
    assert strat.rate_limit_delay > 0.5
