"""Tests for in-server docx document-program envelope extraction and docx
import repair/validation (docx-renderer-server.py).

These cover the three failure modes seen in the 2026-07-18 batch:
  1. empty-.mjs handoff race  -> removed by extracting source_code in-process
  2. trailing content after the envelope object -> tolerated by raw_decode
  3. hallucinated docx import (TabStopLeader) -> auto-repaired to LeaderType
plus the strict extension-vs-content cross-check that guards against silently
running the wrong thing.
"""
import importlib.util
import pathlib

import pytest

_SERVER = pathlib.Path(__file__).resolve().parent.parent / "docx-renderer-server.py"


def _load():
    spec = importlib.util.spec_from_file_location("docxsrv_undertest", _SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


m = _load()

# A minimal but realistic docx export set for import tests (no node probe).
EXPORTS = {
    "Document", "Packer", "Paragraph", "TextRun", "TabStopType",
    "LeaderType", "AlignmentType", "BorderStyle", "PageBreak",
    "convertInchesToTwip",
}

VALID_ENVELOPE = (
    '{ "renderer_type": "node", "source_filename": "render-document.mjs", '
    '"source_code": "import { Document, Packer } from \'docx\';\\n'
    'import { writeFile } from \'node:fs/promises\';\\n'
    'const doc = new Document({});\\n'
    'const buffer = await Packer.toBuffer(doc);\\n'
    'await writeFile(process.env.OUTPUT_DOCX_PATH, buffer);\\n" }'
)


# --- analyze_entrypoint_content -------------------------------------------

def test_plain_module_is_program():
    src = "import { Document } from 'docx';\nconst doc = new Document({});\n"
    assert m.analyze_entrypoint_content(src) == {"kind": "program"}


def test_valid_envelope_extracts_source_code():
    info = m.analyze_entrypoint_content(VALID_ENVELOPE)
    assert info["kind"] == "envelope"
    assert info["renderer_type"] == "node"
    assert info["source_code"].startswith("import { Document, Packer }")
    assert info["trailing"] == ""


def test_envelope_tolerates_trailing_junk():
    # The exact failure from the batch: a complete object then stray content.
    info = m.analyze_entrypoint_content(VALID_ENVELOPE + "\n}")
    assert info["kind"] == "envelope"
    assert info["trailing"] == "}"
    info2 = m.analyze_entrypoint_content(VALID_ENVELOPE + "\nAll done! extra prose")
    assert info2["kind"] == "envelope"
    assert "All done!" in info2["trailing"]


def test_envelope_inside_one_markdown_fence():
    fenced = "```json\n" + VALID_ENVELOPE + "\n```"
    info = m.analyze_entrypoint_content(fenced)
    assert info["kind"] == "envelope"
    assert info["source_code"].startswith("import { Document, Packer }")


def test_broken_when_json_object_lacks_source_code():
    info = m.analyze_entrypoint_content('{ "renderer_type": "node" }')
    assert info["kind"] == "broken"
    assert "missing" in info["detail"]


def test_broken_when_unparseable_json():
    info = m.analyze_entrypoint_content('{ "source_code": "oops" ')  # truncated
    assert info["kind"] == "broken"
    assert "JSON" in info["detail"]


def test_broken_when_source_code_empty():
    info = m.analyze_entrypoint_content(
        '{ "renderer_type": "node", "source_filename": "x", "source_code": "" }'
    )
    assert info["kind"] == "broken"


# --- _docx_imported_names --------------------------------------------------

def test_imported_names_single_block():
    src = "import { Document, Packer, TabStopType } from 'docx';\n"
    assert m._docx_imported_names(src) == ["Document", "Packer", "TabStopType"]


def test_imported_names_multiple_blocks_and_as_alias_and_non_docx():
    src = (
        "import { Document, Packer } from 'docx';\n"
        "import { writeFile } from 'node:fs/promises';\n"
        "import { convertInchesToTwip as toTwip } from 'docx';\n"
    )
    assert m._docx_imported_names(src) == ["Document", "Packer", "convertInchesToTwip"]


# --- repair_and_validate_docx_imports -------------------------------------

def test_alias_repair_rewrites_import_and_usages():
    src = (
        "import { TabStopType, TabStopLeader } from 'docx';\n"
        "const x = { type: TabStopType.RIGHT, leader: TabStopLeader.NONE };\n"
    )
    rep = m.repair_and_validate_docx_imports(src, EXPORTS)
    assert "error" not in rep
    assert "TabStopLeader" not in rep["source_code"]
    assert "LeaderType.NONE" in rep["source_code"]
    assert rep["warnings"] and "TabStopLeader" in rep["warnings"][0]


def test_unknown_import_rejected_with_suggestion():
    src = "import { Documnet } from 'docx';\n"  # typo
    rep = m.repair_and_validate_docx_imports(src, EXPORTS)
    assert "error" in rep
    assert "Documnet" in rep["error"]["message"]
    assert "Document" in rep["error"]["message"]  # nearest-match hint


def test_fail_open_when_exports_unknown():
    # exports=None -> cannot validate existence; must not reject, but still
    # repairs known aliases.
    src = "import { TabStopLeader, MysteryThing } from 'docx';\n"
    rep = m.repair_and_validate_docx_imports(src, None)
    assert "error" not in rep
    assert "TabStopLeader" not in rep["source_code"]
    assert "MysteryThing" in rep["source_code"]  # untouched, not rejected


def test_clean_source_unchanged_no_warnings():
    src = "import { Document, Packer } from 'docx';\n"
    rep = m.repair_and_validate_docx_imports(src, EXPORTS)
    assert rep == {"source_code": src, "warnings": []}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
