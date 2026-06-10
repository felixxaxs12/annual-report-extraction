# Legacy PDF-First Extractor

This folder archives the earlier PDF-first extraction path.

It is not part of the current Docling JSON-first workflow. Keep it only as a reference for older experiments that extracted directly from PDF text/OCR and then validated Markdown outputs.

Legacy files:

- `extract_sections.py`: PDF-first Strategic Report extraction with optional OCR support.
- `validator.py`: Markdown-only validation for the old PDF-first output format.
- `requirements.txt`: dependencies needed by the archived PDF-first code.

The active extractor is now:

```bash
PYTHONPATH=src python -m lto_extractor.extract_docling_sections \
  --input-dir data/input_docling_json \
  --output-dir data/extracted_lto_inputs
```
