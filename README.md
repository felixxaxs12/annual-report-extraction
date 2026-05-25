# LTO Strategic Report Extractor

Extract Strategic Report and CSR/ESG/Sustainability sections from annual report PDFs, then validate whether the Markdown output contains the desired sections and excludes governance/financial-statement material.

## Goals

- Preserve Strategic Report content, including Chairman's statement, CEO/Chief Executive statement, business review, strategy, risks, KPIs, operating review, financial review inside the Strategic Report, and CSR/ESG/Sustainability content.
- Exclude Corporate Governance, Directors' Report, Board biographies, Remuneration Report, Financial Statements, notes, auditor reports, shareholder information, AGM notices, and appendices.
- Produce a validation report that flags likely extraction errors before LTO coding.

## Quick Start

Clone the repository, install dependencies, and create the working folders:

```bash
git clone https://github.com/felixxaxs12/lto-section-extractor.git
cd lto-section-extractor

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

mkdir -p data/input_pdfs data/extracted_markdown data/ocr_cache reports
```

Put the annual report PDFs to process in:

```text
data/input_pdfs/
```

Step 1: extract Markdown files from annual report PDFs.

```bash
PYTHONPATH=src \
python -m lto_extractor.extract_sections \
  --input-dir data/input_pdfs \
  --output-dir data/extracted_markdown \
  --ocr auto \
  --ocr-cache-dir data/ocr_cache

# Optional: append strict CSR/ESG sections found outside the main Strategic Report.
PYTHONPATH=src \
python -m lto_extractor.extract_sections \
  --input-dir data/input_pdfs \
  --output-dir data/extracted_markdown \
  --ocr auto \
  --ocr-cache-dir data/ocr_cache \
  --include-standalone-csr
```

Step 2: validate the extracted Markdown against the source PDFs.

```bash
PYTHONPATH=src \
python -m lto_extractor.auto_validate \
  --pdf-dir data/input_pdfs \
  --md-dir data/extracted_markdown \
  --ocr auto \
  --ocr-cache-dir data/ocr_cache
```

The extractor writes final Markdown files to `data/extracted_markdown/`. The validator prints a readable summary by default and only writes a CSV if you explicitly pass `--out path.csv`.

Markdown output is optimized for LLM reading:

- narrative text is preserved;
- reliable tables are marked as `[Table]` and rendered as Markdown tables;
- KPI/trend charts are marked as `[Chart summary]` instead of copying raw axis noise;
- decorative/non-text visuals are marked as `[Image omitted]`;
- isolated page numbers, chart axes, and numeric fragments are filtered where possible.

## Scanned PDFs and OCR

The extractor now supports scanned annual reports through OCR. By default, `--ocr auto` first checks whether the PDF has a usable text layer. If it does, the old text-layer workflow is used. If not, the code falls back to OCR.

OCR requires the `tesseract` executable. Install it separately and make sure it is available on `PATH`. Alternatively, set `TESSERACT_CMD` to the full path of the executable. Without a usable backend, scanned PDFs will fail with `ocr_unavailable` instead of producing misleading empty Markdown.

Common install options:

```bash
# macOS with Homebrew
brew install tesseract

# Conda / Mamba
conda install -c conda-forge tesseract
```

Recommended command for scanned PDFs:

```bash
PYTHONPATH=src \
python -m lto_extractor.auto_validate \
  --input-dir data/input_pdfs \
  --output-dir data/extracted_markdown \
  --audit-out reports/auto_validation_audit.jsonl \
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
PYTHONPATH=src \
python -m lto_extractor.auto_validate \
  --pdf-dir data/input_pdfs \
  --md-dir data/extracted_markdown \
  --ocr auto \
  --ocr-cache-dir data/ocr_cache
```

Use `auto_validate` with `--input-dir` and `--output-dir` when you want an agent-like extraction loop instead of a separate extract-then-check workflow:

```bash
PYTHONPATH=src \
python -m lto_extractor.auto_validate \
  --input-dir data/input_pdfs \
  --output-dir data/extracted_markdown \
  --audit-out reports/auto_validation_audit.jsonl \
  --ocr auto \
  --ocr-cache-dir data/ocr_cache
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
