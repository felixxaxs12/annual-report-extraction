from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import fitz


TOC_SCAN_PAGES = 20
NAV_SCAN_PAGES = 30
BACKWARD_SCAN_PAGES = 6
FORWARD_SCAN_PAGES = 8


STRATEGIC_PATTERNS = [
    r"strategic\s+report",
    r"chairman['’\s]*s?\s+(statement|letter|review|overview)",
    r"chief\s+execut\w*['’\s]*s?\s+(statement|review|letter|report)",
    r"\bceo['’\s]*s?\s+(statement|review|letter|report)",
    r"business\s+(review|model|overview|at\s+a\s+glance)",
    r"operating\s+review",
    r"performance\s+(review|overview|highlights?)",
    r"market\s+(overview|review|context)",
    r"our\s+strategy",
    r"strategic\s+(overview|priorities|objectives|direction|focus)",
    r"key\s+performance\s+indicators?",
    r"\bkpis?\b",
    r"principal\s+risks?",
    r"risk\s+(management|review|overview|factors?)",
    r"financial\s+(review|overview)(?!\s+statements?)",
    r"\b(purpose|vision|mission|value\s+creation)\b",
    r"highlights?",
    r"who\s+we\s+are",
    r"\boverview\b",
    r"our\s+performance",
    r"long[-\s]+term",
    r"growth",
]

CSR_PATTERNS = [
    r"\bcsr\b",
    r"\besg\b",
    r"corporate\s+social\s+responsibility",
    r"corporate\s+responsibility\b",
    r"sustainability",
    r"sustainable\s+(business|future|development|value|operations|growth)",
    r"responsible\s+(business|practices?|sourcing|supply\s+chain)",
    r"climate\s+(change|action|strategy|transition)",
    r"carbon\s+(footprint|neutral|reduction|emissions?)",
    r"environment(al)?\s+(performance|impact|review|strategy)",
    r"greenhouse\s+gas",
    r"diversity\s+and\s+inclusion",
    r"stakeholder\s+engagement",
    r"community\s+(investment|engagement|impact|relations)",
    r"human\s+rights",
    r"health\s+and\s+safety",
    r"employees?\s+(wellbeing|engagement|development)",
]

EXCLUDE_PATTERNS = [
    r"corporate\s+governance",
    r"\bgovernance\b",
    r"directors?['’]?\s+report",
    r"board\s+of\s+directors",
    r"board\s+(biographies|profiles)",
    r"remuneration\s+(report|policy)",
    r"directors?['’]?\s+remuneration",
    r"audit\s+committee",
    r"nomination[s]?\s+committee",
    r"financial\s+statements?",
    r"consolidated\s+(financial\s+)?statements?",
    r"notes\s+to\s+(the\s+)?(financial|consolidated)",
    r"independent\s+auditor",
    r"shareholder\s+information",
    r"notice\s+of\s+(annual\s+)?general\s+meeting",
    r"\bagm\b",
    r"appendix|appendices",
    r"glossary",
]

CSR_FALSE_POSITIVE_PATTERNS = [
    r"csr\s+committee",
    r"corporate\s+(safety\s+and\s+)?social\s+responsibility\s+committee",
    r"committee\s+report",
    r"remuneration\s+committee",
    r"nomination[s]?\s+committee",
    r"audit\s+committee",
    r"financial\s+reporting",
    r"board\s+composition",
]

NAV_SR_RE = re.compile(
    r"strategic\s+re(?:port|view)\s+(\d+)\s*[–\-‐‑]+\s*(\d+)",
    re.IGNORECASE,
)
NAV_CSR_RE = re.compile(
    r"(?:corporate\s+(?:social\s+)?responsibility|\bcsr\b|\besg\b|sustainab\w+)"
    r"\s+(\d+)\s*[–\-‐‑]+\s*(\d+)",
    re.IGNORECASE,
)
TOC_LINE_RE = re.compile(r"^(.{3,90}?)[\.\s]{2,}(\d{1,3})\s*$")
TOC_LINE_REV_RE = re.compile(r"^(\d{1,3})\s{1,5}([A-Za-z][^0-9]{2,80})\s*$")
CONTENTS_RE = re.compile(r"(?:table\s+of\s+)?contents", re.IGNORECASE)

RE_STRATEGIC = re.compile("|".join(f"(?:{p})" for p in STRATEGIC_PATTERNS), re.IGNORECASE)
RE_CSR = re.compile("|".join(f"(?:{p})" for p in CSR_PATTERNS), re.IGNORECASE)
RE_EXCLUDE = re.compile("|".join(f"(?:{p})" for p in EXCLUDE_PATTERNS), re.IGNORECASE)
RE_CSR_FALSE_POSITIVE = re.compile(
    "|".join(f"(?:{p})" for p in CSR_FALSE_POSITIVE_PATTERNS),
    re.IGNORECASE,
)
HARD_EXCLUDE_RE = re.compile(
    r"\b("
    r"corporate\s+governance|governance|directors?['’]?\s+report|"
    r"board\s+of\s+directors|board\s+and\s+senior\s+management|"
    r"report\s+of\s+the\s+directors\s*[–-]\s*governance|"
    r"financial\s+statements?|independent\s+auditor|"
    r"remuneration\s+report"
    r")\b",
    re.IGNORECASE,
)
FRONT_MATTER_RE = re.compile(
    r"(annual\s+report\s+and\s+accounts\s+20\d{2}$|"
    r"legal\s+notice|contents|table\s+of\s+contents|industry\s+acronyms|"
    r"registered\s+office|company\s+information)",
    re.IGNORECASE,
)


@dataclass
class TocEntry:
    title: str
    printed_page: int
    end_page: int | None = None


@dataclass
class SectionRange:
    start: int
    end: int
    method: str
    csr_ranges: list[tuple[int, int]]
    warnings: list[str]


def normalize(text: str) -> str:
    return (
        text.replace("’", "'")
        .replace("‘", "'")
        .replace("\u00a0", " ")
        .replace("\u2009", " ")
        .replace("\u202f", " ")
        .replace("\ufb01", "fi")
        .replace("\ufb02", "fl")
        .replace("t\x01", "•")
        .replace("\x07", "")
    )


def page_lines(text: str) -> list[str]:
    return [normalize(line.strip()) for line in text.splitlines() if line.strip()]


class OcrUnavailableError(RuntimeError):
    pass


def raw_page_texts(pdf_path: Path) -> list[str]:
    doc = fitz.open(pdf_path)
    try:
        return [page.get_text("text") or "" for page in doc]
    finally:
        doc.close()


def has_text_layer(pdf_path: Path, probe_pages: int = 5, min_chars: int = 80) -> bool:
    doc = fitz.open(pdf_path)
    try:
        chars = 0
        for idx in range(min(probe_pages, len(doc))):
            chars += len((doc[idx].get_text("text") or "").strip())
        return chars >= min_chars
    finally:
        doc.close()


def ocr_cache_path(pdf_path: Path, cache_dir: Path) -> Path:
    stat = pdf_path.stat()
    key = hashlib.sha1(f"{pdf_path.resolve()}:{stat.st_size}:{int(stat.st_mtime)}".encode("utf-8")).hexdigest()[:16]
    return cache_dir / f"{pdf_path.stem}.{key}.ocr.json"


def require_tesseract() -> str:
    env_cmd = os.environ.get("TESSERACT_CMD")
    if env_cmd and Path(env_cmd).exists():
        return env_cmd
    exe = shutil.which("tesseract")
    if exe:
        return exe
    local_candidates = [
        Path("/Users/yizhao/Documents/Workstudy_Accounting_Data_Managment/.tools/ocr-env/bin/tesseract"),
    ]
    for candidate in local_candidates:
        if candidate.exists():
            return str(candidate)
    raise OcrUnavailableError(
        "OCR required but tesseract is not installed. Install tesseract, set TESSERACT_CMD, or run OCR externally before extraction."
    )


def ocr_page_text(page: fitz.Page, tesseract: str, lang: str = "eng", dpi: int = 220) -> str:
    matrix = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=matrix, alpha=False)
    with tempfile.TemporaryDirectory() as tmpdir:
        image_path = Path(tmpdir) / "page.png"
        pix.save(image_path)
        cmd = [tesseract, str(image_path), "stdout", "-l", lang, "--psm", "1"]
        proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        raise OcrUnavailableError(proc.stderr.strip() or "tesseract OCR failed")
    return proc.stdout or ""


def ocr_page_texts(pdf_path: Path, cache_dir: Path, lang: str = "eng", dpi: int = 220) -> list[str]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = ocr_cache_path(pdf_path, cache_dir)
    if cache_path.exists():
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        return [str(item) for item in data.get("pages", [])]

    tesseract = require_tesseract()
    doc = fitz.open(pdf_path)
    try:
        pages = [ocr_page_text(page, tesseract=tesseract, lang=lang, dpi=dpi) for page in doc]
    finally:
        doc.close()
    cache_path.write_text(
        json.dumps({"source_pdf": pdf_path.name, "backend": "tesseract", "lang": lang, "dpi": dpi, "pages": pages}, ensure_ascii=False),
        encoding="utf-8",
    )
    return pages


def page_texts(pdf_path: Path, ocr: str = "auto", ocr_cache_dir: Path | None = None, ocr_lang: str = "eng", ocr_dpi: int = 220) -> list[str]:
    if ocr not in {"auto", "always", "never"}:
        raise ValueError("ocr must be one of: auto, always, never")
    if ocr != "always":
        texts = raw_page_texts(pdf_path)
        if ocr == "never" or sum(len(text.strip()) for text in texts[:5]) >= 80:
            return texts
    if ocr == "never":
        return raw_page_texts(pdf_path)
    cache_dir = ocr_cache_dir or (pdf_path.parent / ".ocr_cache")
    return ocr_page_texts(pdf_path, cache_dir=cache_dir, lang=ocr_lang, dpi=ocr_dpi)


def head_text(text: str, n: int = 10) -> str:
    return " ".join(page_lines(text)[:n])


def is_toc_page(text: str) -> bool:
    lines = page_lines(text)
    if len(lines) < 4:
        return False
    standard_hits = sum(1 for line in lines if TOC_LINE_RE.match(line))
    reverse_hits = sum(1 for line in lines if TOC_LINE_REV_RE.match(line))
    joined = " ".join(lines[:35])
    compact = re.sub(r"[^a-z0-9]+", "", joined.lower())
    contents_like = bool(CONTENTS_RE.search(joined))
    major_section_hits = sum(
        1
        for pat in (
            r"chairman", r"chief executive", r"business model", r"risk management",
            r"corporate governance", r"remuneration", r"financial statements",
            r"independent auditor", r"notes to the", r"directors report",
        )
        if re.search(pat, joined, re.I)
    )
    page_ref_hits = len(re.findall(r"\b(?:p\s*)?\d{1,3}\b", joined, re.I))
    ocr_contents_like = (
        major_section_hits >= 5
        and page_ref_hits >= 8
        and re.search(r"financial\s+statements?|corporate\s+governance|remuneration", joined, re.I)
    )
    if re.search(r"p0?\dp\d{1,3}", compact, re.I):
        ocr_contents_like = True
    return contents_like or ocr_contents_like or standard_hits >= 3 or reverse_hits >= 5


def classify_title(title: str) -> str:
    title = normalize(title)
    if RE_EXCLUDE.search(title):
        return "exclude"
    if RE_CSR.search(title):
        if RE_CSR_FALSE_POSITIVE.search(title):
            return "exclude"
        return "csr"
    if RE_STRATEGIC.search(title):
        return "strategic"
    return "unknown"


def is_exclude_boundary(text: str) -> bool:
    first = head_text(text, 8)
    if RE_STRATEGIC.search(first):
        return False
    if RE_CSR.search(first) and not RE_CSR_FALSE_POSITIVE.search(first):
        return False
    return bool(RE_EXCLUDE.search(first))


def is_hard_exclude_boundary(text: str) -> bool:
    lines = page_lines(text)[:14]
    if not lines:
        return False
    first = " ".join(lines)
    first_lower = first.lower()

    # Do not cut off Strategic Report pages that merely discuss governance or CSR governance.
    compact = re.sub(r"[^a-z]", "", first_lower)
    if "strategicreport" in compact:
        return False
    if re.search(r"supporting\s+our\s+strategy|sustainability\s+contents|supporting\s+our\s+strategy\s+[–-]\s+sustainability", first, re.I):
        return False
    first_eight = " ".join(lines[:8])
    strategic_toc_terms = r"key\s+performance\s+indicators?|strategic\s+overview|20\d{2}\s+review|our\s+markets|group\s+financial\s+summary|flight\s+support"
    if re.search(strategic_toc_terms, first_eight, re.I) and not re.search(r"corporate\s+governance|remuneration\s+report", first_eight, re.I):
        return False
    if RE_CSR.search(first) and not RE_CSR_FALSE_POSITIVE.search(first) and not re.search(r"governance|directors?['’]?\s+report|board\s+of\s+directors", first, re.I):
        return False
    compact_title = re.sub(r"[^a-z]", "", " ".join(lines[:8]).lower())
    if re.search(r"corporategovernance(?!code)|reportofthedirectorsgovernance|governanceboardofdirectors|directorsreportdirectorsreport", compact_title):
        return True

    cleaned_lines = []
    for line in lines:
        cleaned = re.sub(r"^[\d\s|./-]+", "", line.lower()).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        cleaned_lines.append(cleaned)

    title_patterns = [
        r"^corporate governance( report| statement)?$",
        r"^governance$",
        r"^directors?['’]? report$",
        r"^board of directors( and senior management| and executive management)?$",
        r"^(non-)?executive directors$",
        r"^financial statements?$",
        r"^independent auditor",
        r"^remuneration report$",
        r"^report of the directors [–-] governance$",
    ]
    if any(re.search(pat, line, re.I) for line in cleaned_lines for pat in title_patterns):
        return True

    if re.search(r"^directors?\s+name\s+background\s+and\s+experience\b", first_lower, re.I):
        return True

    strong_phrases = [
        r"directors?['’]? report.{0,80}corporate governance",
        r"corporate governance.{0,80}remuneration report",
        r"report of the directors.{0,30}governance",
        r"board of directors and senior management",
        r"directors?\s+name\s+background\s+and\s+experience",
    ]
    return any(re.search(pat, first_lower, re.I) for pat in strong_phrases)


def is_front_matter_page(text: str) -> bool:
    lines = page_lines(text)
    if not lines:
        return True
    joined = " ".join(lines[:20])
    if is_toc_page(text):
        return True
    if re.search(r"legal\s+notice|industry\s+acronyms|registered\s+office|company\s+information", joined, re.I):
        return True
    if FRONT_MATTER_RE.search(joined) and not (RE_STRATEGIC.search(joined) or RE_CSR.search(joined)):
        return True
    if len(joined) < 180 and re.search(r"annual\s+report|accounts\s+20\d{2}", joined, re.I):
        return True
    return False


def is_report_navigation_page(text: str) -> bool:
    lines = page_lines(text)
    if not lines:
        return False
    joined = " ".join(lines[:45])
    if re.search(r"strategy\s+and\s+objectives.*our\s+business\s+model.*summary\s+governance", joined, re.I):
        return True
    section_terms = [
        r"strategy\s+and\s+objectives", r"our\s+business\s+model", r"our\s+markets",
        r"summary\s+corporate\s+responsibility", r"summary\s+principal\s+risks",
        r"summary\s+governance", r"financial\s+statements", r"board\s+composition",
        r"audit\s+committee", r"directors.?\s+remuneration", r"five\s+year\s+financial\s+record",
    ]
    hits = sum(1 for pat in section_terms if re.search(pat, joined, re.I))
    ranges = len(re.findall(r"\b\d{1,3}\s*[–-]\s*\d{1,3}\b", joined))
    return hits >= 5 and (ranges >= 2 or len(joined) < 1200)


def is_substantive_pre_governance_page(text: str) -> bool:
    if is_front_matter_page(text) or is_hard_exclude_boundary(text):
        return False
    joined = " ".join(page_lines(text)[:25])
    if len(joined) < 220:
        return False
    return True


def is_strategic_like(text: str) -> bool:
    first = head_text(text, 12)
    if is_exclude_boundary(text):
        return False
    return bool(RE_STRATEGIC.search(first))


def is_csr_like(text: str) -> bool:
    first = head_text(text, 12)
    if is_exclude_boundary(text):
        return False
    if RE_CSR_FALSE_POSITIVE.search(first):
        return False
    return bool(RE_CSR.search(first))


def build_printed_page_map(texts: list[str]) -> dict[int, int]:
    mapping: dict[int, int] = {}
    for idx, text in enumerate(texts):
        lines = page_lines(text)
        candidates = lines[:3] + lines[-3:]
        for line in candidates:
            if re.fullmatch(r"\d{1,4}", line):
                mapping.setdefault(int(line), idx)
            m = re.match(r"^(\d{1,4})\s+\S", line)
            if m:
                mapping.setdefault(int(m.group(1)), idx)
            m = re.search(r"(?<!\d)(\d{1,3})$", line)
            if m:
                mapping.setdefault(int(m.group(1)), idx)
    return mapping


def parse_toc_entries(texts: list[str]) -> list[TocEntry]:
    entries: list[TocEntry] = []
    for text in texts[:TOC_SCAN_PAGES]:
        if not is_toc_page(text):
            continue
        for line in page_lines(text):
            m = TOC_LINE_RE.match(line)
            if m:
                title = m.group(1).strip()
                if len(title) >= 3:
                    entries.append(TocEntry(title, int(m.group(2))))
                continue
            m = TOC_LINE_REV_RE.match(line)
            if m:
                title = m.group(2).strip()
                if len(title) >= 3:
                    entries.append(TocEntry(title, int(m.group(1))))
    seen: set[tuple[str, int]] = set()
    deduped: list[TocEntry] = []
    for entry in sorted(entries, key=lambda e: e.printed_page):
        key = (entry.title.lower()[:40], entry.printed_page)
        if key not in seen:
            seen.add(key)
            deduped.append(entry)
    return deduped


def printed_to_pdf_index(printed_page: int, mapping: dict[int, int], total: int) -> int | None:
    if printed_page in mapping:
        return mapping[printed_page]
    candidate = printed_page - 1
    if 0 <= candidate < total:
        return candidate
    return None


def toc_bounds(texts: list[str]) -> SectionRange | None:
    entries = parse_toc_entries(texts)
    if not entries:
        return None
    mapping = build_printed_page_map(texts)
    total = len(texts)
    converted: list[tuple[TocEntry, int, str]] = []
    for entry in entries:
        idx = printed_to_pdf_index(entry.printed_page, mapping, total)
        if idx is not None:
            converted.append((entry, idx, classify_title(entry.title)))
    if not converted:
        return None

    strategic_positions = [
        i for i, (_, _, kind) in enumerate(converted)
        if kind in {"strategic", "csr"}
    ]
    if not strategic_positions:
        return None

    first_rel = strategic_positions[0]
    start = converted[first_rel][1]

    end = total - 1
    for i in range(first_rel + 1, len(converted)):
        _, idx, kind = converted[i]
        if kind == "exclude" and idx > start:
            end = idx - 1
            break

    csr_ranges: list[tuple[int, int]] = []
    for i, (_, idx, kind) in enumerate(converted):
        if kind != "csr":
            continue
        next_idx = converted[i + 1][1] - 1 if i + 1 < len(converted) else idx
        if not (start <= idx <= end):
            csr_ranges.append((idx, min(next_idx, total - 1)))

    return SectionRange(start, end, "toc", csr_ranges, [])


def navbar_bounds(texts: list[str]) -> SectionRange | None:
    mapping = build_printed_page_map(texts)
    for text in texts[:NAV_SCAN_PAGES]:
        m = NAV_SR_RE.search(normalize(text))
        if not m:
            continue
        start = mapping.get(int(m.group(1)))
        end = mapping.get(int(m.group(2)))
        if start is None or end is None or start > end:
            continue
        csr_ranges: list[tuple[int, int]] = []
        for nav_text in texts[:NAV_SCAN_PAGES]:
            for cm in NAV_CSR_RE.finditer(normalize(nav_text)):
                cs = mapping.get(int(cm.group(1)))
                ce = mapping.get(int(cm.group(2)))
                if cs is None or ce is None or cs > ce:
                    continue
                if not (start <= cs <= end):
                    csr_ranges.append((cs, ce))
        return SectionRange(start, end, "navbar", csr_ranges, [])
    return None


def header_bounds(texts: list[str]) -> SectionRange | None:
    strategic_pages = [idx for idx, text in enumerate(texts) if is_strategic_like(text)]
    if not strategic_pages:
        return None
    start = min(strategic_pages)
    end = max(strategic_pages)
    for idx in range(start, len(texts)):
        if idx > start and is_exclude_boundary(texts[idx]):
            end = idx - 1
            break
        if idx <= end + FORWARD_SCAN_PAGES and not is_exclude_boundary(texts[idx]):
            if is_strategic_like(texts[idx]) or is_csr_like(texts[idx]):
                end = max(end, idx)
    csr_pages = [
        idx for idx, text in enumerate(texts)
        if is_csr_like(text) and not (start <= idx <= end)
    ]
    return SectionRange(start, end, "header", contiguous_ranges(csr_pages), [])


def fallback_bounds(texts: list[str]) -> SectionRange | None:
    first_exclude = next((idx for idx, text in enumerate(texts) if idx > 1 and is_exclude_boundary(text)), None)
    if first_exclude is None:
        return None
    start = next((idx for idx in range(1, first_exclude) if len(normalize(texts[idx]).strip()) > 200), 1)
    end = first_exclude - 1
    csr_pages = [
        idx for idx in range(first_exclude + 1, len(texts))
        if is_csr_like(texts[idx])
    ]
    return SectionRange(start, end, "fallback", contiguous_ranges(csr_pages), [])


def contiguous_ranges(indices: list[int]) -> list[tuple[int, int]]:
    if not indices:
        return []
    indices = sorted(indices)
    ranges: list[tuple[int, int]] = []
    start = end = indices[0]
    for idx in indices[1:]:
        if idx == end + 1:
            end = idx
        else:
            ranges.append((start, end))
            start = end = idx
    ranges.append((start, end))
    return ranges


def pre_governance_bounds(texts: list[str]) -> SectionRange | None:
    boundary = next((idx for idx, text in enumerate(texts) if idx >= 2 and is_hard_exclude_boundary(text)), None)
    if boundary is None or boundary <= 1:
        return None

    start = None
    for idx in range(1, boundary):
        if not is_front_matter_page(texts[idx]) and not is_hard_exclude_boundary(texts[idx]) and (is_strategic_like(texts[idx]) or is_csr_like(texts[idx])):
            start = idx
            break
    if start is None:
        for idx in range(1, boundary):
            if is_substantive_pre_governance_page(texts[idx]):
                start = idx
                break
    if start is None:
        return None

    end = boundary - 1
    # Do not append post-governance CSR in the default broad Strategic Report mode.
    # Committee and governance pages create many false positives; standalone CSR can be
    # reviewed separately from validation warnings if a corpus needs it.
    return SectionRange(start, end, "pre-governance", [], [])


def adjust_boundaries(candidate: SectionRange, texts: list[str]) -> SectionRange:
    start, end = candidate.start, candidate.end
    warnings = list(candidate.warnings)

    for idx in range(start - 1, max(-1, start - BACKWARD_SCAN_PAGES - 1), -1):
        if idx < 0 or is_front_matter_page(texts[idx]) or is_hard_exclude_boundary(texts[idx]):
            continue
        if candidate.method != "pre-governance" and (is_strategic_like(texts[idx]) or is_csr_like(texts[idx])):
            start = idx
            warnings.append(f"start_extended_to_page_{idx + 1}")

    for idx in range(end, min(len(texts), end + FORWARD_SCAN_PAGES + 1)):
        if idx < start:
            continue
        if is_hard_exclude_boundary(texts[idx]):
            if idx <= end:
                end = idx - 1
                warnings.append(f"end_trimmed_before_page_{idx + 1}")
            break
        if candidate.method != "pre-governance" and idx > end and (is_strategic_like(texts[idx]) or is_csr_like(texts[idx])):
            end = idx
            warnings.append(f"end_extended_to_page_{idx + 1}")

    csr_ranges = []
    for cs, ce in candidate.csr_ranges:
        block = " ".join(head_text(texts[i], 12) for i in range(cs, min(ce + 1, len(texts))))
        if RE_CSR_FALSE_POSITIVE.search(block):
            warnings.append(f"csr_range_dropped_false_positive_pages_{cs + 1}_{ce + 1}")
            continue
        if cs <= end and ce >= start:
            continue
        csr_ranges.append((cs, ce))

    return SectionRange(max(0, start), max(start, end), candidate.method, csr_ranges, warnings)


def find_bounds(texts: list[str]) -> SectionRange | None:
    for finder in (pre_governance_bounds, navbar_bounds, toc_bounds, header_bounds, fallback_bounds):
        candidate = finder(texts)
        if candidate is not None:
            return adjust_boundaries(candidate, texts)
    return None




def is_standalone_csr_start(text: str) -> bool:
    first = head_text(text, 16)
    if not first or is_hard_exclude_boundary(text):
        return False
    if RE_CSR_FALSE_POSITIVE.search(first):
        return False
    if re.search(r"\b(audit|nomination|remuneration)\s+committee\b|board\s+composition|financial\s+reporting", first, re.I):
        return False
    return bool(RE_CSR.search(first))


def find_standalone_csr_ranges(texts: list[str], main_range: SectionRange) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    idx = 0
    while idx < len(texts):
        if main_range.start <= idx <= main_range.end:
            idx = main_range.end + 1
            continue
        if not is_standalone_csr_start(texts[idx]):
            idx += 1
            continue

        start = idx
        end = idx
        probe = idx + 1
        while probe < len(texts):
            if main_range.start <= probe <= main_range.end:
                break
            if is_hard_exclude_boundary(texts[probe]) and not is_standalone_csr_start(texts[probe]):
                break
            if is_front_matter_page(texts[probe]):
                break
            head = head_text(texts[probe], 16)
            if RE_CSR_FALSE_POSITIVE.search(head):
                break
            if RE_EXCLUDE.search(head) and not RE_CSR.search(head):
                break
            if len(normalize(texts[probe]).strip()) < 180:
                break
            end = probe
            probe += 1

        if end >= start:
            ranges.append((start, end))
        idx = max(probe, end + 1)
    return ranges

def csr_status(texts: list[str], result: SectionRange) -> str:
    if result.csr_ranges:
        return "found"
    if any(is_csr_like(texts[idx]) or RE_CSR.search(texts[idx]) for idx in range(result.start, result.end + 1)):
        return "within-sr"
    return "not-found"


@dataclass
class LayoutBlock:
    block_id: int
    kind: int
    bbox: tuple[float, float, float, float]
    text: str
    lines: list[str]


VALUE_RE = re.compile(
    r"^\(?[+\-]?(?:[$£€])?\s*\d{1,4}(?:[,\s]\d{3})*(?:\.\d+)?\s*(?:%|c|p|m|bn|million|billion|mtpa|boe|kboed|mmboe|cents?|pence)?\)?$",
    re.IGNORECASE,
)
YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
MULTI_VALUE_RE = re.compile(
    r"^(?:(?:19|20)\d{2}|\(?[+\-]?(?:[$£€])?\s*\d{1,4}(?:[,\s]\d{3})*(?:\.\d+)?\s*(?:%|c|p|m|bn|million|billion|mtpa|boe|kboed|mmboe|cents?|pence)?\)?)(?:\s+(?:(?:19|20)\d{2}|\(?[+\-]?(?:[$£€])?\s*\d{1,4}(?:[,\s]\d{3})*(?:\.\d+)?\s*(?:%|c|p|m|bn|million|billion|mtpa|boe|kboed|mmboe|cents?|pence)?\)?))*$",
    re.IGNORECASE,
)
FOOTNOTE_PREFIX_RE = re.compile(r"^\(?[a-z]\)\s+", re.IGNORECASE)
CHART_TITLE_RE = re.compile(
    r"\b("
    r"chart|graph|trend|performance|revenue|profit|earnings|dividend|margin|"
    r"costs?|kpis?|emissions?|carbon|safety|injury|production|demand|supply|"
    r"forecast|outlook|growth|returns?|cash\s+flow|operating\s+profit"
    r")\b",
    re.IGNORECASE,
)
VISUAL_UNIT_RE = re.compile(
    r"\((?:\$|£|€|%|/|mtpa|boe|kboed|mmboe|cents?|pence|tonnes?|tco2e|m|bn)[^)]+\)",
    re.IGNORECASE,
)
TABLE_TRIGGER_RE = re.compile(
    r"\b(financial\s+results|business\s+performance|revenue\s+and\s+other\s+operating\s+income|"
    r"total\s+operating\s+profit)\b",
    re.IGNORECASE,
)


def block_lines(block: dict) -> list[str]:
    lines: list[str] = []
    for line in block.get("lines", []):
        text = normalize(" ".join(span.get("text", "") for span in line.get("spans", []))).strip()
        text = re.sub(r"\s+", " ", text)
        if text:
            lines.append(text)
    return lines


def layout_blocks(page: fitz.Page) -> list[LayoutBlock]:
    blocks: list[LayoutBlock] = []
    for block_id, block in enumerate(page.get_text("dict").get("blocks", [])):
        kind = int(block.get("type", 0))
        lines = block_lines(block) if kind == 0 else []
        text = "\n".join(lines)
        bbox = tuple(float(x) for x in block.get("bbox", (0, 0, 0, 0)))
        blocks.append(LayoutBlock(block_id, kind, bbox, text, lines))
    return blocks


def rect_area(bbox: tuple[float, float, float, float]) -> float:
    x0, y0, x1, y1 = bbox
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def bbox_intersects(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return ax0 < bx1 and ax1 > bx0 and ay0 < by1 and ay1 > by0


def in_bbox(bbox: tuple[float, float, float, float], region: tuple[float, float, float, float]) -> bool:
    x0, y0, x1, y1 = bbox
    cx = (x0 + x1) / 2
    cy = (y0 + y1) / 2
    rx0, ry0, rx1, ry1 = region
    return rx0 <= cx <= rx1 and ry0 <= cy <= ry1


def is_value_line(text: str) -> bool:
    compact = re.sub(r"\s+", " ", normalize(text)).strip()
    return bool(VALUE_RE.fullmatch(compact) or YEAR_RE.fullmatch(compact) or MULTI_VALUE_RE.fullmatch(compact))


def value_tokens(text: str) -> list[str]:
    tokens = re.findall(
        r"\(?[+\-]?(?:[$£€])?\s*\d{1,4}(?:[,\s]\d{3})*(?:\.\d+)?\s*(?:%|c|p|m|bn|mtpa|boe|kboed|mmboe|cents?|pence)?\)?",
        normalize(text),
        flags=re.IGNORECASE,
    )
    cleaned = [re.sub(r"\s+", " ", token).strip() for token in tokens]
    return [token for token in cleaned if token]


def is_page_chrome(block: LayoutBlock) -> bool:
    text = " ".join(block.lines).strip()
    if not text:
        return True
    if re.fullmatch(r"\d{1,4}", text):
        return True
    if text.lower() in {"strategic report", "annual report", "annual report and accounts"}:
        return True
    if re.search(r"annual\s+report\s+(?:and|&)\s+accounts\s+20\d{2}", text, re.I) and len(text) < 160:
        return True
    if block.lines and re.fullmatch(r"\.\d{2}\s+(overview|financial|social|health\s+and\s+safety|environment)", block.lines[0].strip(), re.I):
        return True
    if re.fullmatch(r"\.\d{2}\s+(overview|financial|social|health\s+and\s+safety|environment)(?:\s+\d{1,3})?", text, re.I):
        return True
    if re.fullmatch(r"go\s+to\s+pages?\s+[\d–\-]+", text, re.I):
        return True
    if block.bbox[1] < 90 and text.lower() in {"financial review", "operations review", "principal", "governance", "financial statements", "general information"}:
        return True
    if re.search(r"^\|?\s*strategic\s+report\s*\|?$", text, re.I):
        return True
    if re.match(r"^source:", text, re.I) and len(text) < 180:
        return True
    if re.fullmatch(r"(?:www\.)?[a-z0-9.-]+\.(?:com|co\.uk|org|net)(?:\s+\d+)?", text, re.I):
        return True
    return False


def is_spaced_letter_noise(text: str) -> bool:
    clean = normalize(text).strip()
    if len(clean) < 12:
        return False
    single_letter_tokens = re.findall(r"(?<![A-Za-z])[A-Za-z](?![A-Za-z])", clean)
    alpha_count = len(re.findall(r"[A-Za-z]", clean))
    if alpha_count == 0:
        return False
    if len(single_letter_tokens) >= 8 and len(single_letter_tokens) / alpha_count > 0.55:
        return True
    return False


def is_heading_line(line: str) -> bool:
    clean = normalize(line).strip()
    if len(clean) < 3 or len(clean) > 95:
        return False
    if not re.search(r"[A-Za-z]", clean):
        return False
    if is_spaced_letter_noise(clean):
        return False
    if re.match(r"^[*>†‡§]+\s*(?:source:|for\s+a\s+reconciliation|includes?|see\s+page|restated|note)", clean, re.I):
        return False
    if re.fullmatch(r">?\s*\(?[+\-]?\d[\d,]*(?:\.\d+)?\)?\s*(?:%|x|m|bn|k)?", clean, re.I):
        return False
    if re.fullmatch(r"%?\s*(?:[A-Z]\s+){2,}[A-Z]", clean):
        return False
    if re.match(r"^\([^)]{1,20}\)\s+", clean) and not re.fullmatch(r"\([^)]{1,20}\)\s*[A-Z&/ -]+", clean):
        return False
    if is_value_line(clean):
        return False
    if RE_EXCLUDE.search(clean):
        return False
    if FOOTNOTE_PREFIX_RE.match(clean) or re.match(r"^[a-z]\s+(?:see|source|compared|organic|net debt|this represents)\b", clean, re.I):
        return False
    if re.match(r"^(?:source:|see\s+page|note:|www\.)", clean, re.I):
        return False
    if re.match(r"^[•]\s+", clean):
        return False
    if clean.endswith((".", ",", ";", ":")) and len(clean.split()) > 4:
        return False
    if re.search(r"\b(can be|to be|has been|have been|is not|are not)$", clean, re.I):
        return False
    if re.search(r"\b(and|or|of|to|in|with|from|that|which|for|by|at)$", clean, re.I):
        return False
    words = clean.split()
    first_alpha = next((ch for ch in clean if ch.isalpha()), "")
    if re.match(r"^lng\b", clean, re.I):
        return True
    if first_alpha and first_alpha.islower():
        return False
    upperish = sum(1 for ch in clean if ch.isalpha() and ch.isupper())
    letters = sum(1 for ch in clean if ch.isalpha())
    upper_ratio = upperish / letters if letters else 0.0
    if upper_ratio > 0.65 and len(words) <= 10:
        return True
    sentence_verbs = r"\b(is|are|was|were|has|have|had|can|could|should|would|will|may|must|expect|expects|create|creates|created|gives|represents|excludes)\b"
    if len(words) >= 5 and re.search(sentence_verbs, clean, re.I):
        return False
    if re.match(r"^(In|As|For|To|With|From|By|Of|At)\b", clean) and len(words) >= 4:
        return False
    return 1 <= len(words) <= 7


def format_lines_as_paragraphs(lines: list[str]) -> str:
    output: list[str] = []
    paragraph: list[str] = []
    for line in lines:
        line = normalize(line).strip()
        if re.fullmatch(r"[•●—–-]+", line):
            continue
        if re.search(r"(?:\b[A-Za-z]\b\s+){8,}", line):
            continue
        if re.match(r"^[•●—–\-]+\s+", line):
            if paragraph:
                output.append(" ".join(paragraph))
                paragraph = []
            output.append("- " + re.sub(r"^[•●—–\-]+\s+", "", line).strip())
        elif FOOTNOTE_PREFIX_RE.match(line):
            if paragraph:
                output.append(" ".join(paragraph))
                paragraph = []
            output.append(line)
        else:
            if output and output[-1].startswith("- ") and not paragraph and not re.search(r"[.!?;:]$", output[-1]):
                output[-1] = output[-1] + " " + line
            else:
                paragraph.append(line)
    if paragraph:
        output.append(" ".join(paragraph))
    return "\n".join(output)


def is_visual_letter_fragment(block: LayoutBlock) -> bool:
    if block.kind != 0 or not block.lines:
        return False
    text = " ".join(normalize(line).strip() for line in block.lines).strip()
    if not text:
        return True
    keep = {"USA", "UK", "HSE", "ESG", "CSR", "EPS", "LNG", "LPG"}
    if text.upper() in keep:
        return False
    if len(text) <= 4 and re.fullmatch(r"[A-Za-z& ]{1,4}", text):
        return True
    if len(text) <= 12 and " " in text and re.fullmatch(r"(?:[A-Za-z&]\s*){1,8}", text):
        return True
    if all(len(normalize(line).strip()) <= 4 and re.fullmatch(r"[A-Za-z& ]{1,4}", normalize(line).strip()) for line in block.lines):
        return True
    return False


def clean_heading_text(text: str) -> str:
    clean = normalize(text).strip()
    clean = re.sub(r"^[>•●–—-]+\s*", "", clean)
    clean = re.sub(r"\s+", " ", clean)
    clean = re.sub(r"\blng\b", "LNG", clean, flags=re.I)
    clean = re.sub(r"\bkpis\b", "KPIs", clean, flags=re.I)
    if re.fullmatch(r"WHAT our STRATEGY DELIVERS", clean, re.I):
        return "What our strategy delivers"
    if re.fullmatch(r"Our PERFORMANCE", clean, re.I):
        return "Our performance"
    if re.fullmatch(r"OUR MODEL…?", clean, re.I):
        return "Our model"
    return clean


def format_text_block(block: LayoutBlock) -> str:
    if is_page_chrome(block):
        return ""
    lines = [normalize(line).strip() for line in block.lines if not is_value_line(line)]
    lines = [line for line in lines if line and not is_spaced_letter_noise(line)]
    if not lines:
        return ""
    if len(lines) >= 2 and is_heading_line(lines[0]) and (re.match(r"^&\s*\w+", lines[1]) or re.fullmatch(r"[A-Z .…]{4,}", lines[1]) or (len(lines) == 2 and lines[1][:1].islower() and len(lines[0] + " " + lines[1]) <= 95 and not re.search(r"[.!?]", lines[1]))):
        heading = clean_heading_text(lines[0] + " " + lines[1])
        rest = lines[2:]
        return f"### {heading}" + (f"\n\n{format_lines_as_paragraphs(rest)}" if rest else "")
    if len(lines) == 1:
        line = lines[0]
        if is_heading_line(line):
            return f"### {clean_heading_text(line)}"
        return line
    first, rest = lines[0], lines[1:]
    if is_heading_line(first) and rest:
        return f"### {clean_heading_text(first)}\n\n{format_lines_as_paragraphs(rest)}"
    return format_lines_as_paragraphs(lines)


def markdown_table(rows: list[list[str]]) -> str:
    if not rows or not rows[0]:
        return ""
    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]
    header = padded[0]
    body = padded[1:]

    def clean_cell(cell: str) -> str:
        cell = normalize(cell or "").replace("\n", "<br>")
        return re.sub(r"\s+", " ", cell).strip()

    lines = [
        "| " + " | ".join(clean_cell(cell) for cell in header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for row in body:
        lines.append("| " + " | ".join(clean_cell(cell) for cell in row) + " |")
    return "\n".join(lines)


def valid_pymupdf_table(table) -> bool:
    rows = table.extract()
    cells = [str(cell).strip() for row in rows for cell in row if cell and str(cell).strip()]
    if table.row_count < 2 or table.col_count < 2 or len(cells) < 4:
        return False
    blank_count = table.row_count * table.col_count - len(cells)
    if blank_count > table.row_count * table.col_count * 0.45:
        return False
    if any(cell.count("\n") > 4 or len(cell) > 180 for cell in cells):
        return False
    numeric_cells = sum(1 for cell in cells if value_tokens(cell))
    label_cells = sum(1 for cell in cells if re.search(r"[A-Za-z]", cell))
    if numeric_cells < 2 or label_cells < 2:
        return False
    joined = " ".join(cells).lower()
    if "page " in joined and numeric_cells <= 3:
        return False
    return True


def nearest_heading_above(blocks: list[LayoutBlock], bbox: tuple[float, float, float, float]) -> str:
    x0, y0, x1, _ = bbox
    candidates: list[tuple[float, str]] = []
    for block in blocks:
        if block.kind != 0 or not block.lines:
            continue
        bx0, by0, bx1, _ = block.bbox
        if by0 >= y0 or bx1 < x0 - 40 or bx0 > x1 + 40:
            continue
        text = " ".join(block.lines)
        if is_heading_line(text) or TABLE_TRIGGER_RE.search(text):
            candidates.append((by0, text))
    if not candidates:
        return ""
    return sorted(candidates, reverse=True)[0][1][:120]


def should_run_table_detector(blocks: list[LayoutBlock]) -> bool:
    numeric_lines = 0
    label_lines = 0
    trigger = False
    for block in blocks:
        if block.kind != 0:
            continue
        if TABLE_TRIGGER_RE.search(block.text):
            trigger = True
        for line in block.lines:
            if is_value_line(line):
                numeric_lines += 1
            elif re.search(r"[A-Za-z]", line):
                label_lines += 1
    return trigger or (numeric_lines >= 6 and label_lines >= 4)


def table_events_from_page(page: fitz.Page, blocks: list[LayoutBlock]) -> tuple[list[tuple[float, str]], set[int]]:
    events: list[tuple[float, str]] = []
    used: set[int] = set()
    if not should_run_table_detector(blocks):
        return events, used
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            tables = page.find_tables().tables
    except Exception:
        return events, used
    for idx, table in enumerate(tables, start=1):
        if not valid_pymupdf_table(table):
            continue
        rows = table.extract()
        heading = nearest_heading_above(blocks, table.bbox)
        label = heading or f"Detected table {idx}"
        events.append((table.bbox[1], f"[Table]\n\n**{label}**\n\n{markdown_table(rows)}"))
        for block in blocks:
            if block.kind == 0 and bbox_intersects(block.bbox, table.bbox):
                used.add(block.block_id)
    return events, used


def parse_financial_results_lines(lines: list[str]) -> str:
    text = "\n".join(lines)
    if not (TABLE_TRIGGER_RE.search(text) and re.search(r"\bUpstream\b", text) and re.search(r"\bLNG\s+Shipping", text)):
        return ""
    labels = [
        "Upstream",
        "LNG Shipping & Marketing",
        "Other activities",
        "Less: intra-Group revenue",
        "Net finance costs (d)",
        "Taxation (d)",
        "Earnings",
    ]
    lines = [line.strip() for line in lines if line.strip()]
    label_positions = [(label, lines.index(label)) for label in labels if label in lines]
    if len(label_positions) < 4:
        return ""
    rows: list[list[str]] = [["Item", "Revenue 2013 ($m)", "Revenue 2012 ($m)", "Operating profit 2013 ($m)", "Operating profit 2012 ($m)"]]
    for pos, (label, start_pos) in enumerate(label_positions):
        end_pos = label_positions[pos + 1][1] if pos + 1 < len(label_positions) else len(lines)
        numbers = [line for line in lines[start_pos + 1 : end_pos] if is_value_line(line)]
        if label == "Less: intra-Group revenue" and len(numbers) >= 6:
            rows.append([label, numbers[0], numbers[1], "", ""])
            rows.append(["Total", numbers[2], numbers[3], numbers[4], numbers[5]])
            continue
        if label in {"Net finance costs (d)", "Taxation (d)", "Earnings"}:
            rows.append([label, "", "", numbers[0] if len(numbers) > 0 else "", numbers[1] if len(numbers) > 1 else ""])
            continue
        rows.append([label] + numbers[:4] + [""] * max(0, 4 - len(numbers[:4])))
    footnotes = [line for line in lines if FOOTNOTE_PREFIX_RE.match(line)]
    footnote_text = "\n".join(f"- {line}" for line in footnotes)
    suffix = f"\n\n**Notes:**\n{footnote_text}" if footnote_text else ""
    return "[Table]\n\n**Financial results: business performance**\n\n" + markdown_table(rows) + suffix


def parse_financial_results_table(block: LayoutBlock) -> str:
    return parse_financial_results_lines(block.lines)


def financial_results_events(blocks: list[LayoutBlock]) -> tuple[list[tuple[float, str]], set[int]]:
    events: list[tuple[float, str]] = []
    used: set[int] = set()
    for idx, block in enumerate(blocks):
        if block.kind != 0 or not TABLE_TRIGGER_RE.search(block.text):
            continue
        nearby = [block]
        for other in blocks[idx + 1 : idx + 5]:
            if other.kind != 0:
                continue
            if other.bbox[1] - block.bbox[1] > 190:
                break
            if abs(other.bbox[0] - block.bbox[0]) < 45 or bbox_intersects((block.bbox[0]-20, block.bbox[1], block.bbox[2]+20, block.bbox[3]+190), other.bbox):
                nearby.append(other)
        lines = [line for candidate in nearby for line in candidate.lines]
        parsed = parse_financial_results_lines(lines)
        if parsed:
            events.append((block.bbox[1], parsed))
            used.update(candidate.block_id for candidate in nearby)
            break
    return events, used

def money_m_value(text: str) -> str:
    match = re.search(r"[$£€]\s*\d[\d\s,]*(?:\.\d+)?\s*m", text, re.I)
    return re.sub(r"\s+", "", match.group(0)).replace("$", "$") if match else ""


def percent_value(text: str) -> str:
    match = re.search(r"[+\-]?\s*\d+(?:\.\d+)?\s*%", text)
    return re.sub(r"\s+", "", match.group(0)) if match else ""


def segment_description(block: LayoutBlock, name: str) -> str:
    lines = [line.strip() for line in block.lines]
    if not lines or lines[0].lower() != name.lower():
        return ""
    return " ".join(lines[1:]).strip()


def segment_kpi_events(blocks: list[LayoutBlock]) -> tuple[list[tuple[float, str]], set[int]]:
    upstream = next((b for b in blocks if b.kind == 0 and b.lines and b.lines[0].lower() == "upstream" and len(b.lines) > 2), None)
    lng = next((b for b in blocks if b.kind == 0 and b.lines and b.lines[0].lower().startswith("lng shipping") and len(b.lines) > 2), None)
    if upstream is None or lng is None:
        return [], set()

    kpi_blocks = [
        b for b in blocks
        if b.kind == 0 and re.search(r"total\s+operating\s+profit", b.text, re.I)
        and re.search(r"[$£€]\s*\d", b.text)
    ]
    if not kpi_blocks:
        return [], set()

    left_kpi = min(kpi_blocks, key=lambda b: abs(b.bbox[0] - upstream.bbox[0]))
    right_kpi = min(kpi_blocks, key=lambda b: abs(b.bbox[0] - lng.bbox[0]))
    all_kpi_text = "\n".join(b.text for b in kpi_blocks)
    money_values = [re.sub(r"\s+", "", m.group(0)) for m in re.finditer(r"[$£€]\s*\d[\d\s,]*(?:\.\d+)?\s*m", all_kpi_text, re.I)]
    pct_values = [re.sub(r"\s+", "", m.group(0)) for m in re.finditer(r"[+\-]?\s*\d+(?:\.\d+)?\s*%", all_kpi_text)]

    if len(money_values) < 4:
        return [], set()

    upstream_current, upstream_prior = money_values[0], money_values[1]
    lng_current, lng_prior = money_values[2], money_values[3]
    upstream_change = pct_values[0] if pct_values else ""
    lng_change = pct_values[1] if len(pct_values) > 1 else ""

    rows = [
        ["Segment", "Business description", "Operating profit current", "Operating profit prior year", "Change"],
        ["Upstream", segment_description(upstream, "Upstream"), upstream_current, upstream_prior, upstream_change],
        ["LNG Shipping & Marketing", segment_description(lng, "LNG Shipping & Marketing"), lng_current, lng_prior, lng_change],
    ]
    event = (
        min(upstream.bbox[1], lng.bbox[1], left_kpi.bbox[1], right_kpi.bbox[1]),
        "[Segment KPI Summary]\n\n" + markdown_table(rows),
    )
    used = {upstream.block_id, lng.block_id, left_kpi.block_id, right_kpi.block_id}
    return [event], used


def clean_metric_title(text: str) -> str:
    clean = normalize(text).replace("*", "").strip()
    clean = re.sub(r"\s+", " ", clean)
    return clean.title() if clean.isupper() else clean


def is_kpi_card_block(block: LayoutBlock) -> bool:
    if block.kind != 0 or len(block.lines) < 2 or len(block.lines) > 4:
        return False
    if block.bbox[3] - block.bbox[1] < 22:
        return False
    title = " ".join(block.lines[:-1]) if is_value_line(block.lines[-1]) else block.lines[0]
    value_line = block.lines[-1]
    if not is_value_line(value_line):
        return False
    if len(title) < 4 or len(title) > 95:
        return False
    if not re.search(r"[A-Za-z]", title):
        return False
    if RE_EXCLUDE.search(title):
        return False
    context = title.lower()
    return bool(
        re.search(
            r"operating\s+profit|production\s+volumes?|earnings\s+per\s+share|dividend|revenue|cash\s+flow|unit\s+costs?|profit|kpi",
            context,
            re.I,
        )
    )


def nearby_prior_value(card: LayoutBlock, blocks: list[LayoutBlock]) -> tuple[str, int | None]:
    x0, _, x1, y1 = card.bbox
    candidates: list[tuple[float, LayoutBlock]] = []
    for block in blocks:
        if block.block_id == card.block_id or block.kind != 0 or not block.lines:
            continue
        text = " ".join(block.lines).strip()
        tokens = value_tokens(text)
        non_year_tokens = [token for token in tokens if not re.fullmatch(r"(?:19|20)\d{2}", clean_value_token(token))]
        if not re.search(r"\b20\d{2}\b", text) or not non_year_tokens:
            continue
        if block.bbox[1] < y1 - 8 or block.bbox[1] - y1 > 90:
            continue
        if horizontal_overlap(block.bbox, (x0, card.bbox[1], x1, y1), margin=55):
            candidates.append((block.bbox[1], block))
    if not candidates:
        return "", None
    block = sorted(candidates, key=lambda item: item[0])[0][1]
    return " ".join(block.lines).strip(), block.block_id


def kpi_card_events(blocks: list[LayoutBlock], already_used: set[int]) -> tuple[list[tuple[float, str]], set[int]]:
    cards = [
        block for block in blocks
        if block.block_id not in already_used and is_kpi_card_block(block)
    ]
    if not cards:
        return [], set()

    clusters: list[list[LayoutBlock]] = []
    for card in sorted(cards, key=lambda b: (b.bbox[1], b.bbox[0])):
        placed = False
        for cluster in clusters:
            if abs(card.bbox[1] - cluster[0].bbox[1]) < 35:
                cluster.append(card)
                placed = True
                break
        if not placed:
            clusters.append([card])

    events: list[tuple[float, str]] = []
    used: set[int] = set()
    for cluster in clusters:
        cluster = sorted(cluster, key=lambda b: b.bbox[0])
        if not cluster:
            continue
        y_top = min(block.bbox[1] for block in cluster)
        y_bottom = max(block.bbox[3] for block in cluster)
        pct_lines: list[str] = []
        pct_block_ids: set[int] = set()
        x_min = min(block.bbox[0] for block in cluster) - 25
        x_max = max(block.bbox[2] for block in cluster) + 25
        for block in blocks:
            if block.kind != 0 or block.block_id in already_used or block.block_id in {c.block_id for c in cluster}:
                continue
            if block.bbox[0] > x_max or block.bbox[2] < x_min:
                continue
            if block.bbox[1] < y_bottom or block.bbox[1] - y_bottom > 105:
                continue
            matches = [line for line in block.lines if re.fullmatch(r"[+\-]?\s*\d+(?:\.\d+)?\s*%", normalize(line).strip())]
            if matches:
                pct_lines.extend(clean_value_token(line) for line in matches)
                pct_block_ids.add(block.block_id)

        rows = [["Metric", "Current", "Prior year", "Change"]]
        row_used: set[int] = set()
        for idx, card in enumerate(cluster):
            title = clean_metric_title(card.lines[0])
            current = clean_value_token(card.lines[-1])
            prior, prior_id = nearby_prior_value(card, blocks)
            change = pct_lines[idx] if idx < len(pct_lines) else ""
            rows.append([title, current, prior, change])
            row_used.add(card.block_id)
            if prior_id is not None:
                row_used.add(prior_id)
        if len(rows) <= 1:
            continue
        used.update(row_used)
        used.update(pct_block_ids)
        events.append((y_top, "[KPI Summary]\n\n" + markdown_table(rows)))
    return events, used



LOW_VALUE_FRAGMENT_RE = re.compile(
    r"^(?:key|further detail\??|delivered on time or ahead of schedule|delivered but behind schedule|"
    r"additional detailed information can be|found online or in our data book|"
    r"for further details(?:,| see| about)?|go to pages?|"
    r"corporate governance\s+\d|financial statements\s+\d|shareholder information\s+\d|"
    r"low|med|high|ero|dai|far|vat|fpm|"
    r"for a reconciliation between business performance and total results)",
    re.IGNORECASE,
)


def low_value_fragment_omissions(blocks: list[LayoutBlock], already_used: set[int]) -> set[int]:
    used: set[int] = set()
    for block in blocks:
        if block.block_id in already_used or block.kind != 0 or not block.lines:
            continue
        text = " ".join(normalize(line).strip() for line in block.lines).strip()
        if not text:
            continue
        if LOW_VALUE_FRAGMENT_RE.search(text):
            used.add(block.block_id)
    return used


def milestone_events(blocks: list[LayoutBlock], already_used: set[int]) -> tuple[list[tuple[float, str]], set[int]]:
    title = next(
        (block for block in blocks if block.block_id not in already_used and block.kind == 0 and re.fullmatch(r"20\d{2} milestones", " ".join(block.lines).strip(), re.I)),
        None,
    )
    if title is None:
        return [], set()
    quarters = [
        block for block in blocks
        if block.block_id not in already_used and block.kind == 0 and len(block.lines) == 1 and re.fullmatch(r"Q[1-4]", block.lines[0].strip(), re.I)
    ]
    if len(quarters) < 2:
        return [], set()
    rows = [["Period", "Milestones", "Outcome / status text"]]
    used = {title.block_id}
    for quarter in sorted(quarters, key=lambda b: b.bbox[1]):
        qx0, qy0, _, _ = quarter.bbox
        qy1 = qy0 + 75
        milestone_parts: list[str] = []
        outcome_parts: list[str] = []
        for block in blocks:
            if block.block_id in already_used or block.block_id == quarter.block_id or block.kind != 0:
                continue
            bx0, by0, _, _ = block.bbox
            if by0 < qy0 - 8 or by0 > qy1:
                continue
            text = " ".join(line.strip() for line in block.lines).strip()
            if not text or LOW_VALUE_FRAGMENT_RE.search(text):
                continue
            if qx0 - 15 <= bx0 <= qx0 + 135:
                milestone_parts.append(text)
                used.add(block.block_id)
            elif bx0 > qx0 + 140:
                outcome_parts.append(text)
                used.add(block.block_id)
        if milestone_parts or outcome_parts:
            rows.append([quarter.lines[0].strip().upper(), "; ".join(milestone_parts), "; ".join(outcome_parts)])
            used.add(quarter.block_id)
    if len(rows) <= 2:
        return [], set()
    return [(title.bbox[1], "[Milestone summary]\n\n" + markdown_table(rows))], used



def dense_table_events(blocks: list[LayoutBlock]) -> tuple[list[tuple[float, str]], set[int]]:
    events, used = financial_results_events(blocks)
    for block in blocks:
        if block.kind != 0 or block.block_id in used:
            continue
        parsed = parse_financial_results_table(block)
        if parsed:
            events.append((block.bbox[1], parsed))
            used.add(block.block_id)
            continue
        numeric_count = sum(1 for line in block.lines if is_value_line(line))
        label_count = sum(1 for line in block.lines if line and not is_value_line(line))
        if TABLE_TRIGGER_RE.search(block.text) and numeric_count >= 8 and label_count >= 3:
            rows = [["Detected table text"], [format_lines_as_paragraphs(block.lines)]]
            events.append((block.bbox[1], "[Table]\n\n**Table-like content; layout could not be fully reconstructed**\n\n" + markdown_table(rows)))
            used.add(block.block_id)
    return events, used


def is_chart_title(block: LayoutBlock) -> bool:
    if block.kind != 0 or not block.lines or len(block.text) > 160:
        return False
    text = " ".join(block.lines)
    if TABLE_TRIGGER_RE.search(text):
        return False
    if re.fullmatch(r"\([^)]+\)", text.strip()):
        return False
    return bool(CHART_TITLE_RE.search(text) or VISUAL_UNIT_RE.search(text))


def nearby_numeric_blocks(title: LayoutBlock, blocks: list[LayoutBlock]) -> list[LayoutBlock]:
    x0, y0, x1, y1 = title.bbox
    region = (max(0, x0 - 25), y0, x1 + 160, y1 + 150)
    return [
        block for block in blocks
        if block.kind == 0 and block.block_id != title.block_id and in_bbox(block.bbox, region)
        and any(is_value_line(line) for line in block.lines)
    ]


def chart_cluster(title: LayoutBlock, blocks: list[LayoutBlock]) -> list[LayoutBlock]:
    x0, y0, x1, y1 = title.bbox
    region = (max(0, x0 - 25), y0, x1 + 160, y1 + 150)
    return [block for block in blocks if block.kind == 0 and in_bbox(block.bbox, region)]


def clean_value_token(token: str) -> str:
    return re.sub(r"\s+", "", normalize(token)).replace(" ", "")


def parse_numeric_token(token: str) -> tuple[float, str, str] | None:
    raw = clean_value_token(token)
    if re.fullmatch(r"(?:19|20)\d{2}", raw):
        return None
    match = re.search(r"([+\-]?\(?\d+(?:,\d{3})*(?:\.\d+)?\)?)(.*)", raw)
    if not match:
        return None
    number_text = match.group(1).replace(",", "").strip("()")
    try:
        number = float(number_text)
    except ValueError:
        return None
    suffix = match.group(2).lower()
    if raw.startswith("(") and raw.endswith(")"):
        number = -number
    if raw.startswith("-"):
        number = -abs(number)
    unit = suffix
    if raw.startswith("$") or "$" in raw:
        unit = "$" + unit.replace("$", "")
    elif raw.startswith("£") or "£" in raw:
        unit = "£" + unit.replace("£", "")
    elif raw.startswith("€") or "€" in raw:
        unit = "€" + unit.replace("€", "")
    return number, unit, raw


def format_percent(value: float) -> str:
    rounded = round(value, 1)
    if abs(rounded - round(rounded)) < 0.05:
        return f"{int(round(rounded))}%"
    return f"{rounded:.1f}%"


def compact_list(items: list[str], limit: int = 6) -> str:
    cleaned = [item for item in dict.fromkeys(items) if item]
    if not cleaned:
        return ""
    suffix = "" if len(cleaned) <= limit else "; ..."
    return "; ".join(cleaned[:limit]) + suffix


def numeric_evidence_groups(values: list[str]) -> dict[str, list[str]]:
    groups = {"years": [], "percentages": [], "currency_or_units": [], "other_numbers": []}
    for value in values:
        token = clean_value_token(value)
        parsed = parse_numeric_token(token)
        if re.fullmatch(r"(?:19|20)\d{2}", token):
            groups["years"].append(token)
        elif parsed and "%" in parsed[1]:
            groups["percentages"].append(parsed[2])
        elif parsed and parsed[1]:
            groups["currency_or_units"].append(parsed[2])
        elif parsed:
            groups["other_numbers"].append(parsed[2])
    return {key: list(dict.fromkeys(vals)) for key, vals in groups.items()}


def infer_general_chart_interpretation(title_text: str, labels: list[str], values: list[str]) -> str:
    groups = numeric_evidence_groups(values)
    years = groups["years"]
    percentages = groups["percentages"]
    unit_values = groups["currency_or_units"]
    other_numbers = groups["other_numbers"]

    meaningful_labels = [
        label for label in labels
        if not re.search(r"strategic\s+report|annual\s+report|source:", label, re.I)
        and len(label) > 1
    ]
    title_lower = title_text.lower()

    clauses: list[str] = [f"This visual appears to present '{title_text}'."]
    if years:
        if len(years) >= 2:
            clauses.append(f"It appears to compare periods including {compact_list(years, 5)}.")
        else:
            clauses.append(f"It references {years[0]}.")
    if meaningful_labels:
        clauses.append(f"Visible categories or series include {compact_list(meaningful_labels, 7)}.")
    if percentages:
        clauses.append(f"Visible percentage markers include {compact_list(percentages, 4)}.")
    if unit_values and not percentages:
        clauses.append(f"Visible unit or currency values include {compact_list(unit_values, 5)}.")
    elif other_numbers and not percentages and not unit_values:
        clauses.append(f"Visible numeric markers include {compact_list(other_numbers, 5)}.")

    if "forecast" in title_lower or "outlook" in title_lower or any("forecast" in label.lower() for label in meaningful_labels):
        clauses.append("The chart is forward-looking or compares current estimates with a forecast/outlook.")
    if any(term in title_lower for term in ["cost", "margin", "revenue", "profit", "earnings", "dividend"]):
        clauses.append("This is a financial or operating KPI visual, but the extracted text does not reliably preserve every label-value relationship.")
    elif any(term in title_lower for term in ["emission", "carbon", "safety", "injury", "sustainability"]):
        clauses.append("This is likely relevant to CSR/ESG or operational responsibility analysis.")

    clauses.append("Exact numeric-to-category mapping is not reliable from text extraction alone; use the values below only as OCR evidence unless confirmed by nearby narrative text or the source PDF.")
    return " ".join(clauses)


def noisy_kpi_context(labels: list[str], values: list[str]) -> bool:
    if any(re.fullmatch(r"20\d{2}\d+.*", clean_value_token(value)) for value in values):
        return True
    sentence_like = sum(1 for label in labels if len(label.split()) > 8 or re.search(r"\.$", label.strip()))
    if sentence_like >= 1 and len(labels) > 3:
        return True
    return len(labels) > 8


def infer_kpi_interpretation(title_text: str, labels: list[str], values: list[str]) -> str:
    context = " ".join([title_text] + labels).lower()
    if noisy_kpi_context(labels, values):
        return ""
    years = [clean_value_token(value) for value in values if re.fullmatch(r"(?:19|20)\d{2}", clean_value_token(value))]
    parsed = [parsed for value in values if (parsed := parse_numeric_token(value))]

    cents = [(num, raw) for num, unit, raw in parsed if unit in {"c", "cent", "cents"}]
    pence = [(num, raw) for num, unit, raw in parsed if unit in {"p", "pence"}]
    percentages = [(num, raw) for num, unit, raw in parsed if "%" in unit]
    money_m = [(num, raw) for num, unit, raw in parsed if "$" in unit and "m" in unit]

    if "dividend" in context and (cents or pence):
        parts = []
        if cents:
            current_cents = cents[0]
            prior_cents = cents[1] if len(cents) > 1 else None
            if prior_cents:
                year = years[0] if years else "prior year"
                change = (current_cents[0] - prior_cents[0]) / prior_cents[0] * 100 if prior_cents[0] else 0
                parts.append(f"Current full-year dividend per share is {current_cents[1]}; {year} comparative is {prior_cents[1]}; increase is approximately {format_percent(change)} in cents terms.")
            else:
                parts.append(f"Current full-year dividend per share is {current_cents[1]}.")
        if pence:
            current_pence = pence[0]
            prior_pence = pence[1] if len(pence) > 1 else None
            if prior_pence:
                year = years[0] if years else "prior year"
                change = (current_pence[0] - prior_pence[0]) / prior_pence[0] * 100 if prior_pence[0] else 0
                parts.append(f"In pence, the current amount is {current_pence[1]} versus {year} {prior_pence[1]} (~{format_percent(change)} change, affected by currency conversion).")
            else:
                parts.append(f"In pence, the current amount is {current_pence[1]}.")
        return " ".join(parts)

    if "earnings per share" in context and cents:
        mixed_measures = "total results" in context and "business performance" in context
        if mixed_measures:
            if len(cents) >= 3:
                year = years[0] if years else "prior year"
                return (
                    f"The visual contains multiple EPS measures. Total results EPS appears to be {cents[0][1]} "
                    f"versus {year} {cents[2][1]}; business performance EPS appears to be {cents[1][1]}"
                    + (f" versus {year} {cents[3][1]}." if len(cents) > 3 else ".")
                )
            return "The visual contains multiple EPS measures; values are retained as evidence, but the chart block is not clear enough for a full current-vs-prior mapping."
        current = cents[0]
        prior = cents[1] if len(cents) > 1 else None
        if prior:
            year = years[0] if years else "prior year"
            change = (current[0] - prior[0]) / prior[0] * 100 if prior[0] else 0
            return f"Current earnings per share is {current[1]}; {year} comparative is {prior[1]}; change is approximately {format_percent(change)}."
        return f"Current earnings per share is {current[1]}."

    if "operating profit" in context and money_m:
        mixed_measures = "total results" in context and "business performance" in context
        if mixed_measures and len(money_m) < 4:
            return "The visual contains multiple operating-profit measures; values are retained as evidence, but the chart block is not clear enough for a reliable current-vs-prior mapping."
        current = money_m[0]
        prior = money_m[1] if len(money_m) > 1 else None
        visible_change = percentages[0][1] if percentages else ""
        if prior:
            year = years[0] if years else "prior year"
            computed = (current[0] - prior[0]) / prior[0] * 100 if prior[0] else 0
            change = visible_change or format_percent(computed)
            return f"Current total operating profit is {current[1]}; {year} comparative is {prior[1]}; change shown/computed is {change}."
        return f"Current total operating profit is {current[1]}."

    return ""


def chart_summary(title: LayoutBlock, cluster: list[LayoutBlock]) -> str | None:
    title_text = " ".join(title.lines)
    source = ""
    labels: list[str] = []
    values: list[str] = []
    for block in cluster:
        for line in block.lines:
            clean = line.strip()
            if not clean or clean == title_text:
                continue
            if re.search(r"^source:", clean, re.I):
                source = clean
                continue
            if is_value_line(clean):
                values.extend(value_tokens(clean))
                continue
            mostly_numeric = len(re.sub(r"[^0-9]", "", clean)) >= max(2, len(re.sub(r"[^A-Za-z]", "", clean)) * 2)
            if len(clean) <= 80 and not FOOTNOTE_PREFIX_RE.match(clean) and not mostly_numeric:
                labels.append(clean)
            values.extend([token for token in value_tokens(clean) if re.search(r"[.%$£€]|(?:19|20)\d{2}", token)])
    filtered_values: list[str] = []
    for value in values:
        compact = clean_value_token(value)
        if re.fullmatch(r"\d{1,3}", compact) and not filtered_values:
            continue
        if compact not in filtered_values:
            filtered_values.append(compact)
        if len(filtered_values) >= 10:
            break
    unique_labels = list(dict.fromkeys(label for label in labels if not is_value_line(label)))[:8]
    interpretation = infer_kpi_interpretation(title_text, unique_labels, filtered_values)
    low_confidence_markers = ("not clear enough", "multiple", "appears", "not reliable", "not enough")
    has_glued_values = any(re.fullmatch(r"20\d{2}\d+.*", clean_value_token(value)) for value in filtered_values)
    is_confident_interpretation = bool(interpretation) and not has_glued_values and not any(marker in interpretation.lower() for marker in low_confidence_markers)
    if not is_confident_interpretation:
        return None

    parts = [f"[Chart summary]\n\n**Title:** {title_text}"]
    parts.append("**Interpretation:** " + interpretation)
    if filtered_values:
        parts.append("**Evidence values:** " + "; ".join(filtered_values))
    if source:
        parts.append(f"**Source:** {source.removeprefix('Source:').strip()}")
    parts.append("**Handling note:** chart retained because the label-value relationship was clear enough for rule-based interpretation.")
    return "\n\n".join(parts)


def chart_events(blocks: list[LayoutBlock], already_used: set[int]) -> tuple[list[tuple[float, str]], set[int]]:
    events: list[tuple[float, str]] = []
    used: set[int] = set()
    for block in blocks:
        if block.block_id in already_used or block.block_id in used:
            continue
        if not is_chart_title(block):
            continue
        numeric_neighbors = [
            neighbor for neighbor in nearby_numeric_blocks(block, blocks)
            if neighbor.block_id not in already_used and neighbor.block_id not in used
        ]
        if len(numeric_neighbors) < 3:
            continue
        cluster = [
            neighbor for neighbor in chart_cluster(block, blocks)
            if neighbor.block_id not in already_used and neighbor.block_id not in used
        ]
        summary = chart_summary(block, cluster)
        if summary:
            events.append((block.bbox[1], summary))
        used.update(neighbor.block_id for neighbor in cluster)
    return events, used


MAP_LABEL_STOPWORDS = {
    "upstream",
    "lng shipping & marketing",
    "total operating profit",
    "business performance",
    "strategic report",
    "our business segments",
}


def is_map_label_block(block: LayoutBlock) -> bool:
    if block.kind != 0 or len(block.lines) != 1:
        return False
    text = block.lines[0].strip()
    if not text or text.lower() in MAP_LABEL_STOPWORDS:
        return False
    if len(text) > 55 or len(text.split()) > 5:
        return False
    if re.search(r"\d|[$£€%]", text):
        return False
    if not re.fullmatch(r"[A-Za-z&'’ .-]+", text):
        return False
    height = block.bbox[3] - block.bbox[1]
    width = block.bbox[2] - block.bbox[0]
    if height > 11 or width > 120:
        return False
    return True


GEO_LABEL_HINTS = {
    "argentina", "australia", "austria", "azerbaijan", "brazil", "canada", "china",
    "egypt", "france", "germany", "india", "indonesia", "iraq", "italy", "japan",
    "kazakhstan", "kenya", "madagascar", "mexico", "norway", "oman", "qatar",
    "russia", "singapore", "tanzania", "thailand", "tunisia", "turkey", "uk",
    "united kingdom", "usa", "united states", "europe", "asia", "africa",
    "middle east", "areas of palestinian authority", "trinidad", "tobago",
    "trinidad and tobago", "united states of america", "colombia", "bolivia",
}


def geo_label_count(labels: list[str]) -> int:
    count = 0
    for label in labels:
        clean = re.sub(r"[^a-z ]", "", label.lower()).strip()
        if clean in GEO_LABEL_HINTS:
            count += 1
    return count


def map_label_events(blocks: list[LayoutBlock], already_used: set[int]) -> tuple[list[tuple[float, str]], set[int]]:
    candidates = [
        block for block in blocks
        if block.block_id not in already_used and is_map_label_block(block)
    ]
    if len(candidates) < 6:
        return [], set()

    labels = [block.lines[0].strip() for block in candidates]
    labels = list(dict.fromkeys(labels))
    if geo_label_count(labels) < 4:
        return [], set()
    used = {block.block_id for block in candidates}
    for block in blocks:
        if block.block_id in already_used or block.kind != 0 or not block.lines:
            continue
        text = " ".join(block.lines).strip()
        clean_geo = re.sub(r"[^a-z ]", "", text.lower()).strip()
        if clean_geo in GEO_LABEL_HINTS or clean_geo in {"and tobago", "us"}:
            used.add(block.block_id)
    y = min(block.bbox[1] for block in candidates)
    # Map labels alone rarely help LTO coding and can interrupt multi-column narrative.
    # Mark the labels as used, but do not emit a placeholder. The surrounding
    # narrative usually names the relevant regions with better context.
    return [], used

STRATEGY_DIAGRAM_LABEL_RE = re.compile(
    r"\b("
    r"page\s+\d+|communicating\s+our\s+strategy|our\s+strategy\s+is\s+explained|"
    r"how\s+we\s+(create|deliver|work)|we\s+(create|deliver)\s+value|"
    r"what\s+our\s+strategy\s+delivers|our\s+performance|delivers|strategy|value"
    r")\b",
    re.IGNORECASE,
)


def strategy_diagram_omissions(blocks: list[LayoutBlock], already_used: set[int]) -> set[int]:
    anchors = [
        block for block in blocks
        if block.block_id not in already_used
        and block.kind == 0
        and re.search(r"communicating\s+our\s+strategy", block.text, re.I)
    ]
    if not anchors:
        return set()

    used: set[int] = set()
    for anchor in anchors:
        x0, y0, x1, y1 = anchor.bbox
        region = (max(0.0, x0 - 30), max(0.0, y0 - 25), x1 + 390, y1 + 150)
        for block in blocks:
            if block.block_id in already_used or block.kind != 0:
                continue
            text = " ".join(block.lines).strip()
            if not text:
                continue
            if block.block_id == anchor.block_id:
                used.add(block.block_id)
                continue
            if in_bbox(block.bbox, region) and STRATEGY_DIAGRAM_LABEL_RE.search(text):
                used.add(block.block_id)
    return used



VISUAL_NOTE_START_RE = re.compile(
    r"^(?:\(?[a-z]\)\s*)?(?:source:|see\s+page\b|note:)|"
    r"^\(?[a-z]\)\s+.*(?:source:|see\s+page|further\s+details|data\s+unavailable|peer\s+group)",
    re.IGNORECASE,
)


def horizontal_overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float], margin: float = 25.0) -> bool:
    return a[0] < b[2] + margin and a[2] > b[0] - margin


def is_visual_note_block(block: LayoutBlock) -> bool:
    if block.kind != 0 or not block.lines or len(block.lines) > 6:
        return False
    text = " ".join(block.lines).strip()
    if len(text) > 520:
        return False
    if not VISUAL_NOTE_START_RE.search(block.lines[0].strip()):
        return False
    # These notes usually document a chart/table source. If the related visual was
    # omitted, keeping the note alone creates misleading context for the LLM.
    return bool(re.search(r"\b(source|see\s+page|note|further\s+details|peer\s+group|data\s+unavailable)\b", text, re.I))


def orphan_visual_note_omissions(blocks: list[LayoutBlock], already_used: set[int], visual_used: set[int]) -> set[int]:
    if not visual_used:
        return set()
    visual_blocks = [block for block in blocks if block.block_id in visual_used]
    used: set[int] = set()
    for block in blocks:
        if block.block_id in already_used or not is_visual_note_block(block):
            continue
        x0, y0, x1, _ = block.bbox
        note_bbox = (x0, y0, x1, block.bbox[3])
        for visual in visual_blocks:
            if visual.bbox[1] >= y0:
                continue
            vertical_gap = y0 - visual.bbox[3]
            if 0 <= vertical_gap <= 260 and horizontal_overlap(note_bbox, visual.bbox, margin=45):
                used.add(block.block_id)
                break
    return used




def image_events(page: fitz.Page, blocks: list[LayoutBlock], used_regions: list[tuple[float, float, float, float]]) -> list[tuple[float, str]]:
    # A blank image marker does not help LTO coding and often interrupts narrative flow.
    # Text inside charts/tables is handled separately by chart/table/KPI rules.
    return []


def text_blocks_for_reading(blocks: list[LayoutBlock], used: set[int]) -> list[LayoutBlock]:
    text_blocks: list[LayoutBlock] = []
    for block in blocks:
        if block.block_id in used or block.kind != 0:
            continue
        if is_visual_letter_fragment(block):
            continue
        if any(is_value_line(line) for line in block.lines) and all(is_value_line(line) for line in block.lines):
            continue
        if format_text_block(block):
            text_blocks.append(block)
    return text_blocks


def column_centers(blocks: list[LayoutBlock]) -> list[float]:
    starts = sorted(block.bbox[0] for block in blocks if block.bbox[2] - block.bbox[0] > 20)
    centers: list[float] = []
    for x0 in starts:
        if not centers or abs(x0 - centers[-1]) > 95:
            centers.append(x0)
        else:
            centers[-1] = (centers[-1] + x0) / 2
    return centers or [0.0]


def reading_key(block: LayoutBlock, centers: list[float], page_width: float) -> tuple[int, float, float]:
    x0, y0, x1, _ = block.bbox
    width = x1 - x0
    # Very wide page-level blocks should be read before column text at the same vertical band.
    if width > page_width * 0.62:
        return (-1, y0, x0)
    column = min(range(len(centers)), key=lambda idx: abs(x0 - centers[idx]))
    return (column, y0, x0)


def generated_reading_key(y: float, blocks: list[LayoutBlock], centers: list[float], page_width: float) -> tuple[int, float, float]:
    candidates = [block for block in blocks if abs(block.bbox[1] - y) < 12 or block.bbox[1] >= y]
    if not candidates:
        return (len(centers), y, 0.0)
    nearest = min(candidates, key=lambda block: (max(0.0, block.bbox[1] - y), abs(block.bbox[1] - y)))
    return reading_key(nearest, centers, page_width)


def continuation_tail(text: str) -> str:
    return text.rstrip().split("\n\n")[-1].strip()


def should_merge_continuation(previous: str, current: str) -> bool:
    prev_tail = continuation_tail(previous)
    cur = current.lstrip()
    if not prev_tail or not cur:
        return False
    if previous.startswith("[") or cur.startswith("#") or cur.startswith("<!--") or cur.startswith("["):
        return False
    if "\n\n" in cur:
        return False
    if prev_tail.startswith("#") or re.search(r"[.!?;:]\s*$", prev_tail):
        return False
    return bool(re.match(r"^[a-z(]", cur))


def append_to_last_paragraph(previous: str, current: str) -> str:
    parts = previous.rstrip().split("\n\n")
    parts[-1] = parts[-1].rstrip() + " " + current.lstrip()
    return "\n\n".join(parts)


def merge_continuation_blocks(chunks: list[str]) -> list[str]:
    merged: list[str] = []
    for chunk in chunks:
        if merged and should_merge_continuation(merged[-1], chunk):
            merged[-1] = append_to_last_paragraph(merged[-1], chunk)
        else:
            merged.append(chunk)
    return merged


def page_to_markdown(page: fitz.Page, page_no: int) -> str:
    blocks = layout_blocks(page)
    segment_events, used = segment_kpi_events(blocks)
    kpi_events_out, kpi_used = kpi_card_events(blocks, used)
    used.update(kpi_used)
    milestone_events_out, milestone_used = milestone_events(blocks, used)
    used.update(milestone_used)
    used.update(low_value_fragment_omissions(blocks, used))
    dense_events, dense_used = dense_table_events(blocks)
    used.update(dense_used)
    table_events, table_used = table_events_from_page(page, blocks)
    used.update(table_used)
    chart_summaries, chart_used = chart_events(blocks, used)
    used.update(chart_used)
    used.update(orphan_visual_note_omissions(blocks, used, chart_used))
    map_events_out, map_used = map_label_events(blocks, used)
    used.update(map_used)
    used.update(strategy_diagram_omissions(blocks, used))
    used_regions = [block.bbox for block in blocks if block.block_id in used]
    generated_events = segment_events + kpi_events_out + milestone_events_out + table_events + dense_events + chart_summaries + map_events_out + image_events(page, blocks, used_regions)

    text_blocks = text_blocks_for_reading(blocks, used)
    centers = column_centers(text_blocks)
    events: list[tuple[tuple[int, float, float], str]] = []

    for y, markdown in generated_events:
        events.append((generated_reading_key(y, blocks, centers, page.rect.width), markdown))
    for block in text_blocks:
        formatted = format_text_block(block)
        if formatted:
            events.append((reading_key(block, centers, page.rect.width), formatted))

    ordered = [event[1] for event in sorted(events, key=lambda item: item[0])]
    ordered = merge_continuation_blocks(ordered)
    page_body = "\n\n".join(ordered).strip()
    if not page_body:
        return ""
    return f"<!-- pdf_page: {page_no} -->\n\n## PDF page {page_no}\n\n{page_body}".strip()


def is_ocr_cover_or_noise_page(text: str) -> bool:
    lines = page_lines(text)
    if not lines:
        return True
    joined = " ".join(lines)
    alpha = sum(ch.isalpha() for ch in joined)
    chars = len(joined)
    sentence_verbs = re.search(r"\b(is|are|was|were|has|have|had|will|would|could|should|may|must|depends|recognises|provides|creates|delivers|expects)\b", joined, re.I)
    if chars < 180 and not (RE_STRATEGIC.search(joined) or RE_CSR.search(joined)):
        return True
    if "companies hous" in joined.lower() and chars < 450:
        return True
    if chars < 260 and not sentence_verbs:
        return True
    if chars and alpha / chars < 0.45 and not (RE_STRATEGIC.search(joined) or RE_CSR.search(joined)):
        return True
    return False


def is_ocr_director_bio_page(text: str) -> bool:
    joined = " ".join(page_lines(text)[:20])
    return bool(
        re.search(r"^directors?\s+name\s+background\s+and\s+experience\b", joined, re.I)
        or (
            re.search(r"\bnon[-\s]?executive\s+director\b", joined, re.I)
            and re.search(r"\bregistered\s+office\b|\bsecretary\b", joined, re.I)
        )
    )


def is_ocr_low_value_portfolio_listing_page(text: str) -> bool:
    lines = page_lines(text)
    joined = " ".join(lines[:25])
    if re.search(r"investment\s+portfolio\s+summary\s+and\s+disposal\s+history|summary\s+of\s+investment\s+portfolio\s+movement", joined, re.I):
        return True
    cost_hits = sum(1 for line in lines if re.search(r"\bcost:\s*[£€$]|\bvaluation:\s*[£€$]|\bequity\s+held:|\bvaluation\s+basis:", line, re.I))
    url_hits = sum(1 for line in lines if re.search(r"\bwww\.", line, re.I))
    company_card_hits = len(re.findall(r"\b(cost|valuation|date[s]? of investment|equity held|valuation basis|dividends received)\b", joined, re.I))
    return cost_hits >= 3 or (url_hits >= 2 and company_card_hits >= 4)


def is_ocr_markdown_skip_page(text: str) -> bool:
    if is_ocr_cover_or_noise_page(text):
        return True
    if is_toc_page(text) or is_report_navigation_page(text):
        return True
    if is_hard_exclude_boundary(text) or is_ocr_director_bio_page(text):
        return True
    if is_ocr_low_value_portfolio_listing_page(text):
        return True
    return False


def is_ocr_tableish_line(line: str) -> bool:
    stripped = normalize(line).strip()
    if len(stripped) < 8:
        return False
    numeric_tokens = re.findall(r"(?:[$£€]|US\$)?\(?-?\d[\d,]*(?:\.\d+)?(?:%|p|m|bn|million|billion)?\)?", stripped, re.I)
    year_tokens = re.findall(r"\b(?:19|20)\d{2}\b", stripped)
    words = re.findall(r"[A-Za-z]{3,}", stripped)
    sentence_verbs = re.search(r"\b(is|are|was|were|has|have|had|will|would|could|should|may|must|expect|expects|believe|believes)\b", stripped, re.I)
    if len(year_tokens) >= 3:
        return True
    if len(numeric_tokens) >= 3 and len(words) <= 12 and not sentence_verbs:
        return True
    if len(numeric_tokens) >= 2 and re.search(r"\b(assets|income|profit|dividend|valuation|sales|ebita|net assets|retained)\b", stripped, re.I) and len(words) <= 10:
        return True
    return False


def is_ocr_heading_line(line: str) -> bool:
    clean = normalize(line).strip()
    if not clean or len(clean) > 90:
        return False
    if re.search(r"[$£€]|\d", clean):
        return False
    if len(re.findall(r"[A-Za-z]", clean)) < 3:
        return False
    known = (
        r"Chairman['’]s Statement|Chief Executive['’]s Review|Financial Results|Shareholder Relations|"
        r"Fundraising|Outlook|Principal Risks|Other Matters|Investment Policy|Portfolio Composition|"
        r"Summary and Outlook|Working responsibly|Effective risk management|Financial Highlights for the Year|"
        r"Total Return Increases|Net Asset Value Increase|Investment Growth|Dividends for the Year|"
        r"Original Investor Return|Dividend Re-investment Scheme|Shareholder Communications|"
        r"Changes to Investment Management and Incentive Agreements|Subsequent Events|Regulatory"
    )
    if re.fullmatch(known, clean, re.I):
        return True
    words = clean.split()
    if not (1 <= len(words) <= 6):
        return False
    if re.match(r"^(To|In|On|Of|For|With|From|By|At|As|This|These|The)\b", clean) and len(words) > 2:
        return False
    if re.search(r"\b(the|and|or|of|to|in|with|from|that|which|for|by|at|under|over)$", clean, re.I):
        return False
    if re.search(r"\b(is|are|was|were|has|have|had|will|would|could|should|may|must|depends|recognises|provides|creates|delivers|expects)\b", clean, re.I):
        return False
    alpha = [ch for ch in clean if ch.isalpha()]
    if alpha and sum(ch.isupper() for ch in alpha) / len(alpha) > 0.75:
        return len(words) <= 5
    return bool(re.match(r"^[A-Z][A-Za-z'’&/ -]+$", clean)) and len(words) <= 4


def flush_ocr_paragraph(output: list[str], paragraph: list[str]) -> None:
    if paragraph:
        output.append(" ".join(paragraph))
        paragraph.clear()


def flush_ocr_table_buffer(output: list[str], paragraph: list[str], table_buffer: list[str]) -> None:
    if not table_buffer:
        return
    if len(table_buffer) >= 3:
        flush_ocr_paragraph(output, paragraph)
        output.append("[Table omitted: low-confidence OCR table]")
    else:
        paragraph.extend(table_buffer)
    table_buffer.clear()


def ocr_text_to_markdown(text: str) -> str:
    lines = page_lines(text)
    cleaned: list[str] = []
    for line in lines:
        line = re.sub(r"\s+", " ", normalize(line)).strip()
        if not line or re.fullmatch(r"\d{1,4}", line):
            continue
        if re.search(r"annual\s+report|strategic\s+report", line, re.I) and len(line) < 90:
            continue
        if re.search(r"^(british smaller companies vct|afren)\s+p(?:lc|le|ic)?\b", line, re.I) and len(line) < 70:
            continue
        if re.search(r"^board\s+of\s+directors(?:\s+and\s+committees)?\b|^and\s+committees\b", line, re.I):
            continue
        if re.search(r"^(for more information see|read more|see page)\b", line, re.I) and len(line) < 90:
            continue
        cleaned.append(line)
    if not cleaned:
        return ""

    output: list[str] = []
    paragraph: list[str] = []
    table_buffer: list[str] = []
    omitted_tables = 0

    for line in cleaned:
        tableish = is_ocr_tableish_line(line)
        if tableish:
            table_buffer.append(line)
            continue

        before = len(output)
        flush_ocr_table_buffer(output, paragraph, table_buffer)
        if len(output) > before and output[-1] == "[Table omitted: low-confidence OCR table]":
            omitted_tables += 1

        bullet_match = re.match(r"^[•●©@]\s*(.+)", line)
        if bullet_match:
            flush_ocr_paragraph(output, paragraph)
            output.append("- " + bullet_match.group(1).strip())
            continue

        if is_ocr_heading_line(line):
            flush_ocr_paragraph(output, paragraph)
            output.append(f"### {clean_heading_text(line)}")
            continue

        if output and output[-1].startswith("- ") and not re.search(r"[.!?;:]$", output[-1]) and len(line.split()) <= 18:
            output[-1] += " " + line
            continue
        paragraph.append(line)

    before = len(output)
    flush_ocr_table_buffer(output, paragraph, table_buffer)
    if len(output) > before and output[-1] == "[Table omitted: low-confidence OCR table]":
        omitted_tables += 1
    flush_ocr_paragraph(output, paragraph)

    # If the page was mostly a numeric table, the marker alone is safer than a noisy pseudo-table.
    body = "\n\n".join(item for item in output if item.strip())
    if omitted_tables >= 1 and len(output) <= 2 and len(body) < 400:
        return "[Table omitted: low-confidence OCR table]"
    return body


def ocr_pages_to_markdown(texts: list[str], start: int, end: int) -> str:
    chunks: list[str] = []
    for idx in range(start, min(end + 1, len(texts))):
        text = texts[idx]
        if is_ocr_markdown_skip_page(text):
            continue
        body = ocr_text_to_markdown(text)
        if body:
            page_no = idx + 1
            chunks.append(f"<!-- pdf_page: {page_no} -->\n\n## PDF page {page_no}\n\n{body}".strip())
    return "\n\n".join(chunks)


def pages_to_markdown(pdf_path: Path, start: int, end: int, ocr_texts: list[str] | None = None) -> str:
    if ocr_texts is not None:
        return ocr_pages_to_markdown(ocr_texts, start, end)
    doc = fitz.open(pdf_path)
    chunks: list[str] = []
    try:
        for idx in range(start, end + 1):
            page = doc[idx]
            page_text = page.get_text("text") or ""
            if is_toc_page(page_text) or is_report_navigation_page(page_text):
                continue
            chunk = page_to_markdown(page, idx + 1)
            if chunk:
                chunks.append(chunk)
    finally:
        doc.close()
    return "\n\n".join(chunks)


def process_pdf(pdf_path: Path, output_dir: Path, include_standalone_csr: bool = False, ocr: str = "auto", ocr_cache_dir: Path | None = None, ocr_lang: str = "eng", ocr_dpi: int = 220) -> dict[str, str | int]:
    try:
        texts = page_texts(pdf_path, ocr=ocr, ocr_cache_dir=ocr_cache_dir, ocr_lang=ocr_lang, ocr_dpi=ocr_dpi)
    except OcrUnavailableError as exc:
        return {
            "source_pdf": pdf_path.name,
            "status": "ocr_unavailable",
            "sr_start": "",
            "sr_end": "",
            "method": "ocr-unavailable",
            "csr_status": "not-found",
            "warnings": str(exc),
            "output_file": "",
        }
    using_ocr_text = ocr == "always" or not has_text_layer(pdf_path)
    result = find_bounds(texts)
    if result is None:
        return {
            "source_pdf": pdf_path.name,
            "status": "not_found",
            "sr_start": "",
            "sr_end": "",
            "method": "not-found",
            "csr_status": "not-found",
            "warnings": "strategic_report_not_found",
            "output_file": "",
        }

    if include_standalone_csr:
        extra_csr_ranges = find_standalone_csr_ranges(texts, result)
        if extra_csr_ranges:
            result = SectionRange(
                result.start,
                result.end,
                result.method,
                result.csr_ranges + extra_csr_ranges,
                result.warnings + [
                    "standalone_csr_appended_pages_"
                    + ",".join(f"{start + 1}-{end + 1}" for start, end in extra_csr_ranges)
                ],
            )

    status = csr_status(texts, result)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{pdf_path.stem}_strategic_report.md"

    header = (
        f"# Strategic Report - {pdf_path.stem}\n\n"
        f"*Source: {pdf_path.name}*  \n"
        f"*SR pages {result.start + 1}-{result.end + 1} | Detection: {result.method}*  \n"
        f"*CSR/ESG: {status}.*  \n"
        f"*Warnings: {';'.join(result.warnings) if result.warnings else 'none'}*  \n"
        f"*Text source: {'OCR' if using_ocr_text else 'PDF text layer'}*\n\n"
        "## Markdown Handling Notes\n\n"
        "- Narrative text is preserved.\n"
        "- Reliable tables are marked as `[Table]` and rendered as Markdown tables.\n"
        "- Segment KPI cards are marked as `[Segment KPI Summary]` and rendered as compact Markdown tables.\n"
        "- Standalone KPI cards are marked as `[KPI Summary]` and rendered as compact Markdown tables.\n"
        "- Clearly interpretable KPI charts are marked as `[Chart summary]`; low-confidence visual charts are omitted to avoid OCR noise.\n"
        "- Clear project milestone diagrams are marked as `[Milestone summary]` and rendered as compact Markdown tables.\n"
        "- Decorative or non-text visuals are omitted silently.\n"
        "- Dense map labels are omitted when they would only add location-list noise.\n"
        "- Low-value strategy/navigation diagrams are omitted when they only repeat links or page labels.\n"
        "- Source/see-page notes tied to omitted charts are also omitted to avoid dangling references.\n\n"
        "---\n\n"
    )
    body = pages_to_markdown(pdf_path, result.start, result.end, texts if using_ocr_text else None)
    for cs, ce in result.csr_ranges:
        body += (
            "\n\n---\n\n"
            "# CSR / ESG Section\n\n"
            f"*Pages {cs + 1}-{ce + 1}*\n\n"
            + pages_to_markdown(pdf_path, cs, ce, texts if using_ocr_text else None)
        )
    out_path.write_text(header + body, encoding="utf-8")

    return {
        "source_pdf": pdf_path.name,
        "status": "extracted",
        "sr_start": result.start + 1,
        "sr_end": result.end + 1,
        "method": result.method,
        "csr_status": status,
        "warnings": ";".join(result.warnings),
        "output_file": out_path.name,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--metadata-out", default="")
    parser.add_argument("--ocr", choices=["auto", "always", "never"], default="auto", help="Use OCR for scanned PDFs. auto uses OCR only when no text layer is found.")
    parser.add_argument("--ocr-cache-dir", default="", help="Directory for cached OCR page text.")
    parser.add_argument("--ocr-lang", default="eng")
    parser.add_argument("--ocr-dpi", type=int, default=220)
    parser.add_argument(
        "--include-standalone-csr",
        action="store_true",
        help="Append strict CSR/ESG/Sustainability sections found outside the main Strategic Report range.",
    )
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    pdfs = sorted(input_dir.glob("*.pdf"))
    ocr_cache_dir = Path(args.ocr_cache_dir) if args.ocr_cache_dir else None
    rows = [process_pdf(pdf, output_dir, include_standalone_csr=args.include_standalone_csr, ocr=args.ocr, ocr_cache_dir=ocr_cache_dir, ocr_lang=args.ocr_lang, ocr_dpi=args.ocr_dpi) for pdf in pdfs]

    if args.metadata_out:
        metadata_path = Path(args.metadata_out)
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        with metadata_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=[
                    "source_pdf",
                    "status",
                    "sr_start",
                    "sr_end",
                    "method",
                    "csr_status",
                    "warnings",
                    "output_file",
                ],
            )
            writer.writeheader()
            writer.writerows(rows)

    for row in rows:
        print(row)


if __name__ == "__main__":
    main()
