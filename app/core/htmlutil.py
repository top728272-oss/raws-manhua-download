"""HTML scoping helpers shared by search parsers.

Search-result parsers must never scan a whole page: no-hit pages are full of
nav/sidebar/recommendation links that look exactly like results. These helpers
remove page chrome so an anchor scan only sees main content.
"""

from __future__ import annotations

import re

_CHROME_BLOCK = re.compile(
    r"<(script|style|nav|header|footer|aside|form)\b[^>]*>.*?</\1>",
    re.I | re.S,
)
_COMMENT = re.compile(r"<!--.*?-->", re.S)


_PLACEHOLDER_RE = re.compile(r"(static|placeholder|load\.gif|logo|bg\.png)", re.I)


_RECO_MARKERS = (
    "您可能喜歡",
    "您可能喜欢",
    "猜你喜歡",
    "猜你喜欢",
    "為您推薦",
    "为您推荐",
    "為你推薦",
    "为你推荐",
    "相关推荐",
    "相關推薦",
    "相关作品",
    "相關作品",
)


def cut_recommendations(html: str) -> str:
    """Drop everything from the first recommendation-section heading onward."""
    if not html:
        return html
    cut = len(html)
    for marker in _RECO_MARKERS:
        pos = html.find(marker)
        if 0 <= pos < cut:
            cut = pos
    return html[:cut] if cut < len(html) else html


def strip_chrome(html: str) -> str:
    """Remove script/style/nav/header/footer/aside/form blocks and comments.

    Conservative: keeps ``<main>``, ``<section>``, ``<div>``, ``<ul>``,
    ``<article>``, ``<noscript>``.
    """
    if not html:
        return html
    return _CHROME_BLOCK.sub(" ", _COMMENT.sub(" ", html))


def split_on_container(html: str, class_fragment: str) -> list[str]:
    """Split ``html`` at every opening tag whose class contains ``class_fragment``.

    Returns the chunks AFTER each opening tag (i.e. each container's content),
    discarding everything before the first container (nav/header). This is
    nesting-safe, unlike a non-greedy ``<div>.*?</div>`` match.

    Each chunk still contains the containers that FOLLOW it. When a card nests
    ``<div>``s of its own, prefer :func:`iter_containers`, which bounds every
    slice at the next container.
    """
    pattern = re.compile(
        r'<[a-zA-Z][^>]*class=["\'][^"\']*' + re.escape(class_fragment) + r'[^"\']*["\'][^>]*>',
        re.I,
    )
    return pattern.split(html)[1:]


def iter_containers(html: str, class_fragment: str) -> list[str]:
    """Yield each container's inner HTML, bounded by the next container.

    Unlike :func:`split_on_container`, a returned slice stops at the next
    container's opening tag, so a card that contains its own ``<div>`` (a cover
    shadow, a badge) can be truncated at a ``</div>`` without losing the fields
    that follow it.
    """
    pattern = re.compile(
        r'<[a-zA-Z][^>]*class=["\'][^"\']*' + re.escape(class_fragment) + r'[^"\']*["\'][^>]*>',
        re.I,
    )
    marks = list(pattern.finditer(html))
    out: list[str] = []
    for idx, mark in enumerate(marks):
        end = marks[idx + 1].start() if idx + 1 < len(marks) else len(html)
        out.append(html[mark.end() : end])
    return out


def is_placeholder_cover(url: str | None) -> bool:
    """True if the cover URL looks like a lazy-load placeholder or site chrome."""
    return bool(url and _PLACEHOLDER_RE.search(url))
