from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path

import fitz

from lto_extractor.extract_sections import (
    RE_CSR_FALSE_POSITIVE,
    RE_EXCLUDE,
    is_csr_like,
    is_hard_exclude_boundary,
    is_front_matter_page,
    is_strategic_like,
    page_lines,
    page_texts,
    OcrUnavailableError,
)


HEADER_RE = re.compile(
    r"^\*SR pages\s+(\d+)[–-](\d+)\s+\|\s+Detection:\s+([^*]+)\*",
    re.MULTILINE,
)
SOURCE_RE = re.compile(r"^\*Source:\s+([^*]+)\*", re.MULTILINE)
CSR_RE = re.compile(r"^\*CSR/ESG:\s+([^*]+)\*", re.MULTILINE)
MD_EXCLUDE_HEADING_RE = re.compile(
    r"^#{1,6}\s+.*("
    r"Corporate Governance|Directors['’]? Report|Board of Directors|"
    r"Remuneration Report|Financial Statements|Independent Auditor|"
    r"Audit Committee|Nomination[s]? Committee|Financial Reporting"
    r")",
    re.IGNORECASE | re.MULTILINE,
)


def read_pdf_pages(pdf_path: Path, ocr: str = "auto", ocr_cache_dir: Path | None = None, ocr_lang: str = "eng", ocr_dpi: int = 220) -> list[str]:
    return page_texts(pdf_path, ocr=ocr, ocr_cache_dir=ocr_cache_dir, ocr_lang=ocr_lang, ocr_dpi=ocr_dpi)


def find_pdf(source: str, pdf_dirs: list[Path]) -> Path | None:
    for pdf_dir in pdf_dirs:
        candidate = pdf_dir / source
        if candidate.exists():
            return candidate
    return None


def has_substantive_excluded_heading(md_text: str) -> list[str]:
    hits = []
    lines = md_text.splitlines()
    for i, line in enumerate(lines):
        if not MD_EXCLUDE_HEADING_RE.search(line):
            continue
        window = "\n".join(lines[i : i + 8])
        if "read more page" in window.lower() or "see page" in window.lower():
            hits.append(f"minor:{i + 1}:{line.strip()}")
        else:
            hits.append(f"substantive:{i + 1}:{line.strip()}")
    return hits


def validate_one(md_path: Path, pdf_dirs: list[Path], ocr: str = "auto", ocr_cache_dir: Path | None = None, ocr_lang: str = "eng", ocr_dpi: int = 220) -> dict[str, str | int]:
    md_text = md_path.read_text(encoding="utf-8", errors="replace")
    source_match = SOURCE_RE.search(md_text)
    header_match = HEADER_RE.search(md_text)
    csr_match = CSR_RE.search(md_text)

    if not source_match or not header_match:
        return {
            "markdown_file": md_path.name,
            "source_pdf": "",
            "status": "fail",
            "severity": "high",
            "detected_start": "",
            "detected_end": "",
            "method": "",
            "csr_status": "",
            "issues": "missing_header_metadata",
            "suggested_fix": "regenerate_markdown_with_standard_header",
        }

    source = source_match.group(1).strip()
    start = int(header_match.group(1))
    end = int(header_match.group(2))
    method = header_match.group(3).strip()
    csr_status = csr_match.group(1).strip() if csr_match else ""
    pdf_path = find_pdf(source, pdf_dirs)

    if pdf_path is None:
        return {
            "markdown_file": md_path.name,
            "source_pdf": source,
            "status": "fail",
            "severity": "high",
            "detected_start": start,
            "detected_end": end,
            "method": method,
            "csr_status": csr_status,
            "issues": "source_pdf_not_found",
            "suggested_fix": "place_pdf_in_one_of_the_pdf_dirs",
        }

    try:
        texts = read_pdf_pages(pdf_path, ocr=ocr, ocr_cache_dir=ocr_cache_dir, ocr_lang=ocr_lang, ocr_dpi=ocr_dpi)
    except OcrUnavailableError as exc:
        return {
            "markdown_file": md_path.name,
            "source_pdf": source,
            "status": "fail",
            "severity": "high",
            "detected_start": start,
            "detected_end": end,
            "method": method,
            "csr_status": csr_status,
            "issues": "ocr_unavailable",
            "suggested_fix": str(exc),
        }
    issues: list[str] = []
    fixes: list[str] = []

    if start > 1:
        for page_no in range(max(1, start - 6), start):
            text = texts[page_no - 1]
            if not is_front_matter_page(text) and (is_strategic_like(text) or is_csr_like(text)):
                issues.append(f"possible_start_too_late_page_{page_no}")
                fixes.append(f"review_extend_start_to_{page_no}")
                break

    for page_no in range(start, min(end, len(texts)) + 1):
        if is_hard_exclude_boundary(texts[page_no - 1]):
            issues.append(f"excluded_section_inside_range_page_{page_no}")
            fixes.append(f"trim_end_before_{page_no}")
            break

    if end < len(texts):
        for page_no in range(end + 1, min(len(texts), end + 8) + 1):
            text = texts[page_no - 1]
            if is_hard_exclude_boundary(text):
                break
            if is_strategic_like(text) or is_csr_like(text):
                issues.append(f"possible_end_too_early_page_{page_no}")
                fixes.append(f"review_extend_end_to_{page_no}")
                break

    heading_hits = has_substantive_excluded_heading(md_text)
    substantive_hits = [hit for hit in heading_hits if hit.startswith("substantive:")]
    minor_hits = [hit for hit in heading_hits if hit.startswith("minor:")]
    if substantive_hits:
        issues.append("substantive_excluded_heading_in_markdown")
        fixes.append("trim_or_reclassify_excluded_section")
    elif minor_hits:
        issues.append("minor_excluded_reference_heading_in_markdown")

    if "found" in csr_status.lower() and RE_CSR_FALSE_POSITIVE.search(md_text):
        issues.append("standalone_csr_false_positive_risk")
        fixes.append("drop_csr_range_if_committee_governance_context")

    if end - start + 1 < 5:
        issues.append("suspiciously_short_extraction")
    if end - start + 1 > 120:
        issues.append("suspiciously_long_extraction")

    high_issue = any(
        issue.startswith(("possible_start_too_late", "possible_end_too_early", "excluded_section_inside_range"))
        or issue in {"substantive_excluded_heading_in_markdown", "standalone_csr_false_positive_risk"}
        for issue in issues
    )
    if high_issue:
        status = "fail"
        severity = "high"
    elif issues:
        status = "warning"
        severity = "medium"
    else:
        status = "pass"
        severity = "low"

    return {
        "markdown_file": md_path.name,
        "source_pdf": source,
        "status": status,
        "severity": severity,
        "detected_start": start,
        "detected_end": end,
        "method": method,
        "csr_status": csr_status,
        "issues": ";".join(issues) if issues else "none",
        "suggested_fix": ";".join(dict.fromkeys(fixes)) if fixes else "none",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf-dir", action="append", required=True)
    parser.add_argument("--md-dir", required=True)
    parser.add_argument("--out", default="", help="Optional CSV path. If omitted, no CSV is written.")
    parser.add_argument("--ocr", choices=["auto", "always", "never"], default="auto")
    parser.add_argument("--ocr-cache-dir", default="")
    parser.add_argument("--ocr-lang", default="eng")
    parser.add_argument("--ocr-dpi", type=int, default=220)
    args = parser.parse_args()

    pdf_dirs = [Path(path) for path in args.pdf_dir]
    md_dir = Path(args.md_dir)
    ocr_cache_dir = Path(args.ocr_cache_dir) if args.ocr_cache_dir else None
    rows = [validate_one(path, pdf_dirs, ocr=args.ocr, ocr_cache_dir=ocr_cache_dir, ocr_lang=args.ocr_lang, ocr_dpi=args.ocr_dpi) for path in sorted(md_dir.glob("*.md"))]

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=[
                    "markdown_file",
                    "source_pdf",
                    "status",
                    "severity",
                    "detected_start",
                    "detected_end",
                    "method",
                    "csr_status",
                    "issues",
                    "suggested_fix",
                ],
            )
            writer.writeheader()
            writer.writerows(rows)

    counts = {"pass": 0, "warning": 0, "fail": 0}
    for row in rows:
        counts[str(row["status"])] = counts.get(str(row["status"]), 0) + 1
    print(f"Validation summary: pass={counts.get('pass', 0)}, warning={counts.get('warning', 0)}, fail={counts.get('fail', 0)}")
    print("Logic: check header metadata -> locate source PDF -> inspect nearby boundaries -> scan extracted Markdown for excluded headings -> flag length/CSR false-positive risks.")
    for row in rows:
        status = row["status"]
        if status == "pass":
            print(f"PASS {row['markdown_file']}: pages {row['detected_start']}-{row['detected_end']}; CSR={row['csr_status']}; issues=none")
        else:
            print(f"{str(status).upper()} {row['markdown_file']}: issues={row['issues']}; suggested_fix={row['suggested_fix']}")


if __name__ == "__main__":
    main()

