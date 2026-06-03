from __future__ import annotations

import argparse
import csv
import re
from dataclasses import dataclass
from pathlib import Path


SOURCE_RE = re.compile(r"^\*Source:\s+([^*]+)\*", re.MULTILINE)
HEADER_RE = re.compile(
    r"^\*SR pages\s+(\d+)[–-](\d+)\s+\|\s+Detection:\s+([^*]+)\*",
    re.MULTILINE,
)
CSR_RE = re.compile(r"^\*CSR/ESG:\s+([^*]+)\*", re.MULTILINE)
PAGE_MARKER_RE = re.compile(r"<!--\s*pdf_page:\s*(\d+)\s*-->")

EXCLUDED_HEADING_RE = re.compile(
    r"^#{1,6}\s+.*("
    r"Corporate Governance|Directors['’]? Report|Board of Directors|"
    r"Remuneration Report|Financial Statements|Independent Auditor|"
    r"Audit Committee|Nomination[s]? Committee|Financial Reporting"
    r")",
    re.IGNORECASE | re.MULTILINE,
)
CSR_FALSE_POSITIVE_RE = re.compile(
    r"csr\s+committee|"
    r"corporate\s+(safety\s+and\s+)?social\s+responsibility\s+committee|"
    r"committee\s+report|"
    r"remuneration\s+committee|"
    r"nomination[s]?\s+committee|"
    r"audit\s+committee|"
    r"financial\s+reporting|"
    r"board\s+composition",
    re.IGNORECASE,
)

CSV_FIELDS = [
    "markdown_file",
    "source_pdf",
    "detected_start",
    "detected_end",
    "method",
    "csr_status",
    "issues",
    "suggested_fix",
]


@dataclass(frozen=True)
class MarkdownMetadata:
    source_pdf: str = ""
    detected_start: str = ""
    detected_end: str = ""
    method: str = ""
    csr_status: str = ""

    @property
    def has_page_range(self) -> bool:
        return self.detected_start.isdigit() and self.detected_end.isdigit()

    @property
    def start_page(self) -> int:
        return int(self.detected_start)

    @property
    def end_page(self) -> int:
        return int(self.detected_end)


def parse_metadata(md_text: str) -> tuple[MarkdownMetadata, list[str], list[str]]:
    source_match = SOURCE_RE.search(md_text)
    header_match = HEADER_RE.search(md_text)
    csr_match = CSR_RE.search(md_text)
    issues: list[str] = []
    fixes: list[str] = []

    source_pdf = source_match.group(1).strip() if source_match else ""
    detected_start = header_match.group(1).strip() if header_match else ""
    detected_end = header_match.group(2).strip() if header_match else ""
    method = header_match.group(3).strip() if header_match else ""
    csr_status = csr_match.group(1).strip() if csr_match else ""

    if not source_match or not header_match:
        issues.append("missing_header_metadata")
        fixes.append("regenerate_markdown_with_standard_header")
    if not csr_match:
        issues.append("missing_csr_metadata")
        fixes.append("regenerate_markdown_with_standard_header")

    metadata = MarkdownMetadata(
        source_pdf=source_pdf,
        detected_start=detected_start,
        detected_end=detected_end,
        method=method,
        csr_status=csr_status,
    )

    if metadata.has_page_range and metadata.start_page > metadata.end_page:
        issues.append("invalid_declared_page_range")
        fixes.append("regenerate_markdown_with_valid_page_range")

    return metadata, issues, fixes


def page_markers(md_text: str) -> list[int]:
    return [int(match.group(1)) for match in PAGE_MARKER_RE.finditer(md_text)]


def validate_page_markers(
    metadata: MarkdownMetadata,
    markers: list[int],
) -> tuple[list[str], list[str]]:
    issues: list[str] = []
    fixes: list[str] = []

    if not metadata.has_page_range or metadata.start_page > metadata.end_page:
        return issues, fixes

    if not markers:
        issues.append("missing_page_markers")
        fixes.append("regenerate_markdown_with_page_markers")
        return issues, fixes

    declared_pages = set(range(metadata.start_page, metadata.end_page + 1))
    marker_pages = set(markers)

    outside_pages = sorted(marker_pages - declared_pages)
    missing_pages = sorted(declared_pages - marker_pages)

    for page_no in outside_pages:
        issues.append(f"page_marker_outside_declared_range_page_{page_no}")
    if outside_pages:
        fixes.append("review_or_remove_out_of_range_page_markers")

    for page_no in missing_pages:
        issues.append(f"declared_page_missing_marker_page_{page_no}")
    if missing_pages:
        fixes.append("review_missing_declared_pages")

    return issues, fixes


def excluded_heading_hits(md_text: str) -> list[str]:
    hits = []
    lines = md_text.splitlines()
    for i, line in enumerate(lines):
        if not EXCLUDED_HEADING_RE.search(line):
            continue
        window = "\n".join(lines[i : i + 8])
        if "read more page" in window.lower() or "see page" in window.lower():
            hits.append(f"minor:{i + 1}:{line.strip()}")
        else:
            hits.append(f"substantive:{i + 1}:{line.strip()}")
    return hits


def validate_excluded_headings(md_text: str) -> tuple[list[str], list[str]]:
    hits = excluded_heading_hits(md_text)
    issues: list[str] = []
    fixes: list[str] = []

    if any(hit.startswith("substantive:") for hit in hits):
        issues.append("substantive_excluded_heading_in_markdown")
        fixes.append("trim_or_reclassify_excluded_section")
    elif any(hit.startswith("minor:") for hit in hits):
        issues.append("minor_excluded_reference_heading_in_markdown")
        fixes.append("review_excluded_reference_context")

    return issues, fixes


def validate_csr_false_positive(
    md_text: str,
    metadata: MarkdownMetadata,
) -> tuple[list[str], list[str]]:
    if "found" not in metadata.csr_status.lower():
        return [], []
    if not CSR_FALSE_POSITIVE_RE.search(md_text):
        return [], []
    return (
        ["standalone_csr_false_positive_risk"],
        ["drop_csr_range_if_committee_governance_context"],
    )


def unique_join(values: list[str]) -> str:
    return ";".join(dict.fromkeys(values)) if values else "none"


def validate_one(md_path: Path) -> dict[str, str]:
    md_text = md_path.read_text(encoding="utf-8", errors="replace")
    metadata, issues, fixes = parse_metadata(md_text)

    marker_issues, marker_fixes = validate_page_markers(metadata, page_markers(md_text))
    issues.extend(marker_issues)
    fixes.extend(marker_fixes)

    heading_issues, heading_fixes = validate_excluded_headings(md_text)
    issues.extend(heading_issues)
    fixes.extend(heading_fixes)

    csr_issues, csr_fixes = validate_csr_false_positive(md_text, metadata)
    issues.extend(csr_issues)
    fixes.extend(csr_fixes)

    return {
        "markdown_file": md_path.name,
        "source_pdf": metadata.source_pdf,
        "detected_start": metadata.detected_start,
        "detected_end": metadata.detected_end,
        "method": metadata.method,
        "csr_status": metadata.csr_status,
        "issues": unique_join(issues),
        "suggested_fix": unique_join(fixes),
    }


def validate_markdown_dir(md_dir: Path) -> list[dict[str, str]]:
    return [validate_one(path) for path in sorted(md_dir.glob("*.md"))]


def write_validation_csv(rows: list[dict[str, str]], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def print_validation_summary(rows: list[dict[str, str]]) -> None:
    issue_count = sum(1 for row in rows if row["issues"] != "none")
    print(f"Markdown validation summary: files={len(rows)}, with_issues={issue_count}")
    for row in rows:
        if row["issues"] == "none":
            print(
                f"PASS {row['markdown_file']}: "
                f"pages {row['detected_start']}-{row['detected_end']}; "
                f"CSR={row['csr_status']}; issues=none"
            )
        else:
            print(
                f"ISSUES {row['markdown_file']}: "
                f"issues={row['issues']}; suggested_fix={row['suggested_fix']}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate extracted Strategic Report Markdown without reading source PDFs."
    )
    parser.add_argument("--md-dir", required=True, help="Directory containing extracted Markdown files.")
    parser.add_argument("--out", default="", help="Optional CSV output path.")
    args = parser.parse_args()

    rows = validate_markdown_dir(Path(args.md_dir))
    if args.out:
        write_validation_csv(rows, Path(args.out))
    print_validation_summary(rows)


if __name__ == "__main__":
    main()
