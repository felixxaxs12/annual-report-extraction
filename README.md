# LTO Section Extractor

This repository extracts machine-readable inputs from annual-report Docling JSON files for Long-Term Orientation (LTO) research.

The current pipeline is **Docling JSON-first**. It does not parse PDFs directly. Convert annual-report PDFs to Docling JSON first, then use this extractor to keep the report sections that are useful for downstream RAG and LLM-based LTO analysis.

```text
Docling JSON
-> Strategic Report and Governance page windows
-> grouped chunks that preserve Docling tables and groups
-> high-confidence noise marked as delete
-> Markdown for human review
-> canonical JSON for RAG ingestion
```

The old PDF-first extractor is archived under `versions/legacy_pdf_first/`.

## What It Extracts

The active extractor keeps:

- Strategic Report content, including strategy, business review, risk, operating review, KPIs, and related narrative.
- Corporate Governance content, including board oversight, committee narrative, governance policy, and governance-related risk discussion.
- Tables that fall inside the retained Strategic Report or Governance page windows.
- Docling groups as grouped blocks, so timeline-like, KPI-like, and layout-heavy content is not split into isolated fragments when Docling has already grouped it.

The extractor excludes:

- Financial Statements.
- Notes to the financial statements.
- Auditor reports.
- AGM notices and shareholder administration.
- Pictures and text nested under pictures.
- High-confidence noise such as repeated report footers, isolated units, isolated page-like numbers, and short labels with no usable context.

The deletion policy is intentionally conservative: content is deleted only when it is highly likely to be noise. Ambiguous fragments are kept so that later RAG or review steps can decide whether they matter.

## Install

The current Docling JSON extractor uses only the Python standard library.

```bash
git clone https://github.com/felixxaxs12/lto-section-extractor.git
cd lto-section-extractor

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Input

Put Docling JSON files in a local input folder, for example:

```text
data/input_docling_json/
```

Expected file pattern:

```text
*.docling.json
```

Input and output folders under `data/` are ignored by git.

## Run

```bash
PYTHONPATH=src python3 -m lto_extractor.extract_docling_sections \
  --input-dir data/input_docling_json \
  --output-dir data/extracted_lto_inputs
```

If your Docling JSON files are stored outside the repository, pass the absolute path:

```bash
PYTHONPATH=src python3 -m lto_extractor.extract_docling_sections \
  --input-dir /path/to/docling_json \
  --output-dir data/extracted_lto_inputs
```

Each input report produces two files:

```text
<report>_lto_input.md
<report>_lto_document.json
```

## Outputs

### Markdown

The Markdown file is for human review and prompt debugging. It contains only chunks with:

```json
"status": "kept"
```

Deleted noise is omitted from Markdown so the document remains readable.

### JSON

The JSON file is the canonical machine-readable output. It includes both kept chunks and deleted chunks so the filtering decision is auditable.

```json
{
  "schema_version": "docling_lto_document_v2",
  "source_pdf": "Non-Stopper_Afren PLC_20131231.pdf",
  "sections": [
    {
      "section_type": "strategic_report",
      "start_page": 2,
      "end_page": 13,
      "chunks": [
        {
          "chunk_id": "strategic_report_00001",
          "status": "kept",
          "content_type": "paragraph",
          "page_no": 2,
          "heading_path": ["Strategic report"],
          "text_md": "The Board reviews long-term strategy and principal risks."
        }
      ]
    }
  ]
}
```

Chunk fields:

- `chunk_id`: stable identifier within the extracted section.
- `status`: `kept` or `delete`.
- `content_type`: simplified output type such as `paragraph`, `heading`, `list_item`, `table`, or `group`.
- `page_no`: source PDF page number from Docling provenance.
- `heading_path`: nearest retained section heading context.
- `text_md`: Markdown-compatible chunk text.

## RAG Usage

For vector database ingestion, use chunks where:

```text
status = kept
```

Recommended metadata:

```text
source_pdf
section_type
page_no
heading_path
content_type
chunk_id
```

Use `text_md` as the document text. For better retrieval, prepend section and heading context during embedding, for example:

```text
Section: governance
Heading: Board responsibilities
Page: 42

The Board reviews the Group's principal risks and long-term strategic priorities.
```

## Repository Layout

```text
src/lto_extractor/extract_docling_sections.py   # active Docling JSON extractor
tests/test_extract_docling_sections.py          # active tests
versions/legacy_pdf_first/                     # archived PDF-first implementation
data/                                          # ignored local inputs and outputs
reports/                                       # ignored local reports
```

## Tests

```bash
PYTHONPATH=src python3 -B -m pytest -q
```

The current tests cover:

- Strategic Report and Governance boundary extraction.
- Picture dropping.
- Table preservation.
- Docling group preservation.
- Conservative high-confidence-noise deletion.
- Markdown and canonical JSON output generation.
