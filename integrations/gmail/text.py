"""Turning untrusted email markup into safe, readable plain text. Nothing here executes anything."""

import re
from html.parser import HTMLParser

_SKIP_TAGS = {"script", "style", "head", "title", "noscript", "template", "svg", "iframe", "object", "embed"}
_BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "table", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "hr",
    "section", "article", "header", "footer",
}
_INVISIBLE = re.compile("[​‌‍⁠﻿­]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MAX_HTML_CHARS = 500_000


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def normalize_whitespace(text: str) -> str:
    text = _CONTROL.sub("", _INVISIBLE.sub("", text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")))
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def html_to_text(html: str) -> str:
    """Readable text from HTML. Scripts, styles and embedded objects are dropped; tags never run."""
    parser = _TextExtractor()
    try:
        parser.feed(html[:MAX_HTML_CHARS])
        parser.close()
    except Exception:  # noqa: BLE001 - malformed markup must not crash the parser
        pass
    return normalize_whitespace("".join(parser.parts))


_QUOTE_HEADERS = (
    re.compile(r"^on .{5,200}wrote:\s*$", re.I),
    re.compile(r"^-{2,}\s*(original message|forwarded message)\s*-{2,}\s*$", re.I),
    re.compile(r"^_{5,}\s*$"),
    re.compile(r"^from:\s.+", re.I),  # Outlook style header block starts a quoted reply
)


def strip_quoted_reply(text: str) -> str:
    """The new part of a reply: quoted lines, the quoted-reply header and everything after it are removed,
    and a conventional '-- ' signature is cut. Conservative: if nothing new would remain, the text is kept."""
    lines = text.split("\n")
    kept: list[str] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if line.rstrip() == "--" or line == "-- ":
            break  # signature delimiter
        if any(p.match(stripped) for p in _QUOTE_HEADERS[:3]):
            break
        if _QUOTE_HEADERS[3].match(stripped) and index > 0 and any(
            re.match(r"^(sent|date|to|subject):", n.strip(), re.I) for n in lines[index + 1 : index + 4]
        ):
            break
        if stripped.startswith(">"):
            continue
        kept.append(line)
    result = normalize_whitespace("\n".join(kept))
    return result or text.strip()


def sanitize_for_prompt(text: str, limit: int) -> str:
    """Email text placed inside a delimited prompt block: no angle brackets (so it cannot close the block or
    fake a tag), no control characters, bounded length."""
    cleaned = normalize_whitespace(text).replace("<", "(").replace(">", ")")
    return cleaned[:limit]


def one_line(text: str, limit: int) -> str:
    return " ".join(sanitize_for_prompt(text, limit * 2).split())[:limit]
