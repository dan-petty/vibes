# Adaptive Web Crawler Reference Application (`examples/adaptive-web-crawler/`)

A production-grade, two-tier adaptive web crawler and documentation extractor for AI agents that dynamically navigates around the limitations of headless browsing.

---

## 🎯 The Core Problem: Headless Browsing Limitations

AI agents frequently need to fetch external technical documentation, API specifications, and code examples. In practice, agents encounter five pervasive failure modes:

1. **The SPA Empty Shell Trap**:
   Modern documentation engines (Docusaurus, Next.js, GitBook, VitePress) serve minimal static HTML shells (`<div id="root"></div>` or `<div id="__next"></div>`). Simple HTTP scrapers capture only unhydrated loader scripts.
2. **Headless Browser Resource Bloat**:
   Spawning a full Chromium instance for every simple static page introduces 2–5 seconds of startup latency, high memory consumption, and potential zombie process leaks.
3. **Hydration Race Conditions & Incomplete Renders**:
   Headless browsers often trigger content extraction before asynchronous client-side API calls or React component hydration completes, resulting in partial text.
4. **Overlay Pollution (Cookie Banners & Modals)**:
   Cookie consent overlays, promotional dialogs, and sticky sidebars pollute the extracted text, wasting context window tokens on boilerplate.
5. **Agent Amnesia (Zero Learning Across Crawls)**:
   Traditional agents treat every URL independently, repeatedly attempting failing static HTTP requests before falling back, rather than remembering domain capabilities.

---

## 🏗️ Architecture: Two-Tier Adaptive Pipeline

The **Adaptive Web Crawler** resolves these issues with a hybrid execution engine and an **Epistemic Strategy Store**:

```mermaid
flowchart TD
    URL[Target URL] --> SecCheck[SSRF Security Gate: Block RFC 1918]
    SecCheck --> StratLookup{Domain Strategy Store<br/>Known Strategy?}
    
    StratLookup -->|Requires JS / Headless| Tier2[Tier-2: Headless Browser Driver]
    StratLookup -->|Unknown or Static| Tier1[Tier-1: Fast HTTP + Authentic Headers]
    
    Tier1 --> SPADetect{SPA Shell Detector<br/>Empty or Hydrated?}
    SPADetect -->|Rich Content| Clean1[HTML Content Cleaner: Strip Nav & Scripts]
    SPADetect -->|Empty Shell / JS Required| Escalate[Record Strategy: Escalate to Tier-2]
    
    Escalate --> Tier2
    Tier2 --> WaitHydration[Wait for Hydration Selector / Network Idle]
    WaitHydration --> Clean2[HTML Content Cleaner: Strip Modals & Boilerplate]
    
    Clean1 --> RecordSuccess[Update Strategy Store: Success Rate]
    Clean2 --> RecordSuccess
    RecordSuccess --> Output[Clean Markdown + Token Estimates]
```

---

## 🔑 Key Engineering Capabilities

1. **Two-Tier Dynamic Escalation**:
   - **Tier 1 (Fast HTTP)**: Employs realistic browser headers (`Sec-CH-UA`, `Accept-Language`, modern `User-Agent`) to fetch pages in <100ms.
   - **Tier 2 (Headless Browser)**: Invoked only when `SPADetector` identifies an unhydrated shell, saving up to 80% CPU and memory across mixed documentation sites.
2. **Domain Strategy Learning (`DomainStrategyStore`)**:
   - Remembers whether a domain (e.g. `docs.python.org` vs. client-rendered apps) requires JavaScript rendering.
   - Subsequent crawls to the same domain automatically bypass Tier 1 and start immediately on the optimal tier.
   - Persists learned strategies to JSON across agent sessions.
3. **Boilerplate & Modal Stripping (`HTMLContentCleaner`)**:
   - Automatically drops `<script>`, `<style>`, `<nav>`, `<header>`, `<footer>`, `<svg>`, and modal dialogs.
   - Formats headings (`#`, `##`), bullet lists, and paragraphs into compact markdown, eliminating token bloat.
4. **Politeness & Rate-Limit Backoff**:
   - Detects HTTP 429 (`Too Many Requests`) and doubles the per-domain delay with exponential backoff.
5. **Zero-Trust Egress Defense (SSRF Mitigation)**:
   - Validates all destination URLs against private RFC 1918 IP addresses (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`) and cloud metadata endpoints (`169.254.169.254`).

---

## 🚀 Running the Automated Test Suite

The reference app includes comprehensive unit tests verifying tier escalation, strategy learning, rate limiting, and sanitization:

```bash
python3 -m pytest examples/adaptive-web-crawler/test_crawler.py -v
```

---

## 💻 Programmatic Usage

```python
from pathlib import Path
from crawler import AdaptiveWebCrawler, DomainStrategyStore

# Initialize crawler with persistent domain strategy memory
store = DomainStrategyStore(persistence_path=Path(".data/crawler_strategies.json"))
crawler = AdaptiveWebCrawler(strategy_store=store)

# First crawl to an SPA domain: automatically detects shell and escalates
page = crawler.crawl_page("http://example.com/docs")
print(f"Title: {page.title}")
print(f"Tier Used: {page.tier_used.value}")  # "headless_browser"
print(f"Tokens: ~{page.token_estimate}")

# Second crawl to the same domain: starts directly on Tier 2 with zero wasted latency!
page2 = crawler.crawl_page("http://example.com/api-reference")
print(f"Tier Used: {page2.tier_used.value}")  # "headless_browser" (instant dispatch)
```
