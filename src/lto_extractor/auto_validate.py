from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import fitz

from lto_extractor.extract_sections import (
    SectionRange,
    csr_status,
    find_bounds,
    find_standalone_csr_ranges,
    is_ocr_markdown_skip_page,
    is_report_navigation_page,
    is_toc_page,
    page_texts,
    page_to_markdown,
    has_text_layer,
    ocr_text_to_markdown,
    OcrUnavailableError,
)
from lto_extractor.validate_extractions import (
    SOURCE_RE,
    find_pdf,
    has_substantive_excluded_heading,
    validate_one,
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
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--pdf-dir", action="append", default=[])
    parser.add_argument("--max-iterations", type=int, default=4)
    parser.add_argument("--include-standalone-csr", action="store_true")
    parser.add_argument("--audit-out", default="", help="Optional JSONL audit log path.")
    parser.add_argument("--ocr", choices=["auto", "always", "never"], default="auto")
    parser.add_argument("--ocr-cache-dir", default="")
    parser.add_argument("--ocr-lang", default="eng")
    parser.add_argument("--ocr-dpi", type=int, default=220)
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    pdf_dirs = [Path(p) for p in args.pdf_dir] or [input_dir]
    ocr_cache_dir = Path(args.ocr_cache_dir) if args.ocr_cache_dir else None
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
