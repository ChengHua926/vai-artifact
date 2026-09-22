"""Deterministic segmentation of Tau policy markdown into labelable units.

A policy document becomes an ordered list of sentence-level segments with
stable ids `{domain}-pNN-sNN`, where both the paragraph and sentence
counters are 1-based and run over the whole document. Heading lines set
the heading association for the segments that follow and are not
themselves segments. Pure functions, no I/O.
"""

from __future__ import annotations

import re

_HEADING_RE = re.compile(r"(#{1,6})\s+(\S.*)")
_BULLET_RE = re.compile(r"(?:[-*+]|\d{1,3}[.)])\s")
_BREAK_RE = re.compile(r"([.?!][\"')\]]*)(\s+)(?=[\"'(\[]?[A-Z])")
_ABBREVIATIONS = frozenset(
    {"e.g", "i.e", "etc", "vs", "cf", "mr", "mrs", "ms", "dr", "no", "st"}
)


def _split_sentences(text: str) -> list[str]:
    sentences: list[str] = []
    start = 0
    for match in _BREAK_RE.finditer(text):
        if text[match.start(1)] == ".":
            before = text[start : match.start(1)]
            word = before.rsplit(None, 1)[-1] if before.split() else ""
            bare = word.strip("(\"'").lower()
            if bare in _ABBREVIATIONS or (len(bare) == 1 and bare.isalpha()):
                continue
        piece = text[start : match.end(1)].strip()
        if piece:
            sentences.append(piece)
        start = match.end()
    remainder = text[start:].strip()
    if remainder:
        sentences.append(remainder)
    return sentences


def _block_units(lines: list[str]) -> list[str]:
    """Sentence units of one paragraph block; each bullet item is one unit."""

    units: list[str] = []
    prose: list[str] = []
    bullet: list[str] | None = None

    def flush_prose() -> None:
        if prose:
            units.extend(_split_sentences(" ".join(prose)))
            prose.clear()

    for line in lines:
        stripped = line.strip()
        if _BULLET_RE.match(stripped):
            flush_prose()
            if bullet:
                units.append(" ".join(bullet))
            bullet = [stripped]
        elif bullet is not None:
            bullet.append(stripped)
        else:
            prose.append(stripped)
    if bullet:
        units.append(" ".join(bullet))
    flush_prose()
    return [unit for unit in (u.strip() for u in units) if unit]


def segment_policy(text: str, domain: str) -> list[dict[str, object]]:
    """Segment a policy markdown string into stable ordered units."""

    segments: list[dict[str, object]] = []
    heading: str | None = None
    paragraph = 0
    sentence = 0
    block: list[str] = []

    def flush() -> None:
        nonlocal paragraph, sentence
        units = _block_units(block)
        block.clear()
        if not units:
            return
        paragraph += 1
        for unit in units:
            sentence += 1
            segments.append(
                {
                    "id": f"{domain}-p{paragraph:02d}-s{sentence:02d}",
                    "paragraph": paragraph,
                    "sentence": sentence,
                    "heading": heading,
                    "text": unit,
                }
            )

    for raw_line in text.split("\n"):
        line = raw_line.rstrip()
        heading_match = _HEADING_RE.match(line.strip())
        if heading_match:
            flush()
            heading = heading_match.group(2).strip()
            continue
        if not line.strip():
            flush()
            continue
        block.append(line)
    flush()
    return segments
