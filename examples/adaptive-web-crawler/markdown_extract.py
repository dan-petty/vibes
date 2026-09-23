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


class ModelReadyExtractor(HTMLParser):
    """Walk a document once, emitting Markdown blocks and recording what was skipped."""

    # Elements with no end tag. A void element can never open a region to skip, and a
    # counter that does not know this waits forever for a `</embed>` that never comes.
    VOID: ClassVar[frozenset[str]] = frozenset({"br", "hr", "img", "embed", "source", "track"})

    def __init__(self, base_url: str) -> None:
        """Prepare an extractor rooted at the page's own URL."""
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.blocks: list[str] = []
        self.links: list[str] = []
        self.title = ""
        self.regions_seen: set[str] = set()
        self.dropped: set[str] = set()
        self._skip_stack: list[str] = []
        self._in_title = False
        # `(is_markup, text)`. Page text and markup this extractor synthesised are kept
        # apart until the block is closed: escaping at the point of collection cannot
        # tell them apart afterwards, and an emitted `![alt](src)` would come out as
        # `!\\[alt\\](src)`, which renders as literal brackets. Escaping once, at flush,
        # over the text parts only, cannot do that.
        #
        # Named `_fragments` rather than `_pending` because `html.parser.HTMLParser` in
        # CPython 3.14 keeps its own `_pending`, and its `close()` runs
        # `self.rawdata += ''.join(self._pending)`. Shadowing it fed this buffer back
        # through the parser as page text: every synthesised link and image reappeared
        # as a data node and was escaped a second time, at `close()`, with nothing in
        # the traceback pointing at the collision. **Subclassing puts a base class's
        # private attributes in your namespace too**, and `_name` is a convention, not
        # a scope.
        self._fragments: list[tuple[bool, str]] = []
        self._prefix = ""
        self._code: list[str] | None = None
        self._code_language = ""
        self._list_depth = 0
        self._href: str | None = None

    # --- Element entry and exit ----------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Dispatch one opening tag."""
        name = tag.lower()
        attributes = {key.lower(): (value or "") for key, value in attrs}
        if self._enter_skip(name, attributes):
            return
        if name == "base" and attributes.get("href"):
            self.base_url = urljoin(self.base_url, attributes["href"])
            return
        self._start(name, attributes)

    def _enter_skip(self, name: str, attributes: dict[str, str]) -> bool:
        """Return True when this element is inside chrome, or opens some.

        A stack of the tags that opened a skipped region, never a counter. A counter has to
        guess which end tag closes the region, and the first version decremented on every
        non-void end tag — so `</a>` inside a `<nav>` ended the skip and the rest of the
        navigation was emitted into the document as content. The stack pops only when the
        tag that opened the region closes, which is also correct for a `<nav>` inside a
        `<nav>`.
        """
        hidden = attributes.get("aria-hidden") == "true"
        chrome = name in BOILERPLATE_TAGS or attributes.get("role") in BOILERPLATE_ROLES or hidden
        if chrome and name not in self.VOID:
            self._skip_stack.append(name)
            self.dropped.add(name)
            return True
        return bool(self._skip_stack) or chrome

    def _start(self, name: str, attributes: dict[str, str]) -> None:
        """Open one non-chrome element, by table rather than by ladder (§10.1)."""
        if name in MAIN_REGIONS:
            self.regions_seen.add(name)
        handler = _STARTERS.get(name)
        if handler:
            handler(self, name, attributes)

    def _start_title(self, _name: str, _attributes: dict[str, str]) -> None:
        """Begin collecting the document title."""
        self._in_title = True

    def _start_pre(self, _name: str, _attributes: dict[str, str]) -> None:
        """Begin buffering a code block, whose text is never escaped."""
        self._code = []
        self._code_language = ""

    def _start_code(self, _name: str, attributes: dict[str, str]) -> None:
        """Take the highlight language from `<code>` when it is inside a `<pre>`."""
        if self._code is not None:
            self._code_language = self._language(attributes) or self._code_language

    def _start_anchor(self, _name: str, attributes: dict[str, str]) -> None:
        """Resolve the href now, against the base, so the link survives leaving the origin."""
        self._href = urljoin(self.base_url, attributes["href"]) if attributes.get("href") else None

    def _start_heading(self, name: str, _attributes: dict[str, str]) -> None:
        """Start a heading block at the document's own level."""
        self._flush()
        self._prefix = HEADINGS[name] + " "

    def _start_list(self, _name: str, _attributes: dict[str, str]) -> None:
        """Enter a list, which only changes the indent of the items inside it."""
        self._flush()
        self._list_depth += 1

    def _start_item(self, _name: str, _attributes: dict[str, str]) -> None:
        """Start a list item at the current nesting indent."""
        self._flush()
        self._prefix = "  " * max(0, self._list_depth - 1) + "- "

    def _start_quote(self, _name: str, _attributes: dict[str, str]) -> None:
        """Start a block quote."""
        self._flush()
        self._prefix = "> "

    def _start_block(self, _name: str, _attributes: dict[str, str]) -> None:
        """Close whatever was open; these elements only ever end a block."""
        self._flush()

    def _start_break(self, _name: str, _attributes: dict[str, str]) -> None:
        """Emit a hard line break, which Markdown spells as two trailing spaces."""
        self._fragments.append((True, "  \n"))

    def _start_image(self, _name: str, attributes: dict[str, str]) -> None:
        """Emit alt text rather than the image, which a text model cannot see."""
        alt = attributes.get("alt", "").strip()
        if alt:
            resolved = urljoin(self.base_url, attributes.get("src", ""))
            self._fragments.append((True, f"![{escape_inline(alt)}]({resolved})"))

    @staticmethod
    def _language(attributes: dict[str, str]) -> str:
        """Read the highlight language from a class attribute, if one states it."""
        match = _LANGUAGE.search(attributes.get("class", ""))
        return match.group(1) if match else ""

    def handle_endtag(self, tag: str) -> None:
        """Close one element, flushing whatever block it completed."""
        name = tag.lower()
        if self._skip_stack:
            if name == self._skip_stack[-1]:
                self._skip_stack.pop()
            return
        self._end(name)

    def _end(self, name: str) -> None:
        """Close one non-chrome element, by table rather than by ladder (§10.1)."""
        handler = _ENDERS.get(name)
        if handler:
            handler(self)

    def _end_title(self) -> None:
        """Stop collecting the document title."""
        self._in_title = False

    def _end_anchor(self) -> None:
        """Close the open link, so following text is not swallowed into it."""
        self._href = None

    def _end_list(self) -> None:
        """Leave a list, restoring the outer indent."""
        self._list_depth = max(0, self._list_depth - 1)
        self._flush()

    def _end_block(self) -> None:
        """Close whatever block this element was carrying."""
        self._flush()

    def _close_code(self) -> None:
        """Emit the buffered code as a fenced block whose fence the content chose."""
        if self._code is None:
            return
        code = "".join(self._code).strip("\n")
        self._code = None
        if not code:
            return
        self._flush()
        fence = fence_for(code)
        self.blocks.append(f"{fence}{self._code_language}\n{code}\n{fence}")

    # --- Text ----------------------------------------------------------------------------

    def handle_data(self, data: str) -> None:
        """Buffer one text node, escaped unless it is inside a fenced block."""
        if self._skip_stack:
            return
        if self._code is not None:
            # Deliberately unescaped: the fence protects it, and a backslash inside a code
            # block is part of the code.
            self._code.append(data)
            return
        if self._in_title:
            self.title += data.strip()
            return
        text = _WHITESPACE.sub(" ", data)
        if not text.strip():
            return
        self._fragments.append(self._render_text(text))

    def _render_text(self, text: str) -> tuple[bool, str]:
        """Return one run of page text, as markup when a link is open and as text otherwise.

        A link is markup because its brackets are ours; only the label inside it comes from
        the page, so only the label is escaped.
        """
        if self._href:
            self.links.append(self._href)
            return True, f"[{escape_inline(text.strip())}]({self._href})"
        return False, text

    def _flush(self) -> None:
        """Close the block under construction, escaping the page's text and nothing else."""
        joined = "".join(
            fragment if is_markup else escape_inline(fragment)
            for is_markup, fragment in self._fragments
        ).strip()
        self._fragments = []
        prefix, self._prefix = self._prefix, ""
        if joined:
            self.blocks.append(prefix + escape_block(joined) if not prefix else prefix + joined)

    # --- Result --------------------------------------------------------------------------

    def document(self) -> ExtractedDocument:
        """Assemble the extracted document, naming anything that needs qualifying."""
        self._close_code()
        self._flush()
        markdown = _BLANK_RUNS.sub("\n\n", "\n\n".join(self.blocks).strip())
        region = next((name for name in MAIN_REGIONS if name in self.regions_seen), "body")
        warnings: list[str] = []
        if not markdown:
            # An empty string and a page with nothing to say are the same value, and the
            # difference decides whether a caller retries, renders, or gives up.
            warnings.append("no_content: the document yielded no text after boilerplate removal")
        if region == "body":
            warnings.append("no_main_region: no <main> or <article>; the whole body was taken")
        return ExtractedDocument(
            url=self.base_url,
            title=self.title.strip(),
            markdown=markdown,
            links=self.links,
            region=region,
            warnings=warnings,
        )


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


# Table dispatch per §10.1. Built after the class so each entry is the bound function, and
# flat so that adding an element is a row rather than another branch in a ladder the
# sentinel would refuse — which it did, at complexity 16 and depth 12, on the first draft.
_STARTERS: Final[dict[str, Any]] = {
    "title": ModelReadyExtractor._start_title,
    "pre": ModelReadyExtractor._start_pre,
    "code": ModelReadyExtractor._start_code,
    "a": ModelReadyExtractor._start_anchor,
    "ul": ModelReadyExtractor._start_list,
    "ol": ModelReadyExtractor._start_list,
    "li": ModelReadyExtractor._start_item,
    "blockquote": ModelReadyExtractor._start_quote,
    "br": ModelReadyExtractor._start_break,
    "img": ModelReadyExtractor._start_image,
    **{name: ModelReadyExtractor._start_heading for name in HEADINGS},
    **{name: ModelReadyExtractor._start_block for name in ("p", "div", "section", "tr")},
}

_ENDERS: Final[dict[str, Any]] = {
    "title": ModelReadyExtractor._end_title,
    "pre": ModelReadyExtractor._close_code,
    "a": ModelReadyExtractor._end_anchor,
    "ul": ModelReadyExtractor._end_list,
    "ol": ModelReadyExtractor._end_list,
    **{name: ModelReadyExtractor._end_block for name in HEADINGS},
    **{name: ModelReadyExtractor._end_block
       for name in ("p", "li", "blockquote", "div", "section", "tr")},
}
