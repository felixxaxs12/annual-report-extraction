# LTO Strategic Report Extractor

Extract Strategic Report and CSR/ESG/Sustainability sections from annual report PDFs, then validate whether the Markdown output contains the desired sections and excludes governance/financial-statement material.

## Goals

- Preserve Strategic Report content, including Chairman's statement, CEO/Chief Executive statement, business review, strategy, risks, KPIs, operating review, financial review inside the Strategic Report, and CSR/ESG/Sustainability content.
- Exclude Corporate Governance, Directors' Report, Board biographies, Remuneration Report, Financial Statements, notes, auditor reports, shareholder information, AGM notices, and appendices.
- Produce a validation report that flags likely extraction errors before LTO coding.

## Quick Start

Step 1: extract Markdown files from annual report PDFs.

```bash
python -m lto_extractor.extract_sections \
  --input-dir data/sample_pdfs \
  --output-dir data/extracted_markdown

# Optional: append strict CSR/ESG sections found outside the main Strategic Report.
python -m lto_extractor.extract_sections \
  --input-dir data/sample_pdfs \
  --output-dir data/extracted_markdown \
  --include-standalone-csr

```

Step 2: validate the extracted Markdown against the source PDFs.

```bash
python -m lto_extractor.auto_validate \
  --pdf-dir data/sample_pdfs \
  --md-dir data/extracted_markdown
```

The extractor writes final Markdown files to `data/extracted_markdown/`. The validator prints a readable summary by default and only writes a CSV if you explicitly pass `--out path.csv`.

Markdown output is optimized for LLM reading:

- narrative text is preserved;
- reliable tables are marked as `[Table]` and rendered as Markdown tables;
- KPI/trend charts are marked as `[Chart summary]` instead of copying raw axis noise;
- decorative/non-text visuals are marked as `[Image omitted]`;
- isolated page numbers, chart axes, and numeric fragments are filtered where possible.

Use the project virtual environment if available:

```bash
/Users/yizhao/Documents/workstudy/accounting_data_research/agent_extractor/.venv/bin/python -m lto_extractor.extract_sections ...
```

## Scanned PDFs and OCR

The extractor now supports scanned annual reports through OCR. By default, `--ocr auto` first checks whether the PDF has a usable text layer. If it does, the old text-layer workflow is used. If not, the code falls back to OCR.

OCR requires the `tesseract` executable. The local project setup currently checks `PATH`, `TESSERACT_CMD`, and the bundled OCR environment at `/Users/yizhao/Documents/Workstudy_Accounting_Data_Managment/.tools/ocr-env/bin/tesseract`. Without a usable backend, scanned PDFs will fail with `ocr_unavailable` instead of producing misleading empty Markdown.

Recommended command for scanned PDFs:

```bash
python -m lto_extractor.auto_validate \
  --input-dir /Users/yizhao/Documents/workstudy/accounting_data_research/PDF_scanned \
  --output-dir data/scanned_test_markdown \
  --audit-out reports/scanned_auto_validation_audit.jsonl \
  --ocr auto \
  --ocr-cache-dir data/ocr_cache
```

OCR options are available in `extract_sections` and `auto_validate`:

- `--ocr auto`: use the PDF text layer when present; otherwise use OCR.
- `--ocr always`: force OCR even when a text layer exists.
- `--ocr never`: never use OCR.
- `--ocr-cache-dir`: store page-level OCR text so reruns do not OCR the same report again.
- `--ocr-lang`: Tesseract language code, default `eng`.
- `--ocr-dpi`: render resolution for OCR, default `220`.

For text-based PDFs, the existing workflow remains unchanged and should continue to pass validation.


## Automated Validation and Self-Repair

Use `auto_validate` with `--md-dir` when you only want to validate existing Markdown files:

```bash
python -m lto_extractor.auto_validate \
  --pdf-dir data/sample_pdfs \
  --md-dir data/extracted_markdown
```

Use `auto_validate` with `--input-dir` and `--output-dir` when you want an agent-like extraction loop instead of a separate extract-then-check workflow:

```bash
python -m lto_extractor.auto_validate \
  --input-dir data/sample_pdfs \
  --output-dir data/extracted_markdown \
  --audit-out reports/auto_validation_audit.jsonl
```

The loop does:

1. detect Strategic Report / CSR boundaries from each PDF;
2. generate Markdown;
3. validate the Markdown against the source PDF and content-quality rules;
4. automatically apply deterministic repairs when safe;
5. regenerate and revalidate until the file passes or needs manual review.

Current automatic repairs include:

- extending a start page when strategic/CSR content is detected immediately before the extracted range;
- extending an end page when strategic/CSR content is detected immediately after the extracted range;
- trimming before hard excluded sections such as governance, remuneration, auditor reports, and financial statements;
- skipping report navigation / contents-style pages that leak headings such as `Financial statements`;
- dropping noisy visual/navigation pages and detached visual footnotes through the extractor rules.

The JSONL audit log is optional. It records each file's final status, iteration count, validation issues, and auto-repair actions.

## Validation Status

- `pass`: boundaries look correct and no substantial excluded section is detected.
- `warning`: likely minor issue or ambiguous boundary requiring spot-check.
- `fail`: likely missing required Strategic Report/CSR content or including excluded sections.
