"""Image URL extraction from organizer Markdown (#2090, ADR-036).

The SPA renders organizer Markdown with CommonMark (micromark). Extraction uses a
CommonMark parser too, so the server finds the same images the browser would
render: inline and reference images, but not links, code, or raw text.
"""

from __future__ import annotations

from ctf.content_images import extract_image_urls, is_admissible_image_url, is_external_image_url


def test_finds_inline_and_reference_images_in_document_order():
    text = (
        "Intro ![logo](https://img.example.test/a.png)\n\n"
        '![diagram](https://img.example.test/b.webp "Diagram")\n\n'
        "![ref][pic]\n\n"
        "[pic]: https://img.example.test/c.gif\n"
    )
    assert extract_image_urls(text) == [
        "https://img.example.test/a.png",
        "https://img.example.test/b.webp",
        "https://img.example.test/c.gif",
    ]


def test_ignores_links_code_and_plain_text():
    text = (
        "[not an image](https://img.example.test/link.png)\n\n"
        "`![code](https://img.example.test/code.png)`\n\n"
        "    ![indented](https://img.example.test/indented.png)\n\n"
        "https://img.example.test/bare.png\n"
    )
    assert extract_image_urls(text) == []


def test_finds_images_inside_lists_tables_and_links():
    text = (
        "- item ![one](https://img.example.test/1.png)\n\n"
        "| a |\n| --- |\n| ![two](https://img.example.test/2.png) |\n\n"
        "[![three](https://img.example.test/3.png)](https://site.example.test/)\n"
    )
    assert extract_image_urls(text) == [
        "https://img.example.test/1.png",
        "https://img.example.test/2.png",
        "https://img.example.test/3.png",
    ]


def test_deduplicates_and_tolerates_empty_input():
    text = "![a](https://img.example.test/x.png) ![b](https://img.example.test/x.png)"
    assert extract_image_urls(text) == ["https://img.example.test/x.png"]
    assert extract_image_urls("") == []
    assert extract_image_urls(None) == []


def test_external_image_url_is_absolute_http_or_https_only():
    assert is_external_image_url("https://img.example.test/a.png")
    assert is_external_image_url("http://img.example.test/a.png")
    assert not is_external_image_url("/static/images/logo.png")
    assert not is_external_image_url("images/logo.png")
    assert not is_external_image_url("data:image/png;base64,AAAA")
    assert not is_external_image_url("javascript:alert(1)")
    assert not is_external_image_url("//img.example.test/a.png")
    assert not is_external_image_url("")


def test_only_external_images_are_extracted():
    text = "![rel](/static/a.png) ![data](data:image/png;base64,AAAA) ![ext](https://img.example.test/a.png)"
    assert extract_image_urls(text) == ["https://img.example.test/a.png"]


def test_inadmissible_urls_are_not_extracted():
    """Over-long or malformed URLs get no copy row and render as links (core-F4, #2090)."""
    long_url = "https://img.example.test/" + "a" * 2100 + ".png"
    text = f"![long]({long_url}) ![bad](https://exa_mple..test/a.png) ![ok](https://img.example.test/ok.png)"
    assert extract_image_urls(text) == ["https://img.example.test/ok.png"]
    assert not is_admissible_image_url(long_url)
    assert not is_admissible_image_url("https://exa_mple..test/a.png")
    assert is_admissible_image_url("https://img.example.test/ok.png")
