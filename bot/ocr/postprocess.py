"""Deterministic clean-up of recognised text. No AI here: only fixes that are safe to make blind."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

LIGATURES = {"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "ft", "ﬆ": "st"}
MATH_SYMBOLS = set("=+−×÷±≤≥≠≈∑∫√∞∂∆πθλμσΩαβγ^<>∈∀∃→⇒∝°")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏﻿]")
# A digit standing where a letter belongs, in the two cases with only one possible reading:
# "p0wer" -> "power" and "ce11s" -> "cells". A single 1 could be l or i, so it is left for the AI layer.
_DIGIT_IN_WORD = re.compile(r"(?<=[a-z])(?:0|11)(?=[a-z])|(?<=[a-z]{2})11\b")
_MATH_LINE = re.compile(r"[=^√∑∫≤≥≠±×÷]|\b(?:sin|cos|tan|log|ln|lim|dx|dy)\b|\d\s*[+\-*/]\s*[\dA-Za-z(]")


def clean_page(text: str) -> str:
    """Normalise characters and spacing, rejoin hyphenated words, drop specks read as text."""
    text = unicodedata.normalize("NFC", text)
    for ligature, plain in LIGATURES.items():
        text = text.replace(ligature, plain)
    text = _CONTROL.sub("", text).replace(" ", " ")

    lines: list[str] = []
    for raw in text.splitlines():
        line = re.sub(r"[ \t]+", " ", raw).strip()
        if line and _is_noise(line):
            continue
        if line and not looks_like_math(line):
            line = _DIGIT_IN_WORD.sub(lambda m: "o" if m.group() == "0" else "ll", line)
            line = re.sub(r"(?<![\w|])\|(?=\s[a-z])", "I", line)  # a lone "|" before a word is "I"
        lines.append(line)

    # "photo-\nsynthesis" -> "photosynthesis"
    joined: list[str] = []
    for line in lines:
        if joined and re.search(r"[a-z]-$", joined[-1]) and re.match(r"[a-z]", line):
            first, _, rest = line.partition(" ")
            joined[-1] = joined[-1][:-1] + first
            if rest:
                joined.append(rest)
        else:
            joined.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(joined)).strip()


def _is_noise(line: str) -> bool:
    """Dust, ruled lines and smudges come back as one or two stray symbols."""
    if any(ch.isalnum() for ch in line) or any(ch in MATH_SYMBOLS for ch in line):
        return False
    return len(line) <= 3 or len(set(line.replace(" ", ""))) <= 2


def looks_like_math(text: str) -> bool:
    """True when a noticeable part of the lines are formulas."""
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return False
    formula_lines = sum(1 for l in lines if _MATH_LINE.search(l))
    return formula_lines >= 2 and formula_lines >= 0.15 * len(lines) or (len(lines) <= 3 and formula_lines >= 1)


def plausibility(text: str) -> float:
    """0..1: how much the text looks like language rather than recognition debris."""
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return 0.0
    wanted = sum(1 for ch in compact if ch.isalnum() or ch in ".,;:!?()'\"-%/" or ch in MATH_SYMBOLS)
    tokens = text.split()
    wordlike = sum(1 for t in tokens if re.fullmatch(r"[^\W\d_]{2,}[.,;:!?)'\"]*", t) or re.fullmatch(r"[\d.,%]+", t))
    return 0.5 * wanted / len(compact) + 0.5 * (wordlike / len(tokens) if tokens else 0.0)


def strip_running_lines(pages: list[str]) -> list[str]:
    """Remove headers, footers and page numbers that repeat on most pages of a document."""
    def key(line: str) -> str:
        return re.sub(r"\d+", "#", line.strip().lower())

    def edge(lines: list[str]) -> list[str]:
        """The top and bottom lines of a page: two each on a full page, one each on a short one."""
        body = [l for l in lines if l.strip()]
        count = 2 if len(body) >= 8 else 1 if len(body) >= 3 else 0
        return body[:count] + body[-count:] if count else []

    edges: Counter[str] = Counter()
    filled = [p for p in pages if p.strip()]
    if len(filled) >= 4:
        for page in filled:
            for line in {key(l) for l in edge(page.splitlines())}:
                edges[line] += 1
    repeated = {line for line, count in edges.items() if count >= max(3, 0.6 * len(filled)) and len(line) < 80}

    result = []
    for page in pages:
        lines = page.splitlines()
        edge_lines = set(map(id, edge(lines)))
        kept = []
        for line in lines:
            if id(line) in edge_lines:
                if key(line) in repeated:
                    continue
                if re.fullmatch(r"(page\s*)?[-–—]?\s*\d{1,4}\s*[-–—]?(\s*(of|/)\s*\d{1,4})?", line.strip().lower()):
                    continue  # a bare page number
            kept.append(line)
        result.append(re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip())
    return result
