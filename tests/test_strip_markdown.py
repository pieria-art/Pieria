"""C1: AI enrichment emits Markdown emphasis; the placard / /art page render plain text. strip_markdown
flattens inline emphasis so the markers don't show literally. Mirrors static/app.js stripMd."""

import pytest

from config import strip_markdown


@pytest.mark.parametrize("raw,expected", [
    ("*The Irish Question*", "The Irish Question"),
    ("a **bold** word", "a bold word"),
    ("some _emphasis_ here", "some emphasis here"),
    ("`code` span", "code span"),
    ("see [the source](http://example.com)", "see the source"),
    ("# Heading\ntext", "Heading\ntext"),
    ("plain prose, untouched", "plain prose, untouched"),
    (None, ""),
    ("", ""),
])
def test_strip_markdown(raw, expected):
    assert strip_markdown(raw) == expected
