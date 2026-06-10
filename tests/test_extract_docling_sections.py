from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lto_extractor.extract_docling_sections import extract_docling_document, process_json_file


def text_item(
    text: str,
    page_no: int,
    label: str = "text",
    parent: str = "#/body",
) -> dict:
    return {
        "parent": {"$ref": parent},
        "children": [],
        "content_layer": "body",
        "label": label,
        "text": text,
        "orig": text,
        "prov": [{"page_no": page_no, "bbox": {"l": 1, "t": 2, "r": 3, "b": 4}}],
    }


def table_item(page_no: int, rows: list[list[str]], label: str = "table") -> dict:
    return {
        "parent": {"$ref": "#/body"},
        "children": [],
        "content_layer": "body",
        "label": label,
        "prov": [{"page_no": page_no, "bbox": {"l": 1, "t": 2, "r": 3, "b": 4}}],
        "data": {
            "grid": [[{"text": cell} for cell in row] for row in rows],
            "num_rows": len(rows),
            "num_cols": max(len(row) for row in rows),
        },
    }


def group_item(page_no: int, children: list[str], label: str = "group") -> dict:
    return {
        "parent": {"$ref": "#/body"},
        "children": [{"$ref": child} for child in children],
        "content_layer": "body",
        "label": label,
        "prov": [{"page_no": page_no, "bbox": {"l": 1, "t": 2, "r": 3, "b": 4}}],
    }


class ExtractDoclingSectionsTest(unittest.TestCase):
    def test_extracts_coarse_sections_and_drops_pictures(self) -> None:
        document = {
            "name": "Non-Stopper_Test PLC_20131231",
            "origin": {"filename": "Non-Stopper_Test PLC_20131231.pdf"},
            "pages": {str(page): {"page_no": page} for page in range(1, 8)},
            "texts": [
                text_item("Contents", 1, "section_header"),
                text_item("Strategic Report", 1, "text"),
                text_item("Strategic Report", 2, "section_header"),
                text_item("We invest for long-term resilience.", 2),
                text_item("2013", 2, parent="#/pictures/0"),
                text_item("Corporate Governance", 4, "section_header"),
                text_item("The Board oversees strategy and risk.", 4),
                text_item("Independent Auditor's Report", 6, "section_header"),
                text_item("Audit opinion text should not be retained.", 6),
            ],
            "tables": [
                table_item(
                    1,
                    [["Strategic Report", "2"], ["Corporate Governance", "4"], ["Financial Statements", "6"]],
                    label="document_index",
                ),
                table_item(4, [["Metric", "Weight"], ["LTIP", "50%"]]),
            ],
            "pictures": [
                {
                    "parent": {"$ref": "#/body"},
                    "children": [{"$ref": "#/texts/4"}],
                    "label": "picture",
                    "prov": [{"page_no": 2}],
                }
            ],
            "groups": [],
            "body": {
                "children": [
                    {"$ref": "#/tables/0"},
                    {"$ref": "#/texts/2"},
                    {"$ref": "#/texts/3"},
                    {"$ref": "#/pictures/0"},
                    {"$ref": "#/texts/5"},
                    {"$ref": "#/texts/6"},
                    {"$ref": "#/tables/1"},
                    {"$ref": "#/texts/7"},
                    {"$ref": "#/texts/8"},
                ]
            },
        }

        result = extract_docling_document(document, Path("sample.docling.json"))
        output_text = "\n".join(chunk.text_md for chunk in result.chunks)
        section_types = {chunk.section_type for chunk in result.chunks}

        self.assertEqual({"strategic_report", "governance"}, section_types)
        self.assertIn("We invest for long-term resilience.", output_text)
        self.assertIn("The Board oversees strategy and risk.", output_text)
        self.assertIn("[Table]", output_text)
        self.assertNotIn("Audit opinion text", output_text)
        self.assertNotIn("2013", output_text)
        self.assertEqual(1, result.dropped_picture_count)

    def test_deletes_only_high_confidence_noise_but_keeps_other_fragments(self) -> None:
        document = {
            "name": "Non-Stopper_Test PLC_20131231",
            "origin": {"filename": "Non-Stopper_Test PLC_20131231.pdf"},
            "pages": {str(page): {"page_no": page} for page in range(1, 6)},
            "texts": [
                text_item("Strategic Report", 1, "section_header"),
                text_item("Cost:", 1),
                text_item("Valuation:", 1),
                text_item("2005", 1),
                text_item("Admission to AIM and completion of £8 million placing", 1),
                text_item("September 2010", 1),
                text_item("£million", 1),
                text_item("Sales", 1),
                text_item("EBITA", 1),
                text_item("Net assets", 1),
                text_item("47.3", 2),
                text_item("To maximise shareholder value", 1),
                text_item(
                    "The company has delivered strong profits since investment and continues to expand internationally.",
                    1,
                ),
                text_item("Corporate Governance", 3, "section_header"),
                text_item("The Board oversees strategy, risk, and long-term performance.", 3),
                text_item("Independent Auditor's Report", 5, "section_header"),
            ],
            "tables": [],
            "pictures": [],
            "groups": [],
            "body": {
                "children": [{"$ref": f"#/texts/{index}"} for index in range(16)]
            },
        }

        result = extract_docling_document(document, Path("sample.docling.json"))
        output_text = "\n".join(chunk.text_md for chunk in result.chunks)
        dropped_texts = {chunk.raw_text for chunk in result.dropped_chunks}
        dropped_flags = {flag for chunk in result.dropped_chunks for flag in chunk.quality_flags}

        self.assertNotIn("Cost:", output_text)
        self.assertNotIn("Valuation:", output_text)
        self.assertIn("2005", output_text)
        self.assertIn("Admission to AIM", output_text)
        self.assertNotIn("47.3", output_text)
        self.assertIn("September 2010", output_text)
        self.assertNotIn("£million", output_text)
        self.assertIn("Sales", output_text)
        self.assertIn("To maximise shareholder value", output_text)
        self.assertIn("continues to expand internationally", output_text)
        self.assertIn("The Board oversees strategy", output_text)
        self.assertIn("Cost:", dropped_texts)
        self.assertIn("47.3", dropped_texts)
        self.assertIn("short_label", dropped_flags)

    def test_uses_contents_boundaries_and_ignores_navigation_headers(self) -> None:
        document = {
            "name": "Non-Stopper_Test PLC_20131231",
            "origin": {"filename": "Non-Stopper_Test PLC_20131231.pdf"},
            "pages": {str(page): {"page_no": page} for page in range(1, 10)},
            "texts": [
                text_item("Contents", 1, "section_header"),
                text_item("2 Strategic Report", 1, "list_item"),
                text_item("5 Corporate Governance", 1, "list_item"),
                text_item("7 Financial Statements", 1, "list_item"),
                text_item("Governance", 2, "page_header"),
                text_item("Strategic Report", 2, "section_header"),
                text_item("The company invests for long-term growth.", 2),
                text_item("Corporate Governance", 5, "section_header"),
                text_item("The Board reviews strategy and risk.", 5),
                text_item("Financial Statements", 7, "section_header"),
                text_item("Financial statement text should not be retained.", 7),
                text_item("Corporate governance statement", 8, "section_header"),
                text_item("Late auditor-tail governance wording should not be retained.", 8),
            ],
            "tables": [],
            "pictures": [],
            "groups": [],
            "body": {
                "children": [{"$ref": f"#/texts/{index}"} for index in range(13)]
            },
        }

        result = extract_docling_document(document, Path("sample.docling.json"))
        output_text = "\n".join(chunk.text_md for chunk in result.chunks)
        pages_by_section = {
            section.section_type: (section.start_page, section.end_page)
            for section in result.windows
        }

        self.assertEqual((2, 4), pages_by_section["strategic_report"])
        self.assertEqual((5, 6), pages_by_section["governance"])
        self.assertIn("long-term growth", output_text)
        self.assertIn("The Board reviews strategy and risk.", output_text)
        self.assertNotIn("Financial statement text", output_text)
        self.assertNotIn("Late auditor-tail governance", output_text)

    def test_keeps_metric_dense_highlight_blocks_but_deletes_footer(self) -> None:
        document = {
            "name": "Non-Stopper_Test PLC_20131231",
            "origin": {"filename": "Non-Stopper_Test PLC_20131231.pdf"},
            "pages": {str(page): {"page_no": page} for page in range(1, 5)},
            "texts": [
                text_item("Strategic Report", 1, "section_header"),
                text_item("Our highlights", 1, "section_header"),
                text_item("Key Performance Indicators", 1),
                text_item("Revenue US$100m", 1),
                text_item("Operating cash flow US$20m", 1),
                text_item("Basic EPS cents 47 3", 1),
                text_item("Debt maturity 93% due after more than one year", 1),
                text_item("Production 47,112 boepd", 1),
                text_item("TRIR 2 52 LTIF 1 05", 1),
                text_item("02 Test PLC Annual Report and Accounts 2013", 1),
                text_item("We invest to strengthen long-term resilience across the business.", 2),
                text_item("Financial Statements", 3, "section_header"),
            ],
            "tables": [],
            "pictures": [],
            "groups": [],
            "body": {
                "children": [{"$ref": f"#/texts/{index}"} for index in range(12)]
            },
        }

        result = extract_docling_document(document, Path("sample.docling.json"))
        output_text = "\n".join(chunk.text_md for chunk in result.chunks)
        dropped_flags = {flag for chunk in result.dropped_chunks for flag in chunk.quality_flags}

        self.assertIn("Key Performance Indicators", output_text)
        self.assertIn("Revenue US$100m", output_text)
        self.assertNotIn("Annual Report and Accounts", output_text)
        self.assertIn("long-term resilience", output_text)
        self.assertIn("annual_report_page_footer", dropped_flags)

    def test_preserves_docling_group_as_one_chunk(self) -> None:
        document = {
            "name": "Non-Stopper_Test PLC_20131231",
            "origin": {"filename": "Non-Stopper_Test PLC_20131231.pdf"},
            "pages": {str(page): {"page_no": page} for page in range(1, 5)},
            "texts": [
                text_item("Strategic Report", 1, "section_header"),
                text_item("2011", 1, parent="#/groups/0"),
                text_item("Entered Kurdistan", 1, parent="#/groups/0"),
                text_item("First oil production", 1, parent="#/groups/0"),
                text_item("Financial Statements", 3, "section_header"),
            ],
            "tables": [],
            "pictures": [],
            "groups": [group_item(1, ["#/texts/1", "#/texts/2", "#/texts/3"], label="timeline")],
            "body": {
                "children": [
                    {"$ref": "#/texts/0"},
                    {"$ref": "#/groups/0"},
                    {"$ref": "#/texts/4"},
                ]
            },
        }

        result = extract_docling_document(document, Path("sample.docling.json"))
        group_chunks = [chunk for chunk in result.chunks if chunk.content_type == "group"]

        self.assertEqual(1, len(group_chunks))
        self.assertEqual("2011\nEntered Kurdistan\nFirst oil production", group_chunks[0].text_md)

    def test_process_writes_one_markdown_and_one_json(self) -> None:
        document = {
            "name": "Non-Stopper_Test PLC_20131231",
            "origin": {"filename": "Non-Stopper_Test PLC_20131231.pdf"},
            "pages": {str(page): {"page_no": page} for page in range(1, 5)},
            "texts": [
                text_item("Strategic Report", 1, "section_header"),
                text_item("We invest for long-term resilience.", 1),
                text_item("Financial Statements", 3, "section_header"),
            ],
            "tables": [],
            "pictures": [],
            "groups": [],
            "body": {
                "children": [{"$ref": f"#/texts/{index}"} for index in range(3)]
            },
        }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            json_path = tmp_path / "sample.docling.json"
            output_dir = tmp_path / "out"
            json_path.write_text(json.dumps(document), encoding="utf-8")

            row = process_json_file(json_path, output_dir)
            output_names = {path.name for path in output_dir.iterdir()}
            canonical_json = json.loads((output_dir / "sample_lto_document.json").read_text())

        self.assertEqual({"sample_lto_input.md", "sample_lto_document.json"}, output_names)
        self.assertEqual("sample_lto_document.json", row["json_file"])
        self.assertEqual(
            {"schema_version", "source_pdf", "sections"},
            set(canonical_json),
        )
        self.assertEqual("docling_lto_document_v2", canonical_json["schema_version"])
        self.assertEqual(
            {"section_type", "start_page", "end_page", "chunks"},
            set(canonical_json["sections"][0]),
        )
        self.assertEqual(
            {"chunk_id", "status", "content_type", "page_no", "heading_path", "text_md"},
            set(canonical_json["sections"][0]["chunks"][0]),
        )
        self.assertEqual("kept", canonical_json["sections"][0]["chunks"][0]["status"])
        self.assertEqual(
            "strategic_report_00001",
            canonical_json["sections"][0]["chunks"][0]["chunk_id"],
        )


if __name__ == "__main__":
    unittest.main()
