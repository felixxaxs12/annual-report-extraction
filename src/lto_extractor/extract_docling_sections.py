from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


KEEP_TEXT_LABELS = {
    "section_header",
    "text",
    "list_item",
    "caption",
    "footnote",
}
SKIP_TEXT_LABELS = {
    "page_header",
    "page_footer",
}
CONTENTS_HEADER_RE = re.compile(r"^(contents?|table\s+of\s+contents)$", re.IGNORECASE)

STRATEGIC_START_RE = re.compile(
    r"\b("
    r"strategic\s+rep(?:o|or)t|chairman['’]?\s*s?\s+(statement|review|overview)|"
    r"chief\s+execut\w*['’]?\s*s?\s+(statement|review)|ceo['’]?\s*s?\s+(statement|review)|"
    r"business\s+(model|review|overview)|operational\s+review|operations\s+review|"
    r"financial\s+review|principal\s+risks?|risk\s+management|"
    r"strategy|strategic\s+objectives?|investment\s+review|manager['’]?\s*s?\s+report|"
    r"our\s+vision|long[-\s]+term\s+market\s+forces|market\s+drivers?|"
    r"measuring\s+our\s+progress|our\s+report\s+in\s+brief"
    r")\b",
    re.IGNORECASE,
)
GOVERNANCE_START_RE = re.compile(
    r"^("
    r"corporate\s+governance\s+(statement|report|chairman['’]?\s*s?\s+overview|directors?['’]?\s+report)|"
    r"directors?['’]?\s+statement\s+on\s+corporate\s+governance|"
    r"governance\s+statement"
    r")\b",
    re.IGNORECASE,
)
WEAK_GOVERNANCE_START_RE = re.compile(
    r"\b("
    r"board\s+of\s+directors|directors?['’]?\s+report|"
    r"audit\s+committee\s+report|remuneration\s+report|directors?['’]?\s+remuneration|"
    r"nomination[s]?\s+committee\s+report"
    r")\b",
    re.IGNORECASE,
)
STOP_SECTION_RE = re.compile(
    r"\b("
    r"independent\s+auditors?['’]?\s*s?\s+report|auditors?['’]?\s*s?\s+report|"
    r"financial\s+statements?|consolidated\s+financial\s+statements?|"
    r"notes\s+to\s+(the\s+)?financial\s+statements?|"
    r"notice\s+of\s+(annual\s+)?general\s+meeting|annual\s+general\s+meeting|"
    r"shareholder\s+information|advisers?\s+to\s+the\s+company|registered\s+office|glossary"
    r")\b",
    re.IGNORECASE,
)
URL_RE = re.compile(r"\b(?:https?://|www\.)\S+", re.IGNORECASE)
DOMAIN_SIGNAL_RE = re.compile(
    r"\b("
    r"strateg(?:y|ic)|long\s+term|long-term|governance|board|risk|"
    r"sustainab\w*|responsib\w*|stakeholder|employee|customer|community|"
    r"shareholder|shareholder\s+value|growth|returns?|capital|cash\s+flow|"
    r"business\s+model|principal\s+risks?|future|outlook|objective|policy|"
    r"investment\s+policy|financial\s+review|chairman|chief\s+executive|"
    r"performance|remuneration|committee"
    r")\b",
    re.IGNORECASE,
)
SENTENCE_VERB_RE = re.compile(
    r"\b("
    r"is|are|was|were|be|been|being|has|have|had|will|would|should|could|"
    r"might|must|can|continues?|expects?|believes?|aims?|intends?|"
    r"provides?|supports?|delivers?|focus(?:es)?|plans?|operates?|manages?|"
    r"increases?|increased|decreases?|decreased|grew|grown|improves?|improved|"
    r"reduces?|reduced|expands?|expanded"
    r")\b",
    re.IGNORECASE,
)
MONTH_RE = re.compile(
    r"\b("
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
    r")\b",
    re.IGNORECASE,
)
UNIT_FRAGMENT_RE = re.compile(
    r"^("
    r"[£€$]?\s*[em]?\s?m(?:illion)?|[£€$]\s?million|%|"
    r"year\s+ended\b.*|months?\s+ended\b.*|years?|months?"
    r")$",
    re.IGNORECASE,
)
FRAGMENT_DENSE_MIN_ITEMS = 10
FRAGMENT_DENSE_RATIO = 0.75
METRIC_DENSE_MIN_ITEMS = 5
METRIC_DENSE_RATIO = 0.6
METRIC_DENSE_MAX_SENTENCE_RATIO = 0.25
METRIC_DENSE_MAX_AVG_WORDS = 14
METRIC_UNIT_RE = re.compile(
    r"\b("
    r"us\$|usd|£|€|\$|%|pence|cents?|eps|boepd|bopd|boe|mmboe|mboe|"
    r"bbls?|barrels?|trir|ltif|ltir|ebitda?|pbit|pbt|profit|revenue|"
    r"sales|cash\s+flow|debt|maturity|production|reserves?|replacement\s+ratio|"
    r"operating\s+cost|dividend|nav|total\s+return|net\s+asset"
    r")\b",
    re.IGNORECASE,
)
ANNUAL_REPORT_FOOTER_RE = re.compile(
    r"^\d{1,3}\s+.+\bannual\s+report\b.+\b(19|20)\d{2}\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DoclingElement:
    ref: str
    kind: str
    label: str
    text: str
    page_no: int | None
    bbox: dict[str, Any] | None
    parent_ref: str
    level: int | None = None
    table_rows: list[list[str]] | None = None
    children: tuple["DoclingElement", ...] = ()


@dataclass(frozen=True)
class SectionWindow:
    section_type: str
    start_page: int
    end_page: int
    boundary_method: str


@dataclass(frozen=True)
class TocEntry:
    section_type: str
    title: str
    printed_page: int | None


@dataclass(frozen=True)
class TocHints:
    entries: list[TocEntry]
    contents_pages: set[int]


@dataclass(frozen=True)
class ChunkRecord:
    chunk_id: str
    status: str
    order_index: int
    report_id: str
    company_name: str
    report_year: int | None
    source_json: str
    source_pdf: str
    section_type: str
    content_type: str
    text_md: str
    page_no: int | None
    docling_ref: str
    bbox: dict[str, Any] | None
    heading_path: list[str]
    boundary_method: str


@dataclass(frozen=True)
class ChunkCandidate:
    report_id: str
    company_name: str
    report_year: int | None
    source_json: str
    source_pdf: str
    section_type: str
    content_type: str
    raw_text: str
    text_md: str
    page_no: int | None
    docling_ref: str
    bbox: dict[str, Any] | None
    heading_path: list[str]
    boundary_method: str


@dataclass(frozen=True)
class DroppedChunkRecord:
    drop_id: str
    status: str
    order_index: int
    report_id: str
    company_name: str
    report_year: int | None
    source_json: str
    source_pdf: str
    section_type: str
    content_type: str
    raw_text: str
    text_md: str
    page_no: int | None
    docling_ref: str
    bbox: dict[str, Any] | None
    heading_path: list[str]
    boundary_method: str
    quality_flags: list[str]
    drop_reason: str


@dataclass(frozen=True)
class ExtractionResult:
    markdown: str
    chunks: list[ChunkRecord]
    dropped_chunks: list[DroppedChunkRecord]
    warnings: list[str]
    dropped_picture_count: int
    windows: list[SectionWindow]


def normalize_text(text: str) -> str:
    text = (
        text.replace("’", "'")
        .replace("‘", "'")
        .replace("\u00a0", " ")
        .replace("\u2009", " ")
        .replace("\u202f", " ")
        .replace("\ufb01", "fi")
        .replace("\ufb02", "fl")
    )
    return re.sub(r"\s+", " ", text).strip()


def normalize_block_text(text: str) -> str:
    lines = [normalize_text(line) for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def normalize_for_match(text: str) -> str:
    text = normalize_text(text)
    text = re.sub(r"[._~]{2,}", " ", text)
    text = re.sub(r"[^A-Za-z0-9' ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def object_ref(kind: str, index: int) -> str:
    return f"#/{kind}/{index}"


def parent_ref(item: dict[str, Any]) -> str:
    return str(item.get("parent", {}).get("$ref", ""))


def first_page_no(item: dict[str, Any]) -> int | None:
    prov = item.get("prov") or []
    if not prov:
        return None
    page = prov[0].get("page_no")
    return int(page) if isinstance(page, int) else None


def first_bbox(item: dict[str, Any]) -> dict[str, Any] | None:
    prov = item.get("prov") or []
    if not prov:
        return None
    bbox = prov[0].get("bbox")
    return bbox if isinstance(bbox, dict) else None


def ref_index(ref: str) -> tuple[str, int] | None:
    match = re.fullmatch(r"#/(texts|tables|pictures|groups)/(\d+)", ref)
    if not match:
        return None
    return match.group(1), int(match.group(2))


def resolve_ref(document: dict[str, Any], ref: str) -> dict[str, Any] | None:
    parsed = ref_index(ref)
    if parsed is None:
        return None
    collection, index = parsed
    items = document.get(collection, [])
    if not isinstance(items, list) or index >= len(items):
        return None
    return items[index]


def document_index_pages(document: dict[str, Any]) -> set[int]:
    pages: set[int] = set()
    for table in document.get("tables", []):
        if table.get("label") != "document_index":
            continue
        page = first_page_no(table)
        if page is not None:
            pages.add(page)
    return pages


def document_page_numbers(document: dict[str, Any]) -> list[int]:
    return sorted(int(page) for page in document.get("pages", {}) if str(page).isdigit())


def content_page_candidates(document: dict[str, Any]) -> set[int]:
    pages = document_index_pages(document)
    toc_like_counts: dict[int, int] = {}
    toc_like_groups: dict[int, set[str]] = {}
    for item in document.get("texts", []):
        page = first_page_no(item)
        if page is None or page > 12:
            continue
        label = str(item.get("label", ""))
        text = normalize_for_match(str(item.get("text", "")))
        if CONTENTS_HEADER_RE.fullmatch(text):
            pages.add(page)
        title, printed_page = parse_toc_line(str(item.get("text", "")))
        section_type = classify_toc_title(title)
        if printed_page is not None or label == "list_item":
            toc_like_counts[page] = toc_like_counts.get(page, 0) + 1
        if section_type is not None:
            toc_like_groups.setdefault(page, set()).add(section_type)
    for page, count in toc_like_counts.items():
        if count >= 4 and len(toc_like_groups.get(page, set())) >= 2:
            pages.add(page)
    return pages


def table_rows(table: dict[str, Any]) -> list[list[str]]:
    grid = table.get("data", {}).get("grid", [])
    rows: list[list[str]] = []
    for row in grid:
        cells = [normalize_text(str((cell or {}).get("text", ""))) for cell in row]
        if any(cells):
            rows.append(cells)
    return rows


def parse_toc_line(text: str) -> tuple[str, int | None]:
    text = normalize_text(text)
    text = re.sub(r"[._~]{2,}", " ", text).strip()
    if not text:
        return "", None

    match = re.fullmatch(r"(?P<page>\d{1,3})\s+(?P<title>[A-Za-z].*)", text)
    if match:
        return normalize_text(match.group("title")), int(match.group("page"))

    match = re.fullmatch(r"(?P<title>.*?[A-Za-z][^0-9]*?)\s+(?P<page>\d{1,3})", text)
    if match:
        return normalize_text(match.group("title")), int(match.group("page"))

    if re.fullmatch(r"\d{1,3}", text):
        return "", int(text)
    return text, None


def parse_toc_row(cells: list[str]) -> tuple[str, int | None]:
    titles: list[str] = []
    printed_page: int | None = None
    for cell in cells:
        title, page = parse_toc_line(cell)
        if title:
            titles.append(title)
        if page is not None:
            printed_page = page
    if not titles:
        return "", printed_page
    return normalize_text(" ".join(titles)), printed_page


def is_stop_text(text: str) -> bool:
    text = normalize_for_match(text)
    if re.search(r"\bfinancial\s+review\b", text):
        return False
    return bool(STOP_SECTION_RE.search(text))


def classify_toc_title(title: str, current_section: str | None = None) -> str | None:
    text = normalize_for_match(title)
    if not text or text in {"contents", "table of contents"}:
        return None
    if is_stop_text(text):
        return "stop"
    if GOVERNANCE_START_RE.search(text) or WEAK_GOVERNANCE_START_RE.search(text):
        return "governance"
    if re.search(r"\bgovernance\b", text):
        return "governance"
    if STRATEGIC_START_RE.search(text):
        return "strategic_report"
    return current_section


def looks_like_toc_entry(label: str, text: str, printed_page: int | None) -> bool:
    if printed_page is not None:
        return True
    if label == "list_item":
        return True
    return bool(re.search(r"^\d{1,3}\s+\S|\S\s+\d{1,3}$", normalize_text(text)))


def usable_toc_title(title: str) -> bool:
    text = normalize_for_match(title)
    if not text or text in {"contents", "table of contents"}:
        return False
    if re.fullmatch(r"\d{1,3}", text):
        return False
    return len(text) >= 3


def extract_toc_hints(document: dict[str, Any]) -> TocHints:
    contents_pages = content_page_candidates(document)
    entries: list[TocEntry] = []

    def add_entry(section_type: str | None, title: str, printed_page: int | None) -> None:
        if section_type is None or not usable_toc_title(title):
            return
        entries.append(TocEntry(section_type=section_type, title=title, printed_page=printed_page))

    for table in document.get("tables", []):
        if table.get("label") != "document_index":
            continue
        current_section: str | None = None
        for row in table_rows(table):
            title, printed_page = parse_toc_row(row)
            explicit_section = classify_toc_title(title)
            if explicit_section is not None:
                current_section = explicit_section
            section_type = explicit_section or current_section
            add_entry(section_type, title, printed_page)

    current_by_page: dict[int, str | None] = {}
    for item in document.get("texts", []):
        page = first_page_no(item)
        if page not in contents_pages:
            continue
        label = str(item.get("label", ""))
        if label not in KEEP_TEXT_LABELS and label not in {"page_header", "page_footer"}:
            continue
        raw_text = str(item.get("text", ""))
        title, printed_page = parse_toc_line(raw_text)
        if not title:
            continue
        current_section = current_by_page.get(page)
        explicit_section = classify_toc_title(title)
        if explicit_section is not None:
            current_section = explicit_section
            current_by_page[page] = current_section
            add_entry(current_section, title, printed_page)
            continue
        if looks_like_toc_entry(label, raw_text, printed_page):
            add_entry(current_section, title, printed_page)

    return TocHints(entries=entries, contents_pages=contents_pages)


def build_text_element(ref: str, item: dict[str, Any]) -> DoclingElement | None:
    label = str(item.get("label", ""))
    text = normalize_text(str(item.get("text", "")))
    if not text or label in SKIP_TEXT_LABELS:
        return None
    if label not in KEEP_TEXT_LABELS:
        return None
    if parent_ref(item).startswith("#/pictures/"):
        return None
    return DoclingElement(
        ref=ref,
        kind="text",
        label=label,
        text=text,
        page_no=first_page_no(item),
        bbox=first_bbox(item),
        parent_ref=parent_ref(item),
        level=item.get("level") if isinstance(item.get("level"), int) else None,
    )


def build_table_element(ref: str, item: dict[str, Any]) -> DoclingElement | None:
    if item.get("label") == "document_index":
        return None
    if parent_ref(item).startswith("#/pictures/"):
        return None
    rows = table_rows(item)
    if not rows:
        return None
    return DoclingElement(
        ref=ref,
        kind="table",
        label=str(item.get("label", "table")),
        text="",
        page_no=first_page_no(item),
        bbox=first_bbox(item),
        parent_ref=parent_ref(item),
        table_rows=rows,
    )


def group_text(children: list[DoclingElement]) -> str:
    parts: list[str] = []
    for child in children:
        if child.kind == "table":
            parts.append(markdown_table(child.table_rows or []))
        else:
            parts.append(child.text)
    return normalize_block_text("\n".join(part for part in parts if part))


def first_child_page(children: list[DoclingElement]) -> int | None:
    for child in children:
        if child.page_no is not None:
            return child.page_no
    return None


def first_child_bbox(children: list[DoclingElement]) -> dict[str, Any] | None:
    for child in children:
        if child.bbox is not None:
            return child.bbox
    return None


def content_elements(document: dict[str, Any]) -> tuple[list[DoclingElement], int]:
    elements: list[DoclingElement] = []
    dropped_pictures = 0

    def build_from_ref(ref: str) -> DoclingElement | None:
        nonlocal dropped_pictures
        parsed = ref_index(ref)
        item = resolve_ref(document, ref)
        if parsed is None or item is None:
            return None
        collection, _ = parsed

        if collection == "pictures":
            dropped_pictures += 1
            return None
        if collection == "groups":
            children: list[DoclingElement] = []
            for child in item.get("children", []):
                child_ref = child.get("$ref")
                if child_ref:
                    child_element = build_from_ref(str(child_ref))
                    if child_element is not None:
                        children.append(child_element)
            text = group_text(children)
            if not text:
                return None
            return DoclingElement(
                ref=ref,
                kind="group",
                label=str(item.get("label", "group")),
                text=text,
                page_no=first_page_no(item) or first_child_page(children),
                bbox=first_bbox(item) or first_child_bbox(children),
                parent_ref=parent_ref(item),
                children=tuple(children),
            )
        if collection == "texts":
            return build_text_element(ref, item)
        if collection == "tables":
            return build_table_element(ref, item)
        return None

    for child in document.get("body", {}).get("children", []):
        child_ref = child.get("$ref")
        if child_ref:
            element = build_from_ref(str(child_ref))
            if element is not None:
                elements.append(element)
    return elements, dropped_pictures


def boundary_markers(document: dict[str, Any]) -> list[DoclingElement]:
    markers: list[DoclingElement] = []
    for index, item in enumerate(document.get("texts", [])):
        label = str(item.get("label", ""))
        if label not in {"section_header", "page_header", "page_footer"}:
            continue
        text = normalize_text(str(item.get("text", "")))
        if not text:
            continue
        markers.append(
            DoclingElement(
                ref=object_ref("texts", index),
                kind="text",
                label=label,
                text=text,
                page_no=first_page_no(item),
                bbox=first_bbox(item),
                parent_ref=parent_ref(item),
                level=item.get("level") if isinstance(item.get("level"), int) else None,
            )
        )
    return markers


def is_strategic_marker(marker: DoclingElement) -> bool:
    return bool(STRATEGIC_START_RE.search(normalize_for_match(marker.text)))


def is_strong_governance_marker(marker: DoclingElement) -> bool:
    text = normalize_for_match(marker.text)
    return marker.label == "section_header" and (
        text == "governance" or bool(GOVERNANCE_START_RE.search(text))
    )


def is_weak_governance_marker(marker: DoclingElement) -> bool:
    return bool(WEAK_GOVERNANCE_START_RE.search(normalize_for_match(marker.text)))


def is_stop_marker(marker: DoclingElement) -> bool:
    return marker.label == "section_header" and is_stop_text(marker.text)


def first_marker_page(
    markers: list[DoclingElement],
    predicate,
    after_page: int = 0,
    before_page: int | None = None,
) -> int | None:
    for marker in markers:
        if marker.page_no is None or marker.page_no <= after_page:
            continue
        if before_page is not None and marker.page_no >= before_page:
            continue
        if predicate(marker):
            return marker.page_no
    return None


def toc_entries_for(toc_hints: TocHints, section_type: str) -> list[TocEntry]:
    return [entry for entry in toc_hints.entries if entry.section_type == section_type]


def title_match_tokens(text: str) -> set[str]:
    stop_words = {
        "the",
        "and",
        "for",
        "with",
        "our",
        "your",
        "section",
    }
    return {
        token
        for token in re.findall(r"[a-z0-9]+", normalize_for_match(text))
        if len(token) >= 4 and token not in stop_words
    }


def title_matches_marker(title: str, marker_text: str) -> bool:
    title_text = normalize_for_match(title)
    marker_text = normalize_for_match(marker_text)
    if not title_text or not marker_text:
        return False
    if title_text == marker_text:
        return True
    if title_text in {"governance", "strategic report", "financial statements"}:
        return marker_text == title_text
    title_tokens = title_match_tokens(title_text)
    marker_tokens = title_match_tokens(marker_text)

    if len(title_text) >= 8 and marker_text.startswith(title_text) and len(title_tokens) >= 2:
        return True
    if len(marker_text) >= 8 and title_text.startswith(marker_text) and len(marker_tokens) >= 3:
        return True

    if not title_tokens or not marker_tokens:
        return False
    overlap = len(title_tokens & marker_tokens)
    required_overlap = max(2, (len(title_tokens) * 3 + 3) // 4)
    return overlap >= required_overlap


def first_toc_heading_page(
    markers: list[DoclingElement],
    toc_hints: TocHints,
    section_type: str,
    after_page: int = 0,
    before_page: int | None = None,
) -> int | None:
    entries = toc_entries_for(toc_hints, section_type)
    for marker in markers:
        if marker.page_no is None or marker.page_no <= after_page:
            continue
        if before_page is not None and marker.page_no >= before_page:
            continue
        if any(title_matches_marker(entry.title, marker.text) for entry in entries):
            return marker.page_no
    return None


def inferred_toc_offset(markers: list[DoclingElement], toc_hints: TocHints) -> int | None:
    offsets: dict[int, int] = {}
    for entry in toc_hints.entries:
        if entry.printed_page is None:
            continue
        for marker in markers:
            if marker.page_no is None:
                continue
            if title_matches_marker(entry.title, marker.text):
                offset = marker.page_no - entry.printed_page
                offsets[offset] = offsets.get(offset, 0) + 1
                break
    if not offsets:
        return None
    return max(offsets.items(), key=lambda item: (item[1], -abs(item[0])))[0]


def first_toc_printed_page(
    toc_hints: TocHints,
    section_type: str,
    offset: int | None,
    valid_pages: set[int],
    after_page: int = 0,
    before_page: int | None = None,
) -> int | None:
    if offset is None:
        return None
    candidates: list[int] = []
    for entry in toc_entries_for(toc_hints, section_type):
        if entry.printed_page is None:
            continue
        page = entry.printed_page + offset
        if page <= after_page:
            continue
        if before_page is not None and page >= before_page:
            continue
        if valid_pages and page not in valid_pages:
            continue
        candidates.append(page)
    return min(candidates) if candidates else None


def first_existing_page_after(document: dict[str, Any], after_page: int) -> int:
    page_numbers = document_page_numbers(document)
    return max(page_numbers) + 1 if page_numbers else after_page + 1


def min_existing_page(*pages: int | None) -> int | None:
    real_pages = [page for page in pages if page is not None]
    return min(real_pages) if real_pages else None


def boundary_method(base: str, all_markers: list[DoclingElement], section_type: str, page_no: int) -> str:
    if section_type == "strategic_report":
        pattern = re.compile(r"\bstrategic\s+report\b", re.IGNORECASE)
    elif section_type == "governance":
        pattern = re.compile(r"\b(corporate\s+)?governance\b", re.IGNORECASE)
    else:
        pattern = re.compile(r"$^")
    has_header_footer_signal = any(
        marker.page_no == page_no
        and marker.label in {"page_header", "page_footer"}
        and pattern.search(normalize_for_match(marker.text))
        for marker in all_markers
    )
    return f"{base}+header_footer" if has_header_footer_signal else base


def find_section_windows(document: dict[str, Any]) -> tuple[list[SectionWindow], list[str]]:
    toc_hints = extract_toc_hints(document)
    skip_pages = document_index_pages(document) | toc_hints.contents_pages
    all_markers = boundary_markers(document)
    markers = [
        marker
        for marker in all_markers
        if marker.page_no not in skip_pages and marker.label == "section_header"
    ]
    valid_pages = set(document_page_numbers(document))
    toc_offset = inferred_toc_offset(markers, toc_hints)
    warnings: list[str] = []

    strategic_toc_page = first_toc_printed_page(
        toc_hints, "strategic_report", toc_offset, valid_pages
    )
    strategic_heading_page = first_toc_heading_page(markers, toc_hints, "strategic_report")
    strategic_regex_page = first_marker_page(markers, is_strategic_marker)
    strategic_start = min_existing_page(strategic_toc_page, strategic_heading_page, strategic_regex_page)
    if strategic_start is None:
        return [], ["strategic_start_not_found"]

    stop_toc_page = first_toc_printed_page(
        toc_hints, "stop", toc_offset, valid_pages, after_page=strategic_start
    )
    stop_heading_page = first_marker_page(markers, is_stop_marker, after_page=strategic_start)
    stop_page = min_existing_page(stop_toc_page, stop_heading_page)
    if stop_page is None:
        stop_page = first_existing_page_after(document, strategic_start)
        warnings.append("stop_section_not_found")

    governance_toc_page = first_toc_printed_page(
        toc_hints,
        "governance",
        toc_offset,
        valid_pages,
        after_page=strategic_start,
        before_page=stop_page,
    )
    governance_heading_page = first_toc_heading_page(
        markers,
        toc_hints,
        "governance",
        after_page=strategic_start,
        before_page=stop_page,
    )
    governance_start = min_existing_page(governance_toc_page, governance_heading_page)
    if governance_start is None:
        governance_start = first_marker_page(
            markers,
            is_strong_governance_marker,
            after_page=strategic_start,
            before_page=stop_page,
        )
    if governance_start is None:
        governance_start = first_marker_page(
            markers,
            is_weak_governance_marker,
            after_page=strategic_start,
            before_page=stop_page,
        )
        if governance_start is not None:
            warnings.append("governance_start_used_weak_marker")

    if toc_offset is None and toc_hints.entries:
        warnings.append("toc_page_offset_not_inferred")

    windows: list[SectionWindow] = []
    if governance_start is None:
        windows.append(
            SectionWindow(
                section_type="strategic_report",
                start_page=strategic_start,
                end_page=max(strategic_start, stop_page - 1),
                boundary_method=boundary_method(
                    "toc_or_heading", all_markers, "strategic_report", strategic_start
                ),
            )
        )
        warnings.append("governance_start_not_found")
        return windows, warnings

    if strategic_start < governance_start:
        windows.append(
            SectionWindow(
                section_type="strategic_report",
                start_page=strategic_start,
                end_page=governance_start - 1,
                boundary_method=boundary_method(
                    "toc_or_heading", all_markers, "strategic_report", strategic_start
                ),
            )
        )
    windows.append(
        SectionWindow(
            section_type="governance",
            start_page=governance_start,
            end_page=max(governance_start, stop_page - 1),
            boundary_method=boundary_method(
                "toc_or_heading", all_markers, "governance", governance_start
            ),
        )
    )
    return windows, warnings


def section_for_page(page_no: int | None, windows: list[SectionWindow]) -> SectionWindow | None:
    if page_no is None:
        return None
    for window in windows:
        if window.start_page <= page_no <= window.end_page:
            return window
    return None


def text_tokens(text: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z'-]*|\d+(?:\.\d+)?%?", normalize_text(text))


def word_count(text: str) -> int:
    return len(text_tokens(text))


def has_domain_signal(text: str) -> bool:
    return bool(DOMAIN_SIGNAL_RE.search(normalize_for_match(text)))


def has_sentence_signal(text: str) -> bool:
    text = normalize_text(text)
    if re.search(r"[!?;]", text):
        return word_count(text) >= 6
    if re.search(r"[A-Za-z][.](?:\s|$)", text):
        return word_count(text) >= 6
    return bool(SENTENCE_VERB_RE.search(text)) and word_count(text) >= 4


def is_numeric_fragment(text: str) -> bool:
    text = normalize_text(text)
    if not re.search(r"\d", text):
        return False
    if word_count(text) > 4:
        return False
    return bool(re.fullmatch(r"[\s\d,().+\-:£€$%]+", text))


def is_date_fragment(text: str) -> bool:
    text = normalize_text(text)
    if word_count(text) > 7 or has_sentence_signal(text):
        return False
    if re.fullmatch(r"(?:19|20)\d{2}", text):
        return True
    return bool(MONTH_RE.search(text))


def is_unit_or_period_fragment(text: str) -> bool:
    return bool(UNIT_FRAGMENT_RE.fullmatch(normalize_text(text)))


def is_annual_report_footer(text: str) -> bool:
    return bool(ANNUAL_REPORT_FOOTER_RE.search(normalize_text(text)))


def has_metric_signal(text: str) -> bool:
    text = normalize_text(text)
    return bool(re.search(r"\d", text)) and bool(
        METRIC_UNIT_RE.search(text) or re.search(r"[£€$%]", text)
    )


def is_metric_dense_line(candidate: ChunkCandidate) -> bool:
    if candidate.content_type not in {"paragraph", "list_item"}:
        return False
    text = normalize_text(candidate.raw_text)
    words = word_count(text)
    if words == 0 or words > 18:
        return False
    if has_sentence_signal(text):
        return False
    return has_metric_signal(text)


def is_metric_context_line(candidate: ChunkCandidate) -> bool:
    if candidate.content_type not in {"paragraph", "list_item"}:
        return False
    text = normalize_text(candidate.raw_text)
    if not text or has_sentence_signal(text):
        return False
    return word_count(text) <= 5


def fragment_reasons(candidate: ChunkCandidate) -> list[str]:
    if candidate.content_type in {"heading", "table"}:
        return []
    if candidate.content_type == "group":
        return []

    text = normalize_text(candidate.raw_text)
    if not text:
        return ["empty_text"]
    if is_annual_report_footer(text):
        return ["annual_report_page_footer"]

    words = word_count(text)
    if words >= 8 or has_sentence_signal(text):
        return []
    if has_domain_signal(text) and words >= 2:
        return []

    reasons: list[str] = []
    if URL_RE.search(text) and words <= 10:
        reasons.append("standalone_url")
    if text.endswith(":") and words <= 4 and not has_domain_signal(text):
        reasons.append("short_label")
    if is_numeric_fragment(text):
        reasons.append("standalone_numeric_value")
    if is_unit_or_period_fragment(text):
        reasons.append("standalone_unit_or_period")

    return sorted(set(reasons))


CONTEXT_RESCUABLE_FLAGS = {
    "standalone_numeric_value",
}
CONTEXT_WINDOW = 5


def is_probable_page_number(text: str) -> bool:
    return bool(re.fullmatch(r"\d{1,3}", normalize_text(text)))


def has_neighbor_context_signal(candidate: ChunkCandidate) -> bool:
    if candidate.content_type in {"heading", "table", "group"}:
        return False

    text = normalize_text(candidate.raw_text)
    if not text or is_annual_report_footer(text):
        return False
    if has_sentence_signal(text) or has_domain_signal(text):
        return True
    if is_numeric_fragment(text) or is_unit_or_period_fragment(text):
        return False
    return word_count(text) >= 4


def close_in_reading_order(
    left_index: int,
    right_index: int,
    left: ChunkCandidate,
    right: ChunkCandidate,
) -> bool:
    return (
        left.section_type == right.section_type
        and left.page_no == right.page_no
        and abs(left_index - right_index) <= CONTEXT_WINDOW
    )


def has_contextual_neighbor(index: int, candidates: list[ChunkCandidate]) -> bool:
    candidate = candidates[index]
    for neighbor_index, neighbor in enumerate(candidates):
        if neighbor_index == index:
            continue
        if not close_in_reading_order(index, neighbor_index, candidate, neighbor):
            continue
        if has_neighbor_context_signal(neighbor):
            return True
    return False


def apply_contextual_noise_rescue(
    candidates: list[ChunkCandidate],
    reasons_by_index: dict[int, list[str]],
) -> dict[int, list[str]]:
    adjusted: dict[int, list[str]] = {}
    for index, reasons in reasons_by_index.items():
        if not reasons:
            adjusted[index] = []
            continue

        candidate = candidates[index]
        if (
            CONTEXT_RESCUABLE_FLAGS.intersection(reasons)
            and not is_probable_page_number(candidate.raw_text)
            and has_contextual_neighbor(index, candidates)
        ):
            adjusted[index] = [
                reason for reason in reasons if reason not in CONTEXT_RESCUABLE_FLAGS
            ]
        else:
            adjusted[index] = reasons
    return adjusted


def is_metric_dense_window(indices: list[int], candidates: list[ChunkCandidate]) -> bool:
    metric_count = sum(1 for index in indices if is_metric_dense_line(candidates[index]))
    sentence_count = sum(
        1 for index in indices if has_sentence_signal(candidates[index].raw_text)
    )
    avg_words = sum(word_count(candidates[index].raw_text) for index in indices) / len(indices)
    return (
        metric_count / len(indices) >= METRIC_DENSE_RATIO
        and sentence_count / len(indices) <= METRIC_DENSE_MAX_SENTENCE_RATIO
        and avg_words <= METRIC_DENSE_MAX_AVG_WORDS
    )


def can_expand_metric_dense_block(candidate: ChunkCandidate) -> bool:
    return (
        is_metric_dense_line(candidate)
        or is_metric_context_line(candidate)
        or is_annual_report_footer(candidate.raw_text)
    )


def metric_dense_block_indices(
    indices: list[int], candidates: list[ChunkCandidate]
) -> set[int]:
    if len(indices) < METRIC_DENSE_MIN_ITEMS:
        return set()

    dense_indices: set[int] = set()
    for start in range(0, len(indices) - METRIC_DENSE_MIN_ITEMS + 1):
        window = indices[start : start + METRIC_DENSE_MIN_ITEMS]
        if is_metric_dense_window(window, candidates):
            dense_indices.update(window)

    if not dense_indices:
        return set()

    positions = {index: position for position, index in enumerate(indices)}
    expanded = set(dense_indices)
    for index in list(dense_indices):
        position = positions[index]
        left = position - 1
        while left >= 0 and indices[left] not in expanded:
            candidate = candidates[indices[left]]
            if not can_expand_metric_dense_block(candidate):
                break
            expanded.add(indices[left])
            left -= 1

        right = position + 1
        while right < len(indices) and indices[right] not in expanded:
            candidate = candidates[indices[right]]
            if not can_expand_metric_dense_block(candidate):
                break
            expanded.add(indices[right])
            right += 1

    return expanded


def add_dense_block_flags(
    candidates: list[ChunkCandidate], reasons_by_index: dict[int, list[str]]
) -> dict[int, list[str]]:
    flagged = {index: list(reasons) for index, reasons in reasons_by_index.items()}
    page_text_groups: dict[tuple[str, int | None], list[int]] = {}
    page_all_groups: dict[tuple[str, int | None], list[int]] = {}
    for index, candidate in enumerate(candidates):
        page_key = (candidate.section_type, candidate.page_no)
        if candidate.content_type != "table":
            page_all_groups.setdefault(page_key, []).append(index)
        if candidate.content_type in {"heading", "table"}:
            continue
        page_text_groups.setdefault(page_key, []).append(index)

    for indices in page_text_groups.values():
        if len(indices) < FRAGMENT_DENSE_MIN_ITEMS:
            continue
        fragment_count = sum(1 for index in indices if flagged[index])
        if fragment_count / len(indices) < FRAGMENT_DENSE_RATIO:
            continue
        for index in indices:
            if flagged[index]:
                flagged[index] = sorted(set(flagged[index] + ["fragment_dense_block"]))

    for indices in page_text_groups.values():
        for index in metric_dense_block_indices(indices, candidates):
            flagged[index] = sorted(set(flagged[index] + ["metric_dense_block"]))

    for indices in page_all_groups.values():
        if not any("metric_dense_block" in flagged[index] for index in indices):
            continue
        non_heading_indices = [
            index for index in indices if candidates[index].content_type != "heading"
        ]
        if not non_heading_indices:
            continue
        if all(flagged[index] for index in non_heading_indices):
            for index in indices:
                if candidates[index].content_type == "heading":
                    flagged[index] = sorted(set(flagged[index] + ["metric_dense_heading"]))
    return flagged


def markdown_table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]

    def clean_cell(cell: str) -> str:
        return normalize_text(cell).replace("|", "\\|")

    header = padded[0]
    lines = [
        "| " + " | ".join(clean_cell(cell) for cell in header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for row in padded[1:]:
        lines.append("| " + " | ".join(clean_cell(cell) for cell in row) + " |")
    return "\n".join(lines)


def element_to_markdown(element: DoclingElement) -> tuple[str, str]:
    if element.kind == "table":
        return "table", "[Table]\n\n" + markdown_table(element.table_rows or [])
    if element.kind == "group":
        return "group", element.text
    if element.label == "section_header":
        return "heading", f"### {element.text}"
    if element.label == "list_item":
        return "list_item", f"- {element.text}"
    return "paragraph", element.text


def infer_company_and_year(document_name: str, source_pdf: str) -> tuple[str, int | None]:
    name = Path(source_pdf or document_name).stem
    year_match = re.search(r"((?:19|20)\d{2})(?:\d{4})?$", name)
    year = int(year_match.group(1)) if year_match else None
    company = re.sub(r"^(Non-Stopper|Stopper)(?:_icb\d+)?_", "", name)
    company = re.sub(r"_(?:19|20)\d{6}$", "", company)
    return company, year


def chunk_candidates(
    document: dict[str, Any],
    source_json: Path,
    elements: list[DoclingElement],
    windows: list[SectionWindow],
    skip_pages: set[int],
) -> list[ChunkCandidate]:
    origin = document.get("origin", {})
    source_pdf = str(origin.get("filename", ""))
    report_id = str(document.get("name") or source_json.stem)
    company_name, report_year = infer_company_and_year(report_id, source_pdf)
    heading_by_section: dict[str, list[str]] = {}
    candidates: list[ChunkCandidate] = []

    for element in elements:
        if element.page_no in skip_pages:
            continue
        window = section_for_page(element.page_no, windows)
        if window is None:
            continue

        content_type, text_md = element_to_markdown(element)
        if not text_md:
            continue
        if content_type == "heading":
            heading_by_section[window.section_type] = [element.text]
        heading_path = list(heading_by_section.get(window.section_type, []))

        candidates.append(
            ChunkCandidate(
                report_id=report_id,
                company_name=company_name,
                report_year=report_year,
                source_json=source_json.name,
                source_pdf=source_pdf,
                section_type=window.section_type,
                content_type=content_type,
                raw_text=element.text,
                text_md=text_md,
                page_no=element.page_no,
                docling_ref=element.ref,
                bbox=element.bbox,
                heading_path=heading_path,
                boundary_method=window.boundary_method,
            )
        )
    return candidates


def to_chunk_record(candidate: ChunkCandidate, chunk_id: str, order_index: int) -> ChunkRecord:
    return ChunkRecord(
        chunk_id=chunk_id,
        status="kept",
        order_index=order_index,
        report_id=candidate.report_id,
        company_name=candidate.company_name,
        report_year=candidate.report_year,
        source_json=candidate.source_json,
        source_pdf=candidate.source_pdf,
        section_type=candidate.section_type,
        content_type=candidate.content_type,
        text_md=candidate.text_md,
        page_no=candidate.page_no,
        docling_ref=candidate.docling_ref,
        bbox=candidate.bbox,
        heading_path=candidate.heading_path,
        boundary_method=candidate.boundary_method,
    )


def to_dropped_chunk_record(
    candidate: ChunkCandidate,
    drop_id: str,
    order_index: int,
    quality_flags: list[str],
) -> DroppedChunkRecord:
    return DroppedChunkRecord(
        drop_id=drop_id,
        status="delete",
        order_index=order_index,
        report_id=candidate.report_id,
        company_name=candidate.company_name,
        report_year=candidate.report_year,
        source_json=candidate.source_json,
        source_pdf=candidate.source_pdf,
        section_type=candidate.section_type,
        content_type=candidate.content_type,
        raw_text=candidate.raw_text,
        text_md=candidate.text_md,
        page_no=candidate.page_no,
        docling_ref=candidate.docling_ref,
        bbox=candidate.bbox,
        heading_path=candidate.heading_path,
        boundary_method=candidate.boundary_method,
        quality_flags=quality_flags,
        drop_reason=quality_flags[0] if quality_flags else "unknown",
    )


def chunk_records(candidates: list[ChunkCandidate]) -> tuple[list[ChunkRecord], list[DroppedChunkRecord]]:
    reasons_by_index = {
        index: fragment_reasons(candidate)
        for index, candidate in enumerate(candidates)
    }
    reasons_by_index = apply_contextual_noise_rescue(candidates, reasons_by_index)
    chunk_counters: dict[str, int] = {}
    drop_counter = 0
    chunks: list[ChunkRecord] = []
    dropped_chunks: list[DroppedChunkRecord] = []

    for index, candidate in enumerate(candidates):
        quality_flags = reasons_by_index[index]
        if quality_flags:
            drop_counter += 1
            drop_id = f"{candidate.report_id}_dropped_{drop_counter:05d}"
            dropped_chunks.append(to_dropped_chunk_record(candidate, drop_id, index, quality_flags))
            continue

        chunk_counters[candidate.section_type] = chunk_counters.get(candidate.section_type, 0) + 1
        chunk_id = f"{candidate.section_type}_{chunk_counters[candidate.section_type]:05d}"
        chunks.append(to_chunk_record(candidate, chunk_id, index))

    return chunks, dropped_chunks


def render_markdown(document: dict[str, Any], chunks: list[ChunkRecord], warnings: list[str]) -> str:
    origin = document.get("origin", {})
    source_pdf = str(origin.get("filename", ""))
    report_id = str(document.get("name") or Path(source_pdf).stem)
    sections = [
        ("strategic_report", "Strategic Report"),
        ("governance", "Governance"),
    ]

    lines = [
        f"# LTO Input - {report_id}",
        "",
        f"*Source PDF: {source_pdf or 'unknown'}*",
        f"*Included sections: Strategic Report; Governance*",
        "*Excluded sections: Financial Statements; Independent Auditor; AGM / shareholder administration*",
        f"*Warnings: {';'.join(warnings) if warnings else 'none'}*",
        "",
        "---",
        "",
    ]

    for section_type, title in sections:
        section_chunks = [chunk for chunk in chunks if chunk.section_type == section_type]
        if not section_chunks:
            continue
        lines.extend([f"# {title}", ""])
        current_page: int | None = None
        for chunk in section_chunks:
            if chunk.page_no != current_page:
                current_page = chunk.page_no
                lines.extend([f"<!-- pdf_page: {current_page} -->", "", f"## PDF page {current_page}", ""])
            lines.extend([chunk.text_md, ""])
        lines.extend(["---", ""])
    return "\n".join(lines).strip() + "\n"


def extract_docling_document(document: dict[str, Any], source_json: Path) -> ExtractionResult:
    windows, warnings = find_section_windows(document)
    elements, dropped_picture_count = content_elements(document)
    toc_hints = extract_toc_hints(document)
    candidates = chunk_candidates(
        document=document,
        source_json=source_json,
        elements=elements,
        windows=windows,
        skip_pages=document_index_pages(document) | toc_hints.contents_pages,
    )
    chunks, dropped_chunks = chunk_records(candidates)
    markdown = render_markdown(document, chunks, warnings)
    return ExtractionResult(
        markdown=markdown,
        chunks=chunks,
        dropped_chunks=dropped_chunks,
        warnings=warnings,
        dropped_picture_count=dropped_picture_count,
        windows=windows,
    )


def chunk_json(chunk: ChunkRecord) -> dict[str, Any]:
    return {
        "chunk_id": chunk.chunk_id,
        "status": chunk.status,
        "content_type": chunk.content_type,
        "page_no": chunk.page_no,
        "heading_path": chunk.heading_path,
        "text_md": chunk.text_md,
    }


def deleted_chunk_json(chunk: DroppedChunkRecord) -> dict[str, Any]:
    return {
        "chunk_id": chunk.drop_id,
        "status": chunk.status,
        "content_type": chunk.content_type,
        "page_no": chunk.page_no,
        "heading_path": chunk.heading_path,
        "text_md": chunk.text_md,
    }


def canonical_document_json(document: dict[str, Any], result: ExtractionResult) -> dict[str, Any]:
    origin = document.get("origin", {})
    source_pdf = str(origin.get("filename", ""))

    sections: list[dict[str, Any]] = []
    for window in result.windows:
        section_chunks = [chunk for chunk in result.chunks if chunk.section_type == window.section_type]
        deleted_chunks = [
            chunk for chunk in result.dropped_chunks if chunk.section_type == window.section_type
        ]
        all_chunks = sorted(
            [chunk_json(chunk) | {"_order_index": chunk.order_index} for chunk in section_chunks]
            + [
                deleted_chunk_json(chunk) | {"_order_index": chunk.order_index}
                for chunk in deleted_chunks
            ],
            key=lambda chunk: chunk["_order_index"],
        )
        for chunk in all_chunks:
            del chunk["_order_index"]
        sections.append(
            {
                "section_type": window.section_type,
                "start_page": window.start_page,
                "end_page": window.end_page,
                "chunks": all_chunks,
            }
        )

    return {
        "schema_version": "docling_lto_document_v2",
        "source_pdf": source_pdf,
        "sections": sections,
    }


def process_json_file(json_path: Path, output_dir: Path) -> dict[str, Any]:
    document = json.loads(json_path.read_text(encoding="utf-8"))
    result = extract_docling_document(document, json_path)
    output_dir.mkdir(parents=True, exist_ok=True)

    stem = json_path.stem.removesuffix(".docling")
    md_path = output_dir / f"{stem}_lto_input.md"
    document_json_path = output_dir / f"{stem}_lto_document.json"
    md_path.write_text(result.markdown, encoding="utf-8")
    document_json_path.write_text(
        json.dumps(canonical_document_json(document, result), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return {
        "source_json": json_path.name,
        "markdown_file": md_path.name,
        "json_file": document_json_path.name,
        "chunks": len(result.chunks),
        "filtered_fragments": len(result.dropped_chunks),
        "warnings": ";".join(result.warnings) if result.warnings else "none",
        "dropped_pictures": result.dropped_picture_count,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract Strategic Report and Governance sections from Docling JSON into Markdown and JSON."
    )
    parser.add_argument("--input-dir", required=True, help="Directory containing *.docling.json files.")
    parser.add_argument("--output-dir", required=True, help="Directory for Markdown and JSON outputs.")
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    rows = [process_json_file(path, output_dir) for path in sorted(input_dir.glob("*.docling.json"))]
    for row in rows:
        print(row)


if __name__ == "__main__":
    main()
