"""Unit tests for the Adaptive Web Crawler reference application."""

# sentinel: allow[ZeroTrustSanitization] — fixture URLs exercising the crawler's private-network egress guard

from pathlib import Path

import crawler
import pytest
from crawler import (
    AdaptiveWebCrawler,
    DomainStrategyStore,
    ExtractionTier,
    PageQuality,
    SPADetector,
    validate_url_security,
)
from markdown_extract import extract


def test_clean_content_extractor_markdown() -> None:
    """The same expectations, now met by `markdown_extract` rather than a local cleaner.

    The nav link is the one that changed meaning: it used to be collected into the link
    list whether or not its text survived, and chrome links now do not reach the output at
    all — a menu repeated on every page of a site is the same tokens in every document.
    """
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
    document = extract(sample_html, "https://example.com/")
    title, content, links = document.title, document.markdown, document.links

    assert title == "Documentation Guide"
    assert "http://example.com/guide" in links
    assert "/home" not in " ".join(links)

    expected_present = ["Getting Started", "Installation", "- Run pip install framework"]
    expected_absent = ["Header Title", "Copyright 2026", "analytics", "Docs"]
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
    assert (scheme, host) == ("http", "192.0.2.1")

    _scheme, host = validate_url_security("http://example.com/guide")
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


def test_hostname_resolving_to_private_address_is_denied(monkeypatch):
    """The ordinary shape of an SSRF: the attacker controls DNS, not the URL text."""
    import socket as socket_module

    def _fake_getaddrinfo(host, *args, **kwargs):
        return [(socket_module.AF_INET, None, None, "", ("10.0.0.5", 0))]

    monkeypatch.setattr(crawler.socket, "getaddrinfo", _fake_getaddrinfo)
    with pytest.raises(ValueError, match="SSRF violation"):
        crawler.validate_url_security("http://internal-service.example.com/admin")


def test_unresolvable_hostname_is_denied_not_allowed(monkeypatch):
    """Failure to resolve is not permission to proceed."""

    def _raise(host, *args, **kwargs):
        raise OSError("name resolution failed")

    monkeypatch.setattr(crawler.socket, "getaddrinfo", _raise)
    with pytest.raises(ValueError, match="SSRF violation"):
        crawler.validate_url_security("http://nonexistent.example.com/")


def test_cloud_metadata_endpoint_is_denied():
    """Link-local is the single highest-value SSRF target in any cloud environment."""
    with pytest.raises(ValueError, match="SSRF violation"):
        crawler.validate_url_security("http://169.254.169.254/latest/meta-data/")


def test_loopback_is_reachable_in_both_address_families(monkeypatch):
    """An IPv4-only allowlist silently denies localhost once resolution is performed."""
    import socket as socket_module

    def _dual_stack(host, *args, **kwargs):
        return [
            (socket_module.AF_INET6, None, None, "", ("::1", 0, 0, 0)),
            (socket_module.AF_INET, None, None, "", ("127.0.0.1", 0)),
        ]

    monkeypatch.setattr(crawler.socket, "getaddrinfo", _dual_stack)
    assert crawler.validate_url_security("http://localhost:8080/")[1] == "localhost"


# --- SSRF across redirects -----------------------------------------------------------------


def _redirect_client(handler: object) -> object:
    """Return an httpx.Client subclass bound to a mock transport."""
    import httpx

    class Bound(httpx.Client):
        def __init__(self, *a: object, **k: object) -> None:
            k["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **k)

    return Bound


def test_a_redirect_to_a_private_address_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate ran once, on the URL the caller supplied, and the client followed Location.

    RFC 9110 §15.4 permits a user agent to follow a redirect automatically, and httpx offers
    no hook that can veto the next connection — so a page on an attacker-controlled public
    host answering `302 Location: http://169.254.169.254/...` returned the cloud metadata
    document as page content, and the strategy store recorded the crawl as a success.
    """
    import httpx

    meta = "http://169.254.169.254/latest/meta-data/iam/security-credentials/"
    requested: list[str] = []

    def handler(request: object) -> object:
        requested.append(str(request.url))
        if "169.254" in str(request.url):
            return httpx.Response(200, text="<html><body><main><p>SECRET</p></main></body></html>")
        return httpx.Response(302, headers={"Location": meta})

    monkeypatch.setattr(httpx, "Client", _redirect_client(handler))
    with pytest.raises(ValueError, match="SSRF violation"):
        crawler.AdaptiveWebCrawler().crawl_page("http://example.com/doc")
    assert requested == ["http://example.com/doc"]


def test_an_ordinary_redirect_is_still_followed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Revalidating every hop must not stop the crawler following legitimate redirects."""
    import httpx

    def handler(request: object) -> object:
        if str(request.url).endswith("/moved"):
            return httpx.Response(301, headers={"Location": "http://example.com/final"})
        return httpx.Response(200, text="<html><body><main><p>Arrived.</p></main></body></html>")

    monkeypatch.setattr(httpx, "Client", _redirect_client(handler))
    page = crawler.AdaptiveWebCrawler().crawl_page("http://example.com/moved")
    assert "Arrived." in page.markdown_content


def test_a_redirect_loop_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Following hops by hand means the ceiling is ours to impose; RFC 9110 sets none."""
    import httpx

    def handler(request: object) -> object:
        return httpx.Response(302, headers={"Location": "http://example.com/again"})

    monkeypatch.setattr(httpx, "Client", _redirect_client(handler))
    with pytest.raises(ValueError, match="Redirect limit"):
        crawler.AdaptiveWebCrawler().crawl_page("http://example.com/start")


class _FakeResponse:
    """The three attributes `_follow_redirects` reads, and nothing else."""

    def __init__(self, status: int, location: str | None = None, text: str = "") -> None:
        self.status_code = status
        self.text = text
        self.headers = {"location": location} if location else {}
        self.has_redirect_location = location is not None


class _FakeClient:
    """A client answering from a scripted table, so this test needs no HTTP library."""

    def __init__(self, script: dict[str, object]) -> None:
        self.script = script
        self.requested: list[str] = []

    def get(self, url: str, headers: dict[str, str] | None = None) -> object:
        """Record the request and answer from the script."""
        self.requested.append(url)
        return self.script[url]


def test_the_redirect_walk_validates_each_hop_without_an_http_library() -> None:
    """The same invariant as the end-to-end tests, with no dependency at all.

    Those need a real transport to answer a 302 and are only as available as `httpx`. This
    one uses the seam `_follow_redirects` already exposes, so the rule stays covered on any
    interpreter — a security regression that can skip is a security regression that will.
    """
    meta = "http://169.254.169.254/latest/meta-data/"
    client = _FakeClient({
        "http://example.com/a": _FakeResponse(302, location=meta),
        meta: _FakeResponse(200, text="SECRET"),
    })
    with pytest.raises(ValueError, match="SSRF violation"):
        crawler.AdaptiveWebCrawler()._follow_redirects(client, "http://example.com/a", {})
    assert client.requested == ["http://example.com/a"]


def test_a_relative_location_is_resolved_before_it_is_validated() -> None:
    """RFC 9110 §10.2.2 permits a relative Location, and a bare path validates against nothing."""
    client = _FakeClient({
        "http://example.com/a": _FakeResponse(302, location="/b"),
        "http://example.com/b": _FakeResponse(200, text="ok"),
    })
    status, text = crawler.AdaptiveWebCrawler()._follow_redirects(client, "http://example.com/a", {})
    assert (status, text, client.requested[-1]) == (200, "ok", "http://example.com/b")
