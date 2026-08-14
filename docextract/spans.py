"""Source spans — where in the document each extracted value came from.

Every value the engine reads out of a document can point back at the exact
characters it was read from. That pointer is a *span*::

    {"start": 176, "end": 189, "line": 12, "col": 17, "text": "INV-2026-8842"}

``start`` / ``end`` are half-open **code-point** offsets into the raw document
text exactly as it was submitted (no normalisation, no re-wrapping), which is
what Python string slicing uses; a browser must therefore index the same text by
code point (``Array.from(text)``) rather than by UTF-16 code unit. ``line`` and
``col`` are 1-based and follow :meth:`str.splitlines` boundaries. ``text`` is the
source slice itself, so a consumer can verify a span before highlighting it
instead of trusting the offsets blindly.

Two honest limits are baked into the contract:

* A span is the *source evidence*, not a copy of the value. They usually match
  verbatim, but not always — a currency inferred from a "€" symbol carries the
  span of the symbol, and a European-formatted "1.234,56" carries the span of
  those characters while the value is ``1234.56``. Comparing ``span["text"]``
  with the value shows a reader which case they are looking at.
* A value the engine *computed* rather than read — a summed line total, a unit
  price back-calculated from amount ÷ quantity — has no span at all. Those
  slots are ``None``, and the value's owner lists the name under ``derived``.
  Pointing such a value at arbitrary characters would be a lie.

Example::

    from docextract import spans

    text = "Invoice Number: INV-1\\n"
    spans.make_span(text, 16, 21)
    # {"start": 16, "end": 21, "line": 1, "col": 17, "text": "INV-1"}
"""

from __future__ import annotations

from bisect import bisect_right


def line_starts(text: str) -> list[int]:
    """Return the offset at which each line of ``text`` begins.

    The result has one entry per :meth:`str.splitlines` line, so
    ``line_starts(text)[n]`` is the offset of ``text.splitlines()[n]``.
    Pass it to :func:`make_span` to avoid recomputing it per span.
    """
    starts = [0]
    offset = 0
    for raw in text.splitlines(keepends=True):
        offset += len(raw)
        starts.append(offset)
    starts.pop()  # the trailing offset is the end of the text, not a line start
    return starts or [0]


def trim(text: str, start: int, end: int) -> tuple[int, int]:
    """Shrink ``[start, end)`` past surrounding whitespace.

    Extraction strips the values it captures; the span has to be stripped the
    same way or it would highlight trailing blanks.
    """
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def make_span(
    text: str, start: int, end: int, *, starts: list[int] | None = None
) -> dict[str, object] | None:
    """Build a span for ``text[start:end]``, or ``None`` if that is not a real slice.

    Args:
        text: The document the offsets refer to.
        start: Half-open start offset (code points).
        end: Half-open end offset (code points).
        starts: Optional precomputed :func:`line_starts` for ``text``.

    Returns:
        ``{"start", "end", "line", "col", "text"}``, or ``None`` for an empty or
        out-of-range range — a caller that cannot locate a value must be able to
        say so rather than emit a span that points nowhere.
    """
    if start < 0 or end > len(text) or end <= start:
        return None
    starts = line_starts(text) if starts is None else starts
    index = bisect_right(starts, start) - 1
    return {
        "start": start,
        "end": end,
        "line": index + 1,
        "col": start - starts[index] + 1,
        "text": text[start:end],
    }
