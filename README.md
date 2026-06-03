# LTO Strategic Report Extractor

Extract Strategic Report and CSR/ESG/Sustainability sections from annual report PDFs, then validate whether the Markdown output is internally consistent and excludes obvious governance/financial-statement material.

## Goals

- Preserve Strategic Report content, including Chairman's statement, CEO/Chief Executive statement, business review, strategy, risks, KPIs, operating review, financial review inside the Strategic Report, and CSR/ESG/Sustainability content.
- Exclude Corporate Governance, Directors' Report, Board biographies, Remuneration Report, Financial Statements, notes, auditor reports, shareholder information, AGM notices, and appendices.
- Produce a Markdown validation report that flags metadata/page-marker inconsistencies and obvious excluded-section pollution before LTO coding.

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

Step 2: validate the extracted Markdown artifacts.

```bash
PYTHONPATH=src \
python -m lto_extractor.validator \
  --md-dir data/extracted_markdown \
  --out reports/markdown_validation.csv
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

OCR options are available in `extract_sections`:

- `--ocr auto`: use the PDF text layer when present; otherwise use OCR.
- `--ocr always`: force OCR even when a text layer exists.
- `--ocr never`: never use OCR.
- `--ocr-cache-dir`: store page-level OCR text so reruns do not OCR the same report again.
- `--ocr-lang`: Tesseract language code, default `eng`.
- `--ocr-dpi`: render resolution for OCR, default `220`.

For text-based PDFs, the existing workflow remains unchanged and should continue to pass validation.


## Markdown-Only Validation

Use `validator` to validate existing Markdown files:

```bash
PYTHONPATH=src \
python -m lto_extractor.validator \
  --md-dir data/extracted_markdown \
  --out reports/markdown_validation.csv
```

The validator is intentionally independent from PDF extraction code. It does not read source PDFs, run OCR, or repair files. It checks whether each Markdown file is self-consistent:

- standard metadata exists: `Source`, `SR pages`, `Detection`, and `CSR/ESG`;
- every `<!-- pdf_page: N -->` marker is inside the declared `SR pages` range;
- every page in the declared `SR pages` range has a corresponding page marker;
- obvious excluded-section headings such as `Corporate Governance`, `Directors' Report`, `Remuneration Report`, `Financial Statements`, `Independent Auditor`, and committee reports are flagged;
- standalone CSR/ESG false-positive committee contexts are flagged.

The CSV schema is:

```text
markdown_file, source_pdf, detected_start, detected_end, method, csr_status, issues, suggested_fix
```

When no issues are found, `issues` and `suggested_fix` are both `none`.
