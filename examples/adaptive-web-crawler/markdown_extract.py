#!/usr/bin/env python3
"""Turn a fetched page into text a model can be handed directly.

`crawl4ai` and `firecrawl` both answer one question this crawler could not: what comes out
is Markdown shaped for a prompt, not a flattened run of visible strings. The extractor this
replaces walked the document and appended text, which loses the three things that matter
most once the page is out of its browser.

**Code becomes prose.** `<pre><code>` was dropped like any other element, so a snippet
arrived as a single unindented line with no language and no fence. A model reading that
cannot tell code from commentary, and indentation is the whole meaning of some of it.

**Links become orphans.** Hrefs were collected into a separate list, so the text said
"see the guide" and the URL sat somewhere else with nothing joining them — and every
relative href stayed relative, which is a dead link the moment the text leaves the origin.

**Page content becomes document structure.** This is the one worth stating plainly. Output
"shaped for model consumption" is output that something downstream will *parse*, so text
lifted from a page is untrusted input to that parser. A page containing a line of backticks
closes a fence that was opened around it; a line beginning `#` becomes a heading in the
prompt; a line beginning `>` becomes a quote. It is the rule this repository already
applies to a workflow `run:` block and to a contract's rendered answers, arriving a third
time by a different route: **a value from outside is data, and turning it into syntax is
the whole of the vulnerability.**

Zero dependencies, per the rule for sample applications: `html.parser` and `urllib.parse`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, ClassVar, Final
from urllib.parse import urljoin

# Never emitted, and never recursed into. Their text is chrome: it repeats on every page of
# a site, so it inflates every document with the same tokens and tells a model nothing.
BOILERPLATE_TAGS: Final[frozenset[str]] = frozenset({
    "script", "style", "noscript", "svg", "canvas", "template", "iframe", "object", "embed",
    "nav", "header", "footer", "aside", "form", "button", "select", "dialog", "menu",
})

# ARIA landmarks that mean the same as the tags above. Marked-up chrome is still chrome,
# and a site that uses `<div role="navigation">` is not offering different content.
BOILERPLATE_ROLES: Final[frozenset[str]] = frozenset({
    "navigation", "banner", "contentinfo", "search", "complementary", "menubar", "toolbar",
})

# Where the substance usually is, most specific first. Which one matched is reported rather
# than assumed, because "the whole body" and "the article element" are different documents
# and a reader who cannot tell them apart cannot judge what is missing.
MAIN_REGIONS: Final[tuple[str, ...]] = ("main", "article")

HEADINGS: Final[dict[str, str]] = {f"h{level}": "#" * level for level in range(1, 7)}

_WHITESPACE: Final[re.Pattern[str]] = re.compile(r"[ \t\r\f\v]+")
_BLANK_RUNS: Final[re.Pattern[str]] = re.compile(r"\n{3,}")
_BACKTICK_RUN: Final[re.Pattern[str]] = re.compile(r"`+")
_LANGUAGE: Final[re.Pattern[str]] = re.compile(r"(?:language|lang|highlight)-([A-Za-z0-9+#-]+)")

# Escaped wherever they appear, because each one is inline Markdown syntax and none of them
# is rare in prose. Escaping only at line starts would leave `a * b` emphasising a sentence.
_INLINE_SPECIALS: Final[str] = "\\`*_[]<>"

# Only meaningful at the start of a line, so escaping them mid-sentence would fill ordinary
# text with backslashes for no gain.
_LEADING: Final[re.Pattern[str]] = re.compile(r"^(\s*)([#>|+=-]|\d+[.)])")

TRUNCATION_MARKER: Final[str] = "\n\n[truncated: the document continues beyond the budget]\n"


@dataclass
class ExtractedDocument:
    """One page, ready to put in a prompt, with what happened to it recorded."""

    url: str
    title: str
    markdown: str
    links: list[str] = field(default_factory=list)
    region: str = "body"
    warnings: list[str] = field(default_factory=list)
    truncated: bool = False

    def with_provenance(self) -> str:
        """Render the document under a header naming where it came from.

        A page dropped into a prompt with no attribution is a claim with no source, and the
        model has no way to qualify it. Two lines cost nothing and make the difference
        between context and hearsay.
        """
        header = [f"# {escape_inline(self.title)}" if self.title else "# Untitled",
                  "", f"Source: <{self.url}>", ""]
        return "\n".join(header) + self.markdown


def escape_inline(text: str) -> str:
    """Escape every character that is Markdown syntax inside a line."""
    for char in _INLINE_SPECIALS:
        text = text.replace(char, "\\" + char)
    return text


def escape_block(line: str) -> str:
    """Escape a leading character that would turn a line of page text into structure."""
    return _LEADING.sub(lambda match: f"{match.group(1)}\\{match.group(2)}", line)


def fence_for(code: str) -> str:
    """Return a fence longer than the longest backtick run the code contains.

    CommonMark closes a fenced block on the first run of at least the opening length, so a
    three-backtick fence around content containing three backticks ends early and the rest
    of the page becomes prose in the prompt. The content decides the fence, never the other
    way round.
    """
    longest = max((len(run) for run in _BACKTICK_RUN.findall(code)), default=0)
    return "`" * max(3, longest + 1)


@dataclass
class PageContext:
    """Where the page came from, and what the whole document accumulated.

    Split out because resolving a URL, remembering a title and noting which regions were
    seen are one responsibility — facts about the document — and keeping them on the parser
    is what made the parser a class with fourteen attributes.
    """

    base_url: str
    title: str = ""
    links: list[str] = field(default_factory=list)
    regions_seen: set[str] = field(default_factory=set)

    def resolve(self, href: str) -> str:
        """Resolve one href against the page's own base, which `<base>` may have moved."""
        return urljoin(self.base_url, href)

    def region(self) -> str:
        """Return the most specific main region the document turned out to have."""
        return next((name for name in MAIN_REGIONS if name in self.regions_seen), "body")


class ChromeFilter:
    """Tracks whether the walk is inside an element whose subtree is chrome.

    A stack of the tags that opened a skipped region, never a counter. A counter has to
    guess which end tag closes the region, and the first version decremented on every
    non-void end tag — so `</a>` inside a `<nav>` ended the skip and the rest of the
    navigation was emitted into the document as content. The stack pops only when the tag
    that opened the region closes, which is also correct for a `<nav>` inside a `<nav>`.
    """

    def __init__(self) -> None:
        """Start outside any chrome."""
        self.stack: list[str] = []
        self.dropped: set[str] = set()

    @property
    def active(self) -> bool:
        """Return True while the walk is inside a skipped subtree."""
        return bool(self.stack)

    def enter(self, name: str, attributes: dict[str, str], void: bool) -> bool:
        """Return True when this element is inside chrome, or opens some."""
        hidden = attributes.get("aria-hidden") == "true"
        chrome = name in BOILERPLATE_TAGS or attributes.get("role") in BOILERPLATE_ROLES or hidden
        if chrome and not void:
            self.stack.append(name)
            self.dropped.add(name)
            return True
        return self.active or chrome

    def leave(self, name: str) -> None:
        """Close the skipped region if this is the tag that opened it."""
        if self.stack and name == self.stack[-1]:
            self.stack.pop()


class BlockBuilder:
    """Assembles Markdown blocks, keeping page text and synthesised markup apart.

    Escaping at the point of collection cannot tell them apart afterwards: an emitted
    `![alt](src)` went back through the text path and came out as `!\\[alt\\](src)`, which
    renders as literal brackets. Escaping once, at flush, over the text parts only, cannot
    do that.
    """

    def __init__(self) -> None:
        """Start with an empty document and no block under construction."""
        self.blocks: list[str] = []
        self._fragments: list[tuple[bool, str]] = []
        self._prefix = ""
        self._code: list[str] | None = None
        self._language = ""

    def text(self, fragment: str) -> None:
        """Add page text, to be escaped when the block closes."""
        self._fragments.append((False, fragment))

    def markup(self, fragment: str) -> None:
        """Add Markdown this extractor produced, which must never be escaped."""
        self._fragments.append((True, fragment))

    def open(self, prefix: str) -> None:
        """Close the current block and start one carrying the given prefix."""
        self.flush()
        self._prefix = prefix

    def flush(self) -> None:
        """Close the block under construction, escaping the page's text and nothing else."""
        joined = "".join(
            fragment if is_markup else escape_inline(fragment)
            for is_markup, fragment in self._fragments
        ).strip()
        self._fragments = []
        prefix, self._prefix = self._prefix, ""
        if joined:
            self.blocks.append(prefix + escape_block(joined) if not prefix else prefix + joined)

    # --- Code ----------------------------------------------------------------------------

    @property
    def in_code(self) -> bool:
        """Return True while a `<pre>` is open, where nothing is escaped."""
        return self._code is not None

    def open_code(self) -> None:
        """Begin buffering a code block."""
        self._code = []
        self._language = ""

    def code_text(self, fragment: str) -> None:
        """Add code verbatim; a backslash inside a code block is part of the code."""
        if self._code is not None:
            self._code.append(fragment)

    def set_language(self, language: str) -> None:
        """Record the highlight language, when the document states one."""
        if self._code is not None and language:
            self._language = language

    def close_code(self) -> None:
        """Emit the buffered code as a fenced block whose fence the content chose."""
        if self._code is None:
            return
        code = "".join(self._code).strip("\n")
        self._code = None
        if not code:
            return
        self.flush()
        fence = fence_for(code)
        self.blocks.append(f"{fence}{self._language}\n{code}\n{fence}")

    def render(self) -> str:
        """Return the finished document."""
        self.close_code()
        self.flush()
        return _BLANK_RUNS.sub("\n\n", "\n\n".join(self.blocks).strip())


class ModelReadyExtractor(HTMLParser):
    """Walks a document once, delegating filtering, assembly and page facts."""

    # Elements with no end tag. A void element can never open a region to skip, and a
    # tracker that does not know this waits forever for a `</embed>` that never comes.
    VOID: ClassVar[frozenset[str]] = frozenset({"br", "hr", "img", "embed", "source", "track"})

    def __init__(self, base_url: str) -> None:
        """Prepare an extractor rooted at the page's own URL.

        Six attributes, not fourteen. The split is not cosmetic: the smell quantifier
        refused the single class at 27 methods and 14 attributes, and it was right — one
        object was parsing, filtering chrome, assembling blocks and accumulating document
        facts at once.
        """
        super().__init__(convert_charrefs=True)
        self.context = PageContext(base_url)
        self.chrome = ChromeFilter()
        self.builder = BlockBuilder()
        self.in_title = False
        self.list_depth = 0
        self.href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Dispatch one opening tag, after chrome and `<base>` have had their say."""
        name = tag.lower()
        attributes = {key.lower(): (value or "") for key, value in attrs}
        if self.chrome.enter(name, attributes, name in self.VOID):
            return
        if name == "base" and attributes.get("href"):
            self.context.base_url = self.context.resolve(attributes["href"])
            return
        if name in MAIN_REGIONS:
            self.context.regions_seen.add(name)
        handler = _STARTERS.get(name)
        if handler:
            handler(self, name, attributes)

    def handle_endtag(self, tag: str) -> None:
        """Close one element, or one skipped region."""
        name = tag.lower()
        if self.chrome.active:
            self.chrome.leave(name)
            return
        handler = _ENDERS.get(name)
        if handler:
            handler(self)

    def handle_data(self, data: str) -> None:
        """Route one text node to the title, to a code block, or to the current block."""
        if self.chrome.active:
            return
        if self.builder.in_code:
            self.builder.code_text(data)
            return
        if self.in_title:
            self.context.title += data.strip()
            return
        text = _WHITESPACE.sub(" ", data)
        if not text.strip():
            return
        self._emit_text(text)

    def _emit_text(self, text: str) -> None:
        """Emit page text, as a link when one is open and as plain text otherwise."""
        if self.href:
            self.context.links.append(self.href)
            self.builder.markup(f"[{escape_inline(text.strip())}]({self.href})")
            return
        self.builder.text(text)

    def document(self) -> ExtractedDocument:
        """Assemble the extracted document, naming anything that needs qualifying."""
        markdown = self.builder.render()
        region = self.context.region()
        warnings: list[str] = []
        if not markdown:
            # An empty string and a page with nothing to say are the same value, and the
            # difference decides whether a caller retries, renders, or gives up.
            warnings.append("no_content: the document yielded no text after boilerplate removal")
        if region == "body":
            warnings.append("no_main_region: no <main> or <article>; the whole body was taken")
        return ExtractedDocument(
            url=self.context.base_url,
            title=self.context.title.strip(),
            markdown=markdown,
            links=self.context.links,
            region=region,
            warnings=warnings,
        )


# --- Element handlers ---------------------------------------------------------------------
#
# Module-level functions rather than methods, and a table rather than a ladder. The ladder
# breached the sentinel's complexity ceiling; moving each branch to its own method breached
# the smell quantifier's method count. Handlers are not the class's interface, so they do
# not belong on it — the class parses and holds state, and these say what each tag means.


def _start_title(extractor: ModelReadyExtractor, _name: str, _attributes: dict[str, str]) -> None:
    """Begin collecting the document title."""
    extractor.in_title = True


def _start_pre(extractor: ModelReadyExtractor, _name: str, _attributes: dict[str, str]) -> None:
    """Begin buffering a code block, whose text is never escaped."""
    extractor.builder.open_code()


def _start_code(extractor: ModelReadyExtractor, _name: str, attributes: dict[str, str]) -> None:
    """Take the highlight language from `<code>` when it is inside a `<pre>`."""
    match = _LANGUAGE.search(attributes.get("class", ""))
    extractor.builder.set_language(match.group(1) if match else "")


def _start_anchor(extractor: ModelReadyExtractor, _name: str, attributes: dict[str, str]) -> None:
    """Resolve the href now, against the base, so the link survives leaving the origin."""
    href = attributes.get("href")
    extractor.href = extractor.context.resolve(href) if href else None


def _start_heading(extractor: ModelReadyExtractor, name: str, _attributes: dict[str, str]) -> None:
    """Start a heading block at the document's own level."""
    extractor.builder.open(HEADINGS[name] + " ")


def _start_list(extractor: ModelReadyExtractor, _name: str, _attributes: dict[str, str]) -> None:
    """Enter a list, which only changes the indent of the items inside it."""
    extractor.builder.flush()
    extractor.list_depth += 1


def _start_item(extractor: ModelReadyExtractor, _name: str, _attributes: dict[str, str]) -> None:
    """Start a list item at the current nesting indent."""
    extractor.builder.open("  " * max(0, extractor.list_depth - 1) + "- ")


def _start_quote(extractor: ModelReadyExtractor, _name: str, _attributes: dict[str, str]) -> None:
    """Start a block quote."""
    extractor.builder.open("> ")


def _start_block(extractor: ModelReadyExtractor, _name: str, _attributes: dict[str, str]) -> None:
    """Close whatever was open; these elements only ever end a block."""
    extractor.builder.flush()


def _start_break(extractor: ModelReadyExtractor, _name: str, _attributes: dict[str, str]) -> None:
    """Emit a hard line break, which Markdown spells as two trailing spaces."""
    extractor.builder.markup("  \n")


def _start_image(extractor: ModelReadyExtractor, _name: str, attributes: dict[str, str]) -> None:
    """Emit alt text rather than the image, which a text model cannot see."""
    alt = attributes.get("alt", "").strip()
    if alt:
        resolved = extractor.context.resolve(attributes.get("src", ""))
        extractor.builder.markup(f"![{escape_inline(alt)}]({resolved})")


def _end_title(extractor: ModelReadyExtractor) -> None:
    """Stop collecting the document title."""
    extractor.in_title = False


def _end_pre(extractor: ModelReadyExtractor) -> None:
    """Emit the buffered code block."""
    extractor.builder.close_code()


def _end_anchor(extractor: ModelReadyExtractor) -> None:
    """Close the open link, so following text is not swallowed into it."""
    extractor.href = None


def _end_list(extractor: ModelReadyExtractor) -> None:
    """Leave a list, restoring the outer indent."""
    extractor.list_depth = max(0, extractor.list_depth - 1)
    extractor.builder.flush()


def _end_block(extractor: ModelReadyExtractor) -> None:
    """Close whatever block this element was carrying."""
    extractor.builder.flush()


_STARTERS: Final[dict[str, Any]] = {
    "title": _start_title,
    "pre": _start_pre,
    "code": _start_code,
    "a": _start_anchor,
    "ul": _start_list,
    "ol": _start_list,
    "li": _start_item,
    "blockquote": _start_quote,
    "br": _start_break,
    "img": _start_image,
    **{name: _start_heading for name in HEADINGS},
    **{name: _start_block for name in ("p", "div", "section", "tr")},
}

_ENDERS: Final[dict[str, Any]] = {
    "title": _end_title,
    "pre": _end_pre,
    "a": _end_anchor,
    "ul": _end_list,
    "ol": _end_list,
    **{name: _end_block for name in HEADINGS},
    **{name: _end_block for name in ("p", "li", "blockquote", "div", "section", "tr")},
}


def extract(html: str, url: str, max_chars: int | None = None) -> ExtractedDocument:
    """Extract model-ready Markdown from one page.

    `max_chars` truncates at a block boundary and says so in the text. A silent truncation
    hands a model a document that simply stops, and a model has no way to tell a document
    that ended from one that was cut.
    """
    extractor = ModelReadyExtractor(url)
    extractor.feed(html)
    extractor.close()
    document = extractor.document()
    if max_chars is not None and len(document.markdown) > max_chars:
        document.markdown = _truncate(document.markdown, max_chars) + TRUNCATION_MARKER
        document.truncated = True
        document.warnings.append(f"truncated: budget of {max_chars} characters")
    return document


def _truncate(markdown: str, max_chars: int) -> str:
    """Cut at the last block boundary inside the budget, never mid-sentence."""
    window = markdown[:max_chars]
    boundary = window.rfind("\n\n")
    return window[:boundary].rstrip() if boundary > 0 else window.rstrip()
