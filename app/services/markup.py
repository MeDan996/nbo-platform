"""A small, escape-first Markdown subset for question text.

Author-supplied content is rendered for other people to read, so the rule here is
simple: escape everything first, then re-introduce a fixed set of constructs. No
raw HTML ever survives, which removes the whole class of stored-XSS bugs that a
general-purpose Markdown library would need careful configuration to avoid.

The supported set is chosen for biology writing rather than for general prose:

    **bold**  *italic*  `code`
    ~sub~     ^sup^          -> H~2~O, Ca^2+^, 3'->5'
    ### heading
    - bullets / 1. numbered lists
    | pipe | tables |        -> the IBO papers lean on these heavily
    ![caption](/media/figures/x.png)
    [text](https://example.org)
"""
from __future__ import annotations

import html
import re

_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", re.S)
_UNDER_ITALIC = re.compile(r"(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])", re.S)
_CODE = re.compile(r"`([^`]+?)`")
_SUB = re.compile(r"~(?!\s)([^~]{1,40}?)(?<!\s)~")
_SUP = re.compile(r"\^(?!\s)([^\^]{1,40}?)(?<!\s)\^")
_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")

# Only same-origin media and plain http(s) links are allowed through, which
# keeps `javascript:` and `data:` URLs out of rendered question text.
_SAFE_URL = re.compile(r"^(?:/media/|/static/|https?://)[^\s\"'<>]*$")


def _inline(text: str) -> str:
    """Apply inline formatting to already-escaped text."""
    def image(match: re.Match) -> str:
        alt, src = match.group(1), match.group(2)
        if not _SAFE_URL.match(src):
            return html.escape(match.group(0))
        return f'<img src="{src}" alt="{alt}" loading="lazy" class="md-img">'

    def link(match: re.Match) -> str:
        label, href = match.group(1), match.group(2)
        if not _SAFE_URL.match(href):
            return html.escape(match.group(0))
        return f'<a href="{href}" target="_blank" rel="noopener noreferrer">{label}</a>'

    text = _IMAGE.sub(image, text)
    text = _LINK.sub(link, text)
    text = _CODE.sub(r"<code>\1</code>", text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _ITALIC.sub(r"<em>\1</em>", text)
    text = _UNDER_ITALIC.sub(r"<em>\1</em>", text)
    text = _SUB.sub(r"<sub>\1</sub>", text)
    text = _SUP.sub(r"<sup>\1</sup>", text)
    return text


def _split_row(line: str) -> list[str]:
    cells = line.strip().strip("|").split("|")
    return [c.strip() for c in cells]


def _is_separator(line: str) -> bool:
    stripped = line.strip().strip("|")
    return bool(stripped) and set(stripped.replace("|", "")) <= set("-: ")


def render(source: str | None) -> str:
    """Render the Markdown subset to HTML. Always returns safe markup."""
    if not source:
        return ""

    text = html.escape(str(source).replace("\r\n", "\n").replace("\r", "\n"))
    lines = text.split("\n")
    out: list[str] = []
    index = 0
    n = len(lines)

    while index < n:
        line = lines[index]
        stripped = line.strip()

        if not stripped:
            index += 1
            continue

        # Heading
        heading = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if heading:
            level = min(6, len(heading.group(1)) + 2)
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            index += 1
            continue

        # Table: a header row followed by a --- separator row.
        if "|" in stripped and index + 1 < n and _is_separator(lines[index + 1]):
            header = _split_row(stripped)
            index += 2
            body: list[list[str]] = []
            while index < n and "|" in lines[index] and lines[index].strip():
                body.append(_split_row(lines[index]))
                index += 1
            head_html = "".join(f"<th>{_inline(c)}</th>" for c in header)
            rows_html = "".join(
                "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>"
                for row in body
            )
            out.append(
                '<div class="md-table-wrap"><table class="md-table">'
                f"<thead><tr>{head_html}</tr></thead><tbody>{rows_html}</tbody>"
                "</table></div>"
            )
            continue

        # Lists
        bullet = re.match(r"^[-*+]\s+(.*)$", stripped)
        numbered = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if bullet or numbered:
            tag = "ul" if bullet else "ol"
            items: list[str] = []
            pattern = r"^[-*+]\s+(.*)$" if bullet else r"^\d+[.)]\s+(.*)$"
            while index < n:
                match = re.match(pattern, lines[index].strip())
                if not match:
                    break
                items.append(f"<li>{_inline(match.group(1))}</li>")
                index += 1
            out.append(f"<{tag}>" + "".join(items) + f"</{tag}>")
            continue

        # Blockquote
        if stripped.startswith("&gt;"):
            quote: list[str] = []
            while index < n and lines[index].strip().startswith("&gt;"):
                quote.append(lines[index].strip()[4:].strip())
                index += 1
            out.append(f"<blockquote>{_inline(' '.join(quote))}</blockquote>")
            continue

        # Paragraph: consume until a blank line or the start of another block.
        para: list[str] = []
        while index < n and lines[index].strip():
            candidate = lines[index].strip()
            if re.match(r"^(#{1,4}\s|[-*+]\s|\d+[.)]\s|&gt;)", candidate):
                break
            if "|" in candidate and index + 1 < n and _is_separator(lines[index + 1]):
                break
            para.append(candidate)
            index += 1
        if para:
            out.append("<p>" + _inline("<br>".join(para)) + "</p>")

    return "".join(out)


def plain(source: str | None, limit: int = 160) -> str:
    """A short, tag-free excerpt for list views and search results."""
    if not source:
        return ""
    text = re.sub(r"[#*`~^|_>\[\]]", "", str(source))
    text = re.sub(r"!\(.*?\)|\(.*?\)", "", text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
