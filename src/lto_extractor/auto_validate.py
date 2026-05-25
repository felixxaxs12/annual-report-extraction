from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import fitz

from lto_extractor.extract_sections import (
    RE_CSR_FALSE_POSITIVE,
    SectionRange,
    csr_status,
    find_bounds,
    find_standalone_csr_ranges,
    is_csr_like,
    is_front_matter_page,
    is_hard_exclude_boundary,
    is_ocr_markdown_skip_page,
    is_report_navigation_page,
    is_strategic_like,
    is_toc_page,
    page_texts,
    page_to_markdown,
    has_text_layer,
    ocr_text_to_markdown,
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


HANDLING_NOTES = """## Markdown Handling Notes

- Narrative text is preserved.
- Reliable tables are marked as `[Table]` and rendered as Markdown tables.
- Segment KPI cards are marked as `[Segment KPI Summary]` and rendered as compact Markdown tables.
- Standalone KPI cards are marked as `[KPI Summary]` and rendered as compact Markdown tables.
- Clearly interpretable KPI charts are marked as `[Chart summary]`; low-confidence visual charts are omitted to avoid OCR noise.
- Clear project milestone diagrams are marked as `[Milestone summary]` and rendered as compact Markdown tables.
- Decorative or non-text visuals are omitted silently.
- Dense map labels are omitted when they would only add location-list noise.
- Low-value strategy/navigation diagrams are omitted when they only repeat links or page labels.
- Source/see-page notes tied to omitted charts are also omitted to avoid dangling references.

---

"""


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


def validate_markdown_dir(md_dir: Path, pdf_dirs: list[Path], ocr: str = "auto", ocr_cache_dir: Path | None = None, ocr_lang: str = "eng", ocr_dpi: int = 220) -> list[dict[str, str | int]]:
    return [
        validate_one(path, pdf_dirs, ocr=ocr, ocr_cache_dir=ocr_cache_dir, ocr_lang=ocr_lang, ocr_dpi=ocr_dpi)
        for path in sorted(md_dir.glob("*.md"))
    ]


def write_validation_csv(rows: list[dict[str, str | int]], out_path: Path) -> None:
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


def print_validation_summary(rows: list[dict[str, str | int]], label: str = "Validation") -> None:
    counts = {"pass": 0, "warning": 0, "fail": 0}
    for row in rows:
        counts[str(row["status"])] = counts.get(str(row["status"]), 0) + 1
    print(f"{label} summary: pass={counts.get('pass', 0)}, warning={counts.get('warning', 0)}, fail={counts.get('fail', 0)}")
    for row in rows:
        status = str(row["status"])
        if status == "pass":
            print(f"PASS {row['markdown_file']}: pages {row['detected_start']}-{row['detected_end']}; CSR={row['csr_status']}; issues=none")
        else:
            print(f"{status.upper()} {row['markdown_file']}: issues={row['issues']}; suggested_fix={row['suggested_fix']}")


@dataclass
class RepairState:
    pdf_path: Path
    start: int
    end: int
    method: str
    csr_ranges: list[tuple[int, int]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skip_pages: set[int] = field(default_factory=set)  # 1-based PDF page numbers
    actions: list[str] = field(default_factory=list)


@dataclass
class AutoResult:
    source_pdf: str
    output_file: str
    status: str
    iterations: int
    issues: str
    actions: list[str]


def parse_issue_page(issue: str, prefix: str) -> int | None:
    match = re.search(rf"{re.escape(prefix)}_(\d+)", issue)
    return int(match.group(1)) if match else None


def page_markdown_range(pdf_path: Path, start: int, end: int, skip_pages: set[int], ocr_texts: list[str] | None = None) -> str:
    chunks: list[str] = []
    doc = fitz.open(pdf_path)
    try:
        for idx in range(start, end + 1):
            page_no = idx + 1
            page = doc[idx]
            page_text = ocr_texts[idx] if ocr_texts is not None and idx < len(ocr_texts) else (page.get_text("text") or "")
            if page_no in skip_pages:
                continue
            if ocr_texts is not None and is_ocr_markdown_skip_page(page_text):
                continue
            if ocr_texts is None and (is_toc_page(page_text) or is_report_navigation_page(page_text)):
                continue
            if ocr_texts is not None:
                body = ocr_text_to_markdown(page_text)
                chunk = f"<!-- pdf_page: {page_no} -->\n\n## PDF page {page_no}\n\n{body}".strip() if body else ""
            else:
                chunk = page_to_markdown(page, page_no)
            if chunk:
                chunks.append(chunk)
    finally:
        doc.close()
    return "\n\n".join(chunks)


def write_state_markdown(state: RepairState, output_dir: Path, texts: list[str], using_ocr_text: bool) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{state.pdf_path.stem}_strategic_report.md"
    status = csr_status(texts, SectionRange(state.start, state.end, state.method, state.csr_ranges, state.warnings))
    warning_text = ";".join(state.warnings) if state.warnings else "none"
    if state.actions:
        warning_text = (warning_text + ";" if warning_text != "none" else "") + "auto_repaired:" + ",".join(state.actions)
    header = (
        f"# Strategic Report - {state.pdf_path.stem}\n\n"
        f"*Source: {state.pdf_path.name}*  \n"
        f"*SR pages {state.start + 1}-{state.end + 1} | Detection: {state.method}*  \n"
        f"*CSR/ESG: {status}.*  \n"
        f"*Warnings: {warning_text}*  \n"
        f"*Text source: {'OCR' if using_ocr_text else 'PDF text layer'}*\n\n"
        + HANDLING_NOTES
    )
    body = page_markdown_range(state.pdf_path, state.start, state.end, state.skip_pages, texts if using_ocr_text else None)
    for cs, ce in state.csr_ranges:
        body += (
            "\n\n---\n\n"
            "# CSR / ESG Section\n\n"
            f"*Pages {cs + 1}-{ce + 1}*\n\n"
            + page_markdown_range(state.pdf_path, cs, ce, state.skip_pages, texts if using_ocr_text else None)
        )
    out_path.write_text(header + body, encoding="utf-8")
    return out_path


def pdf_page_for_markdown_line(lines: list[str], line_no: int) -> int | None:
    page_no = None
    for idx, line in enumerate(lines[:line_no], start=1):
        match = re.match(r"<!-- pdf_page: (\d+) -->", line)
        if match:
            page_no = int(match.group(1))
    return page_no


def repair_from_validation(state: RepairState, md_path: Path, row: dict[str, Any], texts: list[str]) -> bool:
    changed = False
    issues = str(row.get("issues", ""))
    for issue in issues.split(";"):
        if not issue or issue == "none":
            continue
        page = parse_issue_page(issue, "possible_start_too_late_page")
        if page and page - 1 < state.start:
            state.start = page - 1
            action = f"extend_start_to_page_{page}"
            state.actions.append(action)
            state.warnings.append(action)
            changed = True
            continue
        page = parse_issue_page(issue, "possible_end_too_early_page")
        if page and page - 1 > state.end:
            state.end = page - 1
            action = f"extend_end_to_page_{page}"
            state.actions.append(action)
            state.warnings.append(action)
            changed = True
            continue
        page = parse_issue_page(issue, "excluded_section_inside_range_page")
        if page and page - 2 >= state.start and page - 2 < state.end:
            state.end = page - 2
            action = f"trim_end_before_page_{page}"
            state.actions.append(action)
            state.warnings.append(action)
            changed = True
            continue

    if "substantive_excluded_heading_in_markdown" in issues:
        md_text = md_path.read_text(encoding="utf-8", errors="replace")
        lines = md_text.splitlines()
        for hit in has_substantive_excluded_heading(md_text):
            parts = hit.split(":", 2)
            if len(parts) < 3:
                continue
            line_no = int(parts[1])
            page_no = pdf_page_for_markdown_line(lines, line_no)
            if page_no is None:
                continue
            page_text = texts[page_no - 1] if 0 <= page_no - 1 < len(texts) else ""
            if is_report_navigation_page(page_text) or is_toc_page(page_text):
                if page_no not in state.skip_pages:
                    state.skip_pages.add(page_no)
                    action = f"skip_navigation_page_{page_no}"
                    state.actions.append(action)
                    state.warnings.append(action)
                    changed = True
            elif state.start <= page_no - 1 <= state.end:
                # Conservative fallback: if an excluded heading appears late in the range,
                # trim before that page rather than risk governance/financial statements entering LTO coding.
                if page_no - 2 >= state.start and page_no - 2 < state.end:
                    state.end = page_no - 2
                    action = f"trim_end_before_excluded_heading_page_{page_no}"
                    state.actions.append(action)
                    state.warnings.append(action)
                    changed = True
            if changed:
                break
    return changed


def initial_state(pdf_path: Path, include_standalone_csr: bool, ocr: str, ocr_cache_dir: Path | None, ocr_lang: str, ocr_dpi: int) -> tuple[RepairState | None, list[str], bool]:
    texts = page_texts(pdf_path, ocr=ocr, ocr_cache_dir=ocr_cache_dir, ocr_lang=ocr_lang, ocr_dpi=ocr_dpi)
    using_ocr_text = ocr == "always" or not has_text_layer(pdf_path)
    bounds = find_bounds(texts)
    if bounds is None:
        return None, texts, using_ocr_text
    csr_ranges = list(bounds.csr_ranges)
    warnings = list(bounds.warnings)
    if include_standalone_csr:
        extra = find_standalone_csr_ranges(texts, bounds)
        if extra:
            csr_ranges.extend(extra)
            warnings.append("standalone_csr_appended_pages_" + ",".join(f"{s + 1}-{e + 1}" for s, e in extra))
    return RepairState(pdf_path, bounds.start, bounds.end, bounds.method, csr_ranges, warnings), texts, using_ocr_text


def auto_process_pdf(pdf_path: Path, output_dir: Path, pdf_dirs: list[Path], max_iterations: int, include_standalone_csr: bool, ocr: str, ocr_cache_dir: Path | None, ocr_lang: str, ocr_dpi: int) -> AutoResult:
    try:
        state, texts, using_ocr_text = initial_state(pdf_path, include_standalone_csr, ocr, ocr_cache_dir, ocr_lang, ocr_dpi)
    except OcrUnavailableError as exc:
        return AutoResult(pdf_path.name, "", "fail", 0, f"ocr_unavailable: {exc}", [])
    if state is None:
        return AutoResult(pdf_path.name, "", "fail", 0, "strategic_report_not_found", [])

    md_path = output_dir / f"{pdf_path.stem}_strategic_report.md"
    last_row: dict[str, Any] | None = None
    for iteration in range(1, max_iterations + 1):
        md_path = write_state_markdown(state, output_dir, texts, using_ocr_text)
        row = validate_one(md_path, pdf_dirs, ocr=ocr, ocr_cache_dir=ocr_cache_dir, ocr_lang=ocr_lang, ocr_dpi=ocr_dpi)
        last_row = row
        if row["status"] == "pass":
            return AutoResult(pdf_path.name, md_path.name, "pass", iteration, str(row["issues"]), state.actions)
        changed = repair_from_validation(state, md_path, row, texts)
        if not changed:
            return AutoResult(pdf_path.name, md_path.name, str(row["status"]), iteration, str(row["issues"]), state.actions)

    md_path = write_state_markdown(state, output_dir, texts, using_ocr_text)
    row = validate_one(md_path, pdf_dirs, ocr=ocr, ocr_cache_dir=ocr_cache_dir, ocr_lang=ocr_lang, ocr_dpi=ocr_dpi)
    last_row = row
    return AutoResult(pdf_path.name, md_path.name, str(row["status"]), max_iterations, str(row["issues"]), state.actions)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", default="", help="PDF directory for extract + repair validation mode.")
    parser.add_argument("--output-dir", default="", help="Markdown output directory for extract + repair validation mode.")
    parser.add_argument("--md-dir", default="", help="Existing Markdown directory for validation-only mode.")
    parser.add_argument("--pdf-dir", action="append", default=[])
    parser.add_argument("--max-iterations", type=int, default=4)
    parser.add_argument("--include-standalone-csr", action="store_true")
    parser.add_argument("--audit-out", default="", help="Optional JSONL audit log path.")
    parser.add_argument("--out", default="", help="Optional CSV path for validation-only mode.")
    parser.add_argument("--ocr", choices=["auto", "always", "never"], default="auto")
    parser.add_argument("--ocr-cache-dir", default="")
    parser.add_argument("--ocr-lang", default="eng")
    parser.add_argument("--ocr-dpi", type=int, default=220)
    args = parser.parse_args()

    ocr_cache_dir = Path(args.ocr_cache_dir) if args.ocr_cache_dir else None
    if args.md_dir:
        if not args.pdf_dir:
            parser.error("--pdf-dir is required when using --md-dir validation-only mode")
        rows = validate_markdown_dir(
            Path(args.md_dir),
            [Path(p) for p in args.pdf_dir],
            ocr=args.ocr,
            ocr_cache_dir=ocr_cache_dir,
            ocr_lang=args.ocr_lang,
            ocr_dpi=args.ocr_dpi,
        )
        if args.out:
            write_validation_csv(rows, Path(args.out))
        print_validation_summary(rows)
        return

    if not args.input_dir or not args.output_dir:
        parser.error("Use either --md-dir with --pdf-dir, or --input-dir with --output-dir.")

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    pdf_dirs = [Path(p) for p in args.pdf_dir] or [input_dir]
    results = [
        auto_process_pdf(pdf_path, output_dir, pdf_dirs, args.max_iterations, args.include_standalone_csr, args.ocr, ocr_cache_dir, args.ocr_lang, args.ocr_dpi)
        for pdf_path in sorted(input_dir.glob("*.pdf"))
    ]

    counts = {"pass": 0, "warning": 0, "fail": 0}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    print(f"Auto-validation summary: pass={counts.get('pass', 0)}, warning={counts.get('warning', 0)}, fail={counts.get('fail', 0)}")
    for result in results:
        action_text = ",".join(result.actions) if result.actions else "none"
        print(f"{result.status.upper()} {result.output_file or result.source_pdf}: iterations={result.iterations}; issues={result.issues}; actions={action_text}")

    if args.audit_out:
        audit_path = Path(args.audit_out)
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with audit_path.open("w", encoding="utf-8") as fh:
            for result in results:
                fh.write(json.dumps(result.__dict__, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
