"""Reading order. Turns the text lines found on a page into text a person would read aloud.

OCR engines return lines roughly top-to-bottom, which interleaves the columns of
two-column notes and scrambles tables. Here the lines are split into columns
(recursively, so a full-width title above two columns works), tables are read
row by row with their cells kept apart, and paragraphs and headings get blank lines.
"""

from __future__ import annotations

from statistics import median

from .types import Line

CELL_SEPARATOR = " | "


def page_text(lines: list[Line]) -> str:
    lines = [line for line in lines if line.text.strip()]
    if not lines:
        return ""
    line_height = median(line.height for line in lines)
    blocks = _blocks(lines, line_height, depth=0)
    return "\n\n".join(block for block in blocks if block).strip()


# ── splitting into blocks ────────────────────────────────────────────────
def _blocks(lines: list[Line], line_height: float, depth: int) -> list[str]:
    if len(lines) < 4 or depth > 6:
        return _paragraphs(lines, line_height)
    x0, x1 = min(l.x0 for l in lines), max(l.x1 for l in lines)
    region_width = x1 - x0
    narrow = [l for l in lines if l.width <= 0.6 * region_width]
    gap = _column_gap(narrow, x0, x1, line_height)
    if gap is None:
        return _paragraphs(lines, line_height)

    spanning = sorted((l for l in lines if l.x0 < gap < l.x1), key=lambda l: l.cy)
    if not spanning:
        left = [l for l in lines if l.cx < gap]
        right = [l for l in lines if l.cx >= gap]
        if _is_table(left, right, line_height):
            return _paragraphs(lines, line_height)
        return _blocks(left, line_height, depth + 1) + _blocks(right, line_height, depth + 1)

    # Full-width lines (a title, a caption) cut the page into bands; each band is split again.
    blocks: list[str] = []
    rest = [l for l in lines if not (l.x0 < gap < l.x1)]
    edges = [float("-inf"), *[s.cy for s in spanning], float("inf")]
    for index in range(len(edges) - 1):
        band = [l for l in rest if edges[index] < l.cy <= edges[index + 1]]
        if band:
            blocks += _blocks(band, line_height, depth + 1) if len(band) < len(lines) else _paragraphs(band, line_height)
        if index < len(spanning):
            blocks.append(spanning[index].text.strip())
    return blocks


def _column_gap(lines: list[Line], x0: float, x1: float, line_height: float) -> float | None:
    """x position of the widest empty vertical strip that separates two groups of lines."""
    if len(lines) < 4:
        return None
    width = x1 - x0
    threshold = max(1.2 * line_height, 0.02 * width)
    intervals = sorted((l.x0, l.x1) for l in lines)
    best: tuple[float, float] | None = None
    reach = intervals[0][1]
    for start, end in intervals[1:]:
        if start - reach >= threshold and (best is None or start - reach > best[1] - best[0]):
            best = (reach, start)
        reach = max(reach, end)
    if best is None:
        return None
    middle = (best[0] + best[1]) / 2
    if not x0 + 0.12 * width < middle < x1 - 0.12 * width:
        return None  # a margin note or a bullet column, not a real column break
    left = sum(1 for l in lines if l.cx < middle)
    if min(left, len(lines) - left) < 2:
        return None
    return middle


def _is_table(left: list[Line], right: list[Line], line_height: float) -> bool:
    """Side-by-side text is a table when its rows line up and one side holds short cells.

    Two columns of prose also line up row by row, but their lines are sentences;
    the label column of a table or glossary is a word or two per row.
    """
    if not left or not right:
        return False
    smaller, larger = sorted((left, right), key=len)
    aligned = sum(1 for a in smaller if any(abs(a.cy - b.cy) < 0.5 * line_height for b in larger))
    if len(smaller) < 2 or aligned < 0.7 * len(smaller):
        return False
    return min(median(len(l.text.split()) for l in group) for group in (left, right)) <= 3


# ── a block of lines into text ───────────────────────────────────────────
def _rows(lines: list[Line], line_height: float) -> list[list[Line]]:
    """Group lines that sit on the same text row, each row left to right."""
    rows: list[list[Line]] = []
    for line in sorted(lines, key=lambda l: l.cy):
        if rows and abs(line.cy - median(l.cy for l in rows[-1])) < 0.6 * min(line_height, line.height or line_height):
            rows[-1].append(line)
        else:
            rows.append([line])
    return [sorted(row, key=lambda l: l.x0) for row in rows]


def _row_text(row: list[Line], line_height: float) -> str:
    parts = [row[0].text.strip()]
    for previous, current in zip(row, row[1:]):
        gap = current.x0 - previous.x1
        parts.append(CELL_SEPARATOR if gap > 1.5 * line_height else " ")
        parts.append(current.text.strip())
    return "".join(parts)


def _paragraphs(lines: list[Line], line_height: float) -> list[str]:
    rows = _rows(lines, line_height)
    if not rows:
        return []
    gaps = [min(l.y0 for l in row) - max(l.y1 for l in above) for above, row in zip(rows, rows[1:])]
    usual_gap = max(median(gaps), 0.0) if gaps else 0.0
    blocks: list[list[str]] = [[]]
    for index, row in enumerate(rows):
        if index:
            height = max(l.height for l in row)
            above_height = max(l.height for l in rows[index - 1])
            heading_change = max(height, above_height) > 1.45 * min(height, above_height)
            # a paragraph break is a gap clearly wider than this block's normal line spacing
            wide_gap = gaps[index - 1] > max(1.7 * usual_gap, 0.6 * line_height)
            if wide_gap or heading_change:
                blocks.append([])
        blocks[-1].append(_row_text(row, line_height))
    return ["\n".join(block) for block in blocks if block]
