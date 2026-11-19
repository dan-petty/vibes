# Pattern: Adaptive Headless Web Crawling & Domain Strategy Memory

> **Pattern Class**: Agent Data Acquisition & External Grounding
> **Problem**: Static fetching silently returns empty SPA shells, and unconditional headless rendering costs seconds per page
> **Solution**: Two-tier escalation with persistent per-domain strategy memory, so each domain is learned once and paid for once
> **TLDR**: Extract dynamic web content via adaptive escalation to a headless browser, with per-domain strategy memory, DOM sanitization, and bounded HTTP timeouts.
> **ELI:7b**: A smart web crawler that loads modern JavaScript pages, cleans out junk ads and tracking scripts, and saves clean text safely without hanging.
> **Reference Implementation**: [`examples/adaptive-web-crawler/`](../examples/adaptive-web-crawler/)

A robust, two-tier web crawling and documentation extraction pattern for AI agents that mitigates headless browsing limitations through dynamic tier escalation, SPA shell detection, and persistent domain strategy memory.

---

## 1. Problem Statement

Autonomous AI coding agents frequently crawl external technical documentation, package indexes, and API references to ground their synthesis. However, web crawling in agentic workflows experiences five critical friction points:

1. **The SPA Empty Shell Problem**: Modern documentation portals (built with Docusaurus, Next.js, VitePress, or GitBook) deliver minimal static HTML shells (`<div id="root"></div>`). Fast HTTP GET scrapers capture unhydrated JavaScript loaders with zero usable text.
2. **Headless Browser Resource Bloat**: Spawning a full headless Chromium browser process for every simple static documentation page adds browser startup time and memory to every fetch.
3. **Hydration Race Conditions**: Headless browsers often trigger extraction before client-side component hydration or asynchronous API fetches complete, returning incomplete or truncated content.
4. **Overlay & Modal Clutter**: Cookie consent banners, GDPR dialogs, and newsletter overlays contaminate the extracted text, consuming precious context window tokens with non-semantic boilerplate.
5. **Agent Amnesia**: Without persistent domain memory, an agent repeats the same failing static HTTP request every time it visits a URL on the same domain, wasting time and API quotas.

---

## 2. Core Mechanics

The **Adaptive Headless Crawling** pattern solves these limitations through a three-stage adaptive pipeline:

```mermaid
flowchart TD
    URL[Agent Request: Target URL] --> SSRF[SSRF Security Filter: Block RFC 1918]
    SSRF --> MemoryCheck{Domain Strategy Store<br/>Learned Profile?}

    MemoryCheck -->|Learned: Requires JS| HeadlessTier[Tier-2: Headless Browser Driver]
    MemoryCheck -->|Unknown or Static| HTTPTier[Tier-1: Fast HTTP + Authentic Headers]

    HTTPTier --> SPATest{SPA Detector<br/>Empty Shell or Rich Text?}
    SPATest -->|Rich Semantic Content| ContentCleaner[HTML Content Cleaner]
    SPATest -->|Empty Shell / JS Needed| Escalate[Escalate: Switch to Headless Tier]

    Escalate --> HeadlessTier
    HeadlessTier --> HydrationWait[Wait for Content Selector / Network Idle]
    HydrationWait --> ContentCleaner

    ContentCleaner --> ModalStrip[Strip Cookie Banners, Nav, Header, Footer]
    ModalStrip --> MarkdownGen[Generate Clean Headings & Code Fences]
    MarkdownGen --> UpdateStore[Update Domain Strategy Store: Tier & Latency]
    UpdateStore --> Result[Return CrawledPage + Token Estimate]
```

### 1. Two-Tier Dynamic Escalation
- **Tier 1 (Fast HTTP)**: Injects authentic browser headers (`Sec-CH-UA`, `Accept-Language`, modern desktop `User-Agent`) and fetches the page with one `httpx` request (10 s timeout), checking each redirect hop.
- **SPA Detector**: Evaluates text length, script count, and common framework root elements (`#root`, `#app`, `#__next`). If an unhydrated shell is detected, execution immediately escalates to Tier 2.
- **Tier 2 (Headless Browser)**: Renders the page with a realistic viewport, waits for target content elements (`main`, `article`, `[role='main']`) or network idle, and executes hydration. In the reference implementation this tier is a stub: `_default_headless_fetcher` starts no browser and returns fixed HTML. A real driver (for example Playwright) must be passed in as `headless_fetcher`.

### 2. Epistemic Domain Strategy Memory (`DomainStrategyStore`)
- Stores per-domain operational parameters:
  - Optimal extraction tier (`STATIC_HTTP` vs. `HEADLESS_BROWSER`).
  - Target content and hydration selectors.
  - A politeness delay that doubles on each HTTP 429, up to 10 s (the example sleeps at most 0.05 s per request).
- On subsequent visits to the same domain, the crawler checks its memory and dispatches immediately to the optimal tier, eliminating wasted round-trips.

### 3. Noise & Modal Pruning (`markdown_extract.extract`)
- Automatically removes `<script>`, `<style>`, `<nav>`, `<header>`, `<footer>`, `<svg>`, `<dialog>`, forms and ARIA landmark chrome (`role="navigation"`, `"banner"`, `"contentinfo"` and similar). It does not remove `[role="dialog"]` or class-based banners such as `.cookie-banner`.
- Converts headings, lists, and paragraphs into compact GitHub-flavored markdown.

### 4. Zero-Trust Egress Defense (SSRF Mitigation)
- Rejects connection attempts to private RFC 1918 subnets (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`) and cloud metadata (`169.254.169.254`).

---

## 3. Implementation Blueprint

```python
from crawler import AdaptiveWebCrawler, DomainStrategyStore, ExtractionTier

# Instantiate crawler with persistent strategy store
store = DomainStrategyStore(persistence_path=Path(".data/crawler_strategies.json"))
crawler = AdaptiveWebCrawler(strategy_store=store)

# 1. First crawl: detects empty SPA shell and escalates to headless browser
page = crawler.crawl_page("http://example.com/docs")
assert page.tier_used == ExtractionTier.HEADLESS_BROWSER

# 2. Subsequent crawl to same domain: immediately uses headless browser
page2 = crawler.crawl_page("http://example.com/api-reference")
assert page2.tier_used == ExtractionTier.HEADLESS_BROWSER
```

---

## 4. Verifiable Impact & Key Takeaways

| Metric | Monolithic Headless Scraper | Naive Static Scraper | Adaptive Headless Crawler |
|---|---|---|---|
| **Static Doc Latency** | Browser startup on every page | One HTTP request | **One HTTP request (Tier 1)** |
| **SPA Content** | Rendered | Empty shells | **Rendered after escalation to Tier 2** (a stub in the reference implementation) |
| **Memory Footprint** | One browser process per page | No browser | **Browser only for escalated domains** |
| **Token Waste (Boilerplate)** | High (navbars/modals) | Low (often empty) | **Lower: chrome tags and landmarks removed** |
| **Cross-Turn Learning** | None (Amnesia) | None (Amnesia) | **Per-domain tier memory in a JSON file** |

These rows compare mechanisms, not measurements. The repository has no benchmark that measures latency, memory, or success rates for this crawler.
