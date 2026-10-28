"""Find external image URLs in organizer Markdown (#2090, ADR-036).

The portal enforces ``img-src 'self' data:``, so the platform copies external
images from organizer content into its own storage. This module is the pure
discovery step: it parses Markdown with CommonMark (the same grammar the SPA's
renderer uses) and returns the absolute ``http``/``https`` image URLs in
document order. It has no model or network dependency, so the backfill
migration can use it too. :func:`is_admissible_image_url` is the one admission
rule for copy rows.
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from markdown_it import MarkdownIt
from markdown_it.token import Token

# CommonMark plus GFM tables, matching the SPA's remark-gfm image grammar.
_PARSER = MarkdownIt("commonmark").enable("table")
_EXTERNAL_SCHEMES = frozenset({"http", "https"})
# Matches CTFContentImage.source_url; a longer URL gets no copy row.
MAX_SOURCE_URL_LENGTH = 2048
_URL_VALIDATOR = URLValidator(schemes=sorted(_EXTERNAL_SCHEMES))


def is_external_image_url(url: str) -> bool:
    """Return True for an absolute ``http``/``https`` URL with a host."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme.lower() in _EXTERNAL_SCHEMES and bool(parts.netloc)


def is_admissible_image_url(url: str) -> bool:
    """Return True when ``url`` is external and valid for a copy row.

    The single admission rule for every row writer (sync and backfill): an
    inadmissible URL gets no row, so it renders as a link and never reaches the
    copy queue.
    """
    if len(url) > MAX_SOURCE_URL_LENGTH or not is_external_image_url(url):
        return False
    try:
        _URL_VALIDATOR(url)
    except ValidationError:
        return False
    return True


def _image_sources(tokens: Iterable[Token]) -> Iterable[str]:
    """Yield the ``src`` of every image token, including images nested in inline tokens."""
    for token in tokens:
        if token.type == "image":
            src = token.attrGet("src")
            if isinstance(src, str):
                yield src
        if token.children:
            yield from _image_sources(token.children)


def extract_image_urls(markdown: str | None) -> list[str]:
    """Return the unique admissible external image URLs in ``markdown``, in document order."""
    if not markdown:
        return []
    found: dict[str, None] = {}
    for src in _image_sources(_PARSER.parse(markdown)):
        if is_admissible_image_url(src):
            found.setdefault(src, None)
    return list(found)
