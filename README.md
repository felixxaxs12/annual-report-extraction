# LTO Section Extractor

Extract machine-readable Long-Term Orientation (LTO) input from annual-report Docling JSON.

The current workflow is JSON-first:

```text
Docling JSON
-> Strategic Report / Governance page windows
-> group/table/list preserving chunks
-> high-confidence noise marked as delete
-> Markdown for review
-> canonical JSON for RAG
```

Archived PDF-first experiments live in `versions/legacy_pdf_first/`.

## Current Scope

The active extractor:

- reads `*.docling.json` annual-report files;
- extracts Strategic Report and Corporate Governance sections;
- stops before Financial Statements, auditor reports, AGM/shareholder administration, and similar back matter;
- preserves Docling tables;
- preserves Docling groups as grouped blocks instead of splitting them into isolated fragments;
- drops pictures and text nested under pictures;
- marks only high-confidence noise as `status: "delete"`;
- keeps all non-deleted content as `status: "kept"`.

## Install

The current Docling extractor uses only the Python standard library.

```bash
git clone https://github.com/felixxaxs12/lto-section-extractor.git
cd lto-section-extractor

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

Put Docling JSON files in:

```text
data/input_docling_json/
```

Run extraction:

```bash
PYTHONPATH=src python3 -m lto_extractor.extract_docling_sections \
  --input-dir data/input_docling_json \
  --output-dir data/extracted_lto_inputs
```

Each report produces:

```text
<report>_lto_input.md
<report>_lto_document.json
```

## JSON Output

The canonical JSON is intentionally small:

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

`status` is the only quality gate in the output:

- `kept`: usable input for Markdown review and default RAG ingestion.
- `delete`: high-confidence noise retained in JSON for audit, but omitted from Markdown.

## Tests

```bash
PYTHONPATH=src python3 -B -m pytest -q
```

## Repository Layout

```text
src/lto_extractor/extract_docling_sections.py   # active extractor
tests/test_extract_docling_sections.py          # active tests
versions/legacy_pdf_first/                     # archived PDF-first implementation
data/                                          # ignored local inputs/outputs
reports/                                       # ignored local reports
```
