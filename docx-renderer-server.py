#!/usr/bin/env python3
"""
DOCX Renderer MCP Server
========================

A controlled DOCX execution harness exposed via MCP.

This server does NOT write resumes, choose formatting, convert markdown, or
infer document design.  Those tasks happen before this server is called.

The server's job is to:
1. Accept an AI-generated renderer script (Node.js / ES module).
2. Stage it in a controlled temporary workspace.
3. Execute it with a known runtime.
4. Require it to write a .docx file to a provided output path.
5. Validate the output.
6. Return a structured result to the caller.
"""

from __future__ import annotations

import argparse
import contextlib
from datetime import datetime
import difflib
from dotenv import load_dotenv
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time
import traceback
import yaml
import zipfile
from pydantic import Field, AnyHttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Any, Dict, List, Optional, Annotated, Tuple

try:
    PROJECT_ROOT = Path(__file__).resolve().parent
except NameError:
    PROJECT_ROOT = Path.cwd()

load_dotenv(PROJECT_ROOT / ".env")

from mcp.server.fastmcp import FastMCP

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import HTMLResponse, RedirectResponse
    import uvicorn
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALLOWED_PLACEHOLDERS = ("document_type", "company", "role", "timestamp", "date", "slug")

ALLOWED_DOCUMENT_TYPES = ("resume", "cover_letter", "generic")

ALLOWED_RENDERER_TYPES = ("node", "python")
FUTURE_RENDERER_TYPES: tuple[str, ...] = ()

ALLOWED_FILE_EXTENSIONS = (".mjs", ".js", ".json", ".txt", ".md", ".py")

REQUIRED_ENTRYPOINTS: dict[str, str] = {
    "node": "render-document.mjs",
    "python": "render_document.py",
}

REJECTED_FILENAMES = frozenset({
    "package.json", "package-lock.json", ".npmrc",
})

BLOCKED_NODE_MODULES = frozenset({
    "child_process", "node:child_process",
    "worker_threads", "node:worker_threads",
    "cluster", "node:cluster",
    "vm", "node:vm",
    "http", "node:http",
    "https", "node:https",
    "net", "node:net",
    "tls", "node:tls",
    "dns", "node:dns",
    "dgram", "node:dgram",
    "os", "node:os",
})

BLOCKED_PYTHON_MODULES = frozenset({
    "subprocess",
    "socket",
    "requests",
    "urllib",
    "http",
    "http.client",
    "shutil",
    "multiprocessing",
    "ctypes",
})

# Node/JS patterns rejected before execution. Two shapes here, and the
# difference between them is load-bearing.
#
# Call patterns use `(?<![.\w])` instead of `\b` so they match a bare global
# call but not a method call on an object. `\b` sits in the gap between `.` and
# an identifier, so `\bexec\s*\(` matched `regex.exec(text)` -- the idiomatic
# way to walk repeated matches, and precisely what a markdown-to-docx program
# writes to parse `**bold**`. Excluding the method form costs nothing:
# `exec`/`execFile`/`spawn`/`fork` reach a program only through
# `child_process`, which BLOCKED_NODE_MODULES rejects; `require()` is rejected
# outright; and dynamic `import()` is blocked. A program that clears the import
# checks has no such symbol to call, so a bare `exec(` would be a ReferenceError
# rather than a breach. `eval` and `fetch` genuinely are globals, and their bare
# form is still caught -- as is the same global reached through a global-object
# qualifier (`globalThis.fetch(`, `window.eval(`), which the method-call
# exclusion would otherwise wave through. Other qualifiers (`client.fetch(`)
# are ordinary methods and stay allowed.
#
# Construction patterns require `new ...(` rather than a bare identifier, so
# they match a *use* and not a *mention*. `\bWebSocket\b` matched the word
# anywhere, including inside string literals -- and in these programs the string
# literals are the resume text. A resume that said "WebSocket" was rejected as a
# policy violation for describing its author's own experience.
#
# The identical `exec` pattern in DISALLOWED_PYTHON_PATTERNS is deliberately NOT
# relaxed: `exec` is a Python builtin, so there the bare form is real.
DISALLOWED_CODE_PATTERNS: list[tuple[str, str]] = [
    (r"(?<![.\w])exec\s*\(", "exec( call"),
    (r"(?<![.\w])execFile\s*\(", "execFile( call"),
    (r"(?<![.\w])spawn\s*\(", "spawn( call"),
    (r"(?<![.\w])fork\s*\(", "fork( call"),
    (r"(?:(?<![.\w])|\b(?:globalThis|global|window|self)\s*\.\s*)eval\s*\(", "eval( call"),
    (r"\bnew\s+Function\s*\(", "new Function( call"),
    (r"(?<![.\w])import\s*\(", "dynamic import( call"),
    (r"(?:(?<![.\w])|\b(?:globalThis|global|window|self)\s*\.\s*)fetch\s*\(", "fetch( call"),
    (r"\bnew\s+XMLHttpRequest\s*\(", "XMLHttpRequest construction"),
    (r"\bnew\s+WebSocket\s*\(", "WebSocket construction"),
    (r"\bprocess\.exit\b", "process.exit"),
]

DISALLOWED_PYTHON_PATTERNS: list[tuple[str, str]] = [
    (r"\bos\.system\s*\(", "os.system( call"),
    (r"\bos\.popen\s*\(", "os.popen( call"),
    (r"\bos\.exec\w*\s*\(", "os.exec*( call"),
    (r"\bos\.spawn\w*\s*\(", "os.spawn*( call"),
    (r"\beval\s*\(", "eval( call"),
    (r"\bexec\s*\(", "exec( call"),
    (r"\b__import__\s*\(", "__import__( call"),
    (r"\bshutil\.rmtree\s*\(", "shutil.rmtree( call"),
]

SENSITIVE_ENV_VARS = frozenset({
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "NPM_TOKEN",
})

ALLOWED_PROCESS_ENV_VARS = frozenset({
    "OUTPUT_DOCX_PATH", "DOCX_RENDER_WORKSPACE", "DOCX_RENDER_METADATA_PATH",
})

ALLOWED_PYTHON_ENV_VARS = frozenset({
    "OUTPUT_DOCX_PATH", "DOCX_RENDER_WORKSPACE", "DOCX_RENDER_METADATA_PATH",
})

# Known hallucinated docx (npm) symbols mapped to the real export that is a
# safe drop-in replacement. Applied as a whole-identifier rewrite on the node
# program (import statement AND usages) before the policy scan and execution.
# Deliberately tiny and curated: only add a 1:1 rename where the wrong and
# right symbols share the same shape -- e.g. TabStopLeader.NONE and
# LeaderType.NONE are both enum members with identical values. Anything not in
# this map that does not exist in docx is rejected, never guessed.
DOCX_IMPORT_ALIASES: Dict[str, str] = {
    "TabStopLeader": "LeaderType",
}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logger() -> logging.Logger:
    logger = logging.getLogger("docx-renderer-server")
    if logger.hasHandlers():
        logger.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger


logger: logging.Logger = setup_logger()


# ---------------------------------------------------------------------------
# YAML config loader
# ---------------------------------------------------------------------------

def load_yaml_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        logger.info("Config file '%s' not found; using built-in defaults", path)
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Failed to load config '{path}': {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"Config '{path}' must be a mapping at the top level")
    return data


# ---------------------------------------------------------------------------
# Settings  (spec §6)
# ---------------------------------------------------------------------------

class ServerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MCP_", extra="allow")

    # Transport
    transport: str = Field(default="stdio")
    host: str = Field(default="localhost")
    port: int = Field(default=8000)
    server_url: AnyHttpUrl = Field(default="http://localhost:8000")
    ssl_cert_path: Optional[str] = Field(default=None)
    ssl_key_path: Optional[str] = Field(default=None)

    # Identity
    server_name: str = Field(default="docx-renderer-server")
    description: str = Field(
        default="DOCX renderer service for AI-generated renderer scripts",
    )
    config_path: str = Field(default="docx-renderer-server.yaml")

    # Renderer settings — match spec §6
    output_root: str = Field(default="generated-docx")
    # Node runtime
    node_runtime_root: str = Field(default="node-renderer-runtime")
    # Python runtime
    python_runtime_root: str = Field(default="python-renderer-runtime")
    python_executable: str = Field(default=sys.executable)
    workspace_root_name: str = Field(default="workspaces")
    node_executable: str = Field(default="node")
    default_timeout_seconds: int = Field(default=30)
    max_timeout_seconds: int = Field(default=120)
    keep_failed_workspaces: bool = Field(default=True)
    keep_successful_workspaces: bool = Field(default=False)
    max_file_count: int = Field(default=20)
    max_total_file_bytes: int = Field(default=2_000_000)
    max_single_file_bytes: int = Field(default=1_000_000)
    allowed_document_types: tuple[str, ...] = Field(default=ALLOWED_DOCUMENT_TYPES)
    allowed_renderer_types: tuple[str, ...] = Field(default=ALLOWED_RENDERER_TYPES)
    # Server-side file transport: directory aliases (name -> dir) the
    # server may read renderer source files from via a file entry's
    # 'source_path' ('alias/relative/path'). Configured in the YAML under
    # 'directory_aliases:'. Empty disables the feature (fail closed).
    directory_aliases: dict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Structured error  (spec §9.3)
# ---------------------------------------------------------------------------

def _render_error(code: str, message: str, detail: str = "") -> Dict[str, str]:
    d: Dict[str, str] = {"code": code, "message": message}
    if detail:
        d["detail"] = detail
    return d


# ---------------------------------------------------------------------------
# Result builders  (spec §9, §14.10)
# ---------------------------------------------------------------------------

def _fail_result(
    *,
    status: str,
    document_type: str = "",
    renderer_type: str = "",
    render_id: str = "",
    errors: list[Dict[str, str]] | None = None,
    warnings: list[str] | None = None,
    execution: Dict[str, Any] | None = None,
    workspace_path: str | None = None,
    workspace_kept: bool = False,
    stdout: str = "",
    stderr: str = "",
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "ok": False,
        "status": status,
        "document_type": document_type,
        "renderer_type": renderer_type,
        "render_id": render_id,
        "output": None,
        "workspace": {"path": workspace_path, "kept": workspace_kept},
        "execution": execution,
        "validation": None,
        "warnings": warnings or [],
        "errors": errors or [],
    }
    if stdout:
        result["stdout"] = stdout
    if stderr:
        result["stderr"] = stderr
    return result


def _success_result(
    *,
    document_type: str,
    renderer_type: str,
    render_id: str,
    filename: str,
    output_path: str,
    relative_path: str,
    size_bytes: int,
    execution: Dict[str, Any],
    validation: Dict[str, Any],
    workspace_path: str | None = None,
    workspace_kept: bool = False,
    warnings: list[str] | None = None,
) -> Dict[str, Any]:
    return {
        "ok": True,
        "status": "success",
        "document_type": document_type,
        "renderer_type": renderer_type,
        "render_id": render_id,
        # The absolute path to the created .docx, promoted to the top level as
        # well as nested under "output".
        #
        # This is deliberate redundancy. A caller that cannot find the output
        # fails *after* a successful render, so the failure looks like a
        # renderer fault and consumes the caller's retry budget -- including the
        # workflow contract's mandatory low->middle escalation -- on a defect
        # that is purely a field-name mismatch. The nested block stays for
        # existing callers; this is the one field a caller should have to know.
        "output_path": output_path,
        "output": {
            "filename": filename,
            "path": output_path,
            "relative_path": relative_path,
            "size_bytes": size_bytes,
        },
        "workspace": {"path": workspace_path, "kept": workspace_kept},
        "execution": execution,
        "validation": validation,
        "warnings": warnings or [],
        "errors": [],
    }


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def slugify(value: Any) -> str:
    """Lowercase ASCII slug.  'Senior Backend Engineer' -> 'senior-backend-engineer'."""
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def build_output_filename(
    pattern: str,
    values: Dict[str, str],
    document_type: str,
    now: datetime | None = None,
) -> Tuple[str, list[str]]:
    """Build a sanitized .docx filename from *pattern* + *values*.  (spec §14.2)"""
    warnings: list[str] = []
    now = now or datetime.now()

    replacements = {
        "document_type": slugify(document_type),
        "company": slugify(values.get("company", "")),
        "role": slugify(values.get("role", "")),
        "timestamp": now.strftime("%Y%m%d-%H%M%S"),
        "date": now.strftime("%Y%m%d"),
        "slug": slugify(values.get("slug", "")),
    }

    for found in re.findall(r"\{([^}]*)\}", pattern):
        if found not in ALLOWED_PLACEHOLDERS:
            warnings.append(f"Unknown placeholder '{{{found}}}' ignored")

    def _sub(m: re.Match[str]) -> str:
        return replacements.get(m.group(1), "")

    name = re.sub(r"\{([^}]*)\}", _sub, pattern)

    name = re.sub(r'[<>:"/\\|?*]+', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = re.sub(r"[^a-zA-Z0-9._-]+", "-", name)
    name = re.sub(r"-{2,}", "-", name).strip("-_")

    if len(name) > 200:
        name = name[:200]
        warnings.append("Filename truncated to 200 characters")

    if not name:
        name = slugify(document_type) or "document"
        warnings.append("Filename pattern resolved to empty; fell back to document_type")

    if not name.lower().endswith(".docx"):
        name = f"{name}.docx"

    return name, warnings


def is_safe_relpath(path_str: str) -> bool:
    """Reject absolute paths, drive-qualified paths, and ``..`` traversal."""
    if not path_str or not path_str.strip():
        return False
    if os.path.splitdrive(path_str)[0]:
        return False
    normalized = path_str.replace("\\", "/")
    if normalized.startswith("/"):
        return False
    if Path(path_str).is_absolute():
        return False
    parts = [seg for seg in normalized.split("/") if seg not in ("", ".")]
    return ".." not in parts


def generate_render_id(now: datetime | None = None) -> str:
    now = now or datetime.now()
    return f"{now.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"


def make_output_relative_path(output_path: Path, output_root: Path) -> str:
    """
    Return a stable user-facing relative path like:
        generated-docx/file.docx

    This is relative to output_root.parent, not process cwd.
    """
    output_path = Path(output_path).resolve()
    output_root = Path(output_root).resolve()

    try:
        return output_path.relative_to(output_root.parent).as_posix()
    except ValueError:
        return output_path.name


# ---------------------------------------------------------------------------
# File-entry validation  (spec §5.8)
# ---------------------------------------------------------------------------

def parse_directory_aliases(raw: Any) -> Dict[str, Path]:
    """Parse the config's directory_aliases mapping (name -> dir) into resolved
    Paths. Alias names are simple identifiers (no path separators) and each
    names exactly one directory."""
    aliases: Dict[str, Path] = {}
    if not isinstance(raw, dict):
        return aliases
    for name, directory in raw.items():
        name = str(name).strip()
        if not name or "/" in name or "\\" in name:
            continue
        if isinstance(directory, (list, tuple)):
            # The superseded file_families shape mapped a name to a list of
            # directories. An alias names exactly one, so an old-format entry is
            # config to fix rather than something to guess at.
            logger.warning(
                "Ignoring directory alias %r: expected a single directory path, "
                "got a list. Update the config to 'name: C:\\path'.", name,
            )
            continue
        text = str(directory).strip() if directory is not None else ""
        if text:
            aliases[name] = Path(text).resolve()
    return aliases


def resolve_alias_reference(
    ref: str, *, aliases: Dict[str, Path], max_bytes: int,
) -> Dict[str, Any]:
    """Resolve an 'alias/relative/path' reference to a file's text, safely.

    Returns {"path": Path, "text": str} on success, else {"error": <detail>}.
    Same rules as the AI server: disabled unless aliases configured; known
    alias + relative path; no '..'; resolved path stays inside the alias's
    directory; regular UTF-8 file within max_bytes.
    """
    if not aliases:
        return {"error": "no directory_aliases are configured on this server; "
                         "server-side file transport is disabled."}
    if not isinstance(ref, str) or not ref.strip():
        return {"error": f"file reference is not a usable string: {ref!r}."}
    normalized = ref.strip().replace("\\", "/")
    if "/" not in normalized:
        return {"error": f"file reference {ref!r} must be 'alias/relative/path'."}
    alias_name, rel = normalized.split("/", 1)
    alias_name, rel = alias_name.strip(), rel.strip("/")
    if not alias_name or not rel:
        return {"error": f"file reference {ref!r} must name a directory alias and "
                         "a relative path."}
    if alias_name not in aliases:
        return {"error": f"unknown directory alias {alias_name!r}; configured: {sorted(aliases)}."}
    if ".." in rel.split("/"):
        return {"error": f"file reference {ref!r} may not contain '..' segments."}
    root = aliases[alias_name]
    candidate = (root / rel).resolve()
    root_cmp = os.path.normcase(str(root))
    cand_cmp = os.path.normcase(str(candidate))
    if not (cand_cmp == root_cmp or cand_cmp.startswith(root_cmp + os.sep)):
        return {"error": f"file reference {ref!r} resolves outside the directory "
                         f"for alias {alias_name!r}."}
    if not candidate.is_file():
        return {"error": f"file reference {ref!r} not found in the directory for "
                         f"alias {alias_name!r}."}
    if candidate.stat().st_size > max_bytes:
        return {"error": f"{ref!r} is {candidate.stat().st_size:,} bytes, "
                         f"exceeds per-file limit of {max_bytes:,}."}
    try:
        return {"path": candidate, "text": candidate.read_text(encoding="utf-8")}
    except (OSError, UnicodeDecodeError) as exc:
        return {"error": f"{ref!r} could not be read as UTF-8: {exc}"}


def materialize_file_entries(
    files: list[Dict[str, str]],
    settings: ServerSettings,
) -> tuple[list[Dict[str, str]] | None, Dict[str, str] | None, Dict[str, str]]:
    """Server-side file transport: replace {path, source_path} entries with
    {path, content} by reading each source file from disk.

    'source_path' is an 'alias/relative/path' reference resolved against the
    directory configured for that alias in directory_aliases (fail closed).

    Returns (materialized_files, error, file_sources). On error the first
    element is None. file_sources maps workspace path -> resolved source path
    for every disk-sourced entry (for the audit trail).
    """
    aliases = parse_directory_aliases(settings.directory_aliases)
    materialized: list[Dict[str, str]] = []
    file_sources: Dict[str, str] = {}

    for entry in files:
        if not isinstance(entry, dict):
            return None, _render_error(
                "INVALID_FILE_PATH", "File entry is not an object"), {}
        source_path = entry.get("source_path")
        content = entry.get("content")
        if source_path is None:
            materialized.append(entry)
            continue
        if content is not None:
            return None, _render_error(
                "INVALID_FILE_PATH",
                f"File entry {entry.get('path')!r} supplies BOTH 'content' and "
                "'source_path' (ambiguous source of truth)",
            ), {}
        out = resolve_alias_reference(
            str(source_path), aliases=aliases, max_bytes=settings.max_single_file_bytes
        )
        if "error" in out:
            return None, _render_error("FILE_SOURCE_NOT_ALLOWED", out["error"]), {}
        materialized.append({"path": entry.get("path", ""), "content": out["text"]})
        file_sources[entry.get("path", "")] = str(out["path"])

    return materialized, None, file_sources


def validate_file_entry(
    entry: Dict[str, str],
    settings: ServerSettings,
) -> Dict[str, str] | None:
    """Return a structured error dict, or *None* if the entry is valid."""
    path = entry.get("path", "")
    content = entry.get("content")

    if content is None:
        return _render_error(
            "INVALID_FILE_PATH",
            f"File entry missing 'content': {path!r}",
        )
    if not is_safe_relpath(path):
        return _render_error("INVALID_FILE_PATH", f"Unsafe file path rejected: {path!r}")

    basename = path.replace("\\", "/").split("/")[-1].lower()

    if basename in {n.lower() for n in REJECTED_FILENAMES}:
        return _render_error(
            "INVALID_FILE_PATH",
            f"File '{basename}' is not allowed in v1",
        )

    if "node_modules" in path.replace("\\", "/").split("/"):
        return _render_error(
            "INVALID_FILE_PATH",
            f"node_modules not allowed in file paths: {path!r}",
        )

    ext = os.path.splitext(basename)[1]
    if not ext or ext not in ALLOWED_FILE_EXTENSIONS:
        return _render_error(
            "INVALID_FILE_PATH",
            f"File extension '{ext or '(none)'}' not allowed. "
            f"Allowed: {', '.join(ALLOWED_FILE_EXTENSIONS)}",
        )

    content_bytes = len(content.encode("utf-8"))
    if content_bytes > settings.max_single_file_bytes:
        return _render_error(
            "FILE_TOO_LARGE",
            f"File '{path}' is {content_bytes:,} bytes, "
            f"exceeds limit of {settings.max_single_file_bytes:,}",
        )

    return None


# ---------------------------------------------------------------------------
# Static script policy  (spec §10, §14.5)
# ---------------------------------------------------------------------------

def validate_node_script_policy(files: list[Dict[str, str]]) -> list[Dict[str, str]]:
    """Inspect .mjs/.js files for disallowed patterns before execution."""
    errors: list[Dict[str, str]] = []

    for entry in files:
        path = entry.get("path", "")
        ext = os.path.splitext(path)[1].lower()
        if ext not in (".mjs", ".js"):
            continue
        content = entry.get("content", "")

        # --- blocked module imports (static ES imports) ---
        for m in re.finditer(
            r"""import\s+.*?from\s+['"]([^'"]+)['"]"""
            r"""|import\s+['"]([^'"]+)['"]""",
            content,
        ):
            module = m.group(1) or m.group(2)
            if module in BLOCKED_NODE_MODULES:
                errors.append(_render_error(
                    "SCRIPT_REJECTED_BY_POLICY",
                    f"Blocked import of '{module}' in {path}",
                    detail=m.group(0).strip(),
                ))
            elif re.match(r"^https?://", module):
                errors.append(_render_error(
                    "SCRIPT_REJECTED_BY_POLICY",
                    f"Remote module import from URL is not allowed: '{module}' in {path}",
                    detail=m.group(0).strip(),
                ))

        # --- require() entirely rejected in v1 ---
        for m in re.finditer(r"""\brequire\s*\(\s*['"][^'"]*['"]\s*\)""", content):
            errors.append(_render_error(
                "SCRIPT_REJECTED_BY_POLICY",
                f"CommonJS require() not allowed in v1: {path}",
                detail=m.group(0).strip(),
            ))

        # --- dangerous code patterns ---
        for pattern_re, label in DISALLOWED_CODE_PATTERNS:
            for m in re.finditer(pattern_re, content):
                errors.append(_render_error(
                    "SCRIPT_REJECTED_BY_POLICY",
                    f"Disallowed pattern '{label}' found in {path}",
                    detail=m.group(0).strip(),
                ))

        # --- process.env access (only allowed vars) ---
        allowed_alt = "|".join(ALLOWED_PROCESS_ENV_VARS)
        for m in re.finditer(
            rf"\bprocess\.env\b(?!\.(?:{allowed_alt})\b)", content,
        ):
            errors.append(_render_error(
                "SCRIPT_REJECTED_BY_POLICY",
                f"Disallowed process.env access in {path}; "
                "only process.env.OUTPUT_DOCX_PATH is permitted",
            ))

    return errors


def validate_python_script_policy(files: list[Dict[str, str]]) -> list[Dict[str, str]]:
    """Inspect .py files for disallowed patterns before execution."""
    errors: list[Dict[str, str]] = []

    for entry in files:
        path = entry.get("path", "")
        ext = os.path.splitext(path)[1].lower()
        if ext != ".py":
            continue
        content = entry.get("content", "")

        for m in re.finditer(
            r"^\s*(?:import|from)\s+([a-zA-Z_][\w.]*)",
            content,
            re.MULTILINE,
        ):
            module = m.group(1)
            parts = module.split(".")
            for i in range(len(parts)):
                prefix = ".".join(parts[: i + 1])
                if prefix in BLOCKED_PYTHON_MODULES:
                    errors.append(_render_error(
                        "SCRIPT_REJECTED_BY_POLICY",
                        f"Blocked import of '{prefix}' in {path}",
                        detail=m.group(0).strip(),
                    ))
                    break

        for pattern_re, label in DISALLOWED_PYTHON_PATTERNS:
            for m in re.finditer(pattern_re, content):
                errors.append(_render_error(
                    "SCRIPT_REJECTED_BY_POLICY",
                    f"Disallowed pattern '{label}' found in {path}",
                    detail=m.group(0).strip(),
                ))

        for m in re.finditer(
            r"""\bos\.environ\s*[\[.]""",
            content,
        ):
            after = content[m.end():]
            var_match = re.match(r"""['"(\s]*([A-Z_][A-Z0-9_]*)""", after)
            if var_match:
                var = var_match.group(1)
                if var not in ALLOWED_PYTHON_ENV_VARS:
                    errors.append(_render_error(
                        "SCRIPT_REJECTED_BY_POLICY",
                        f"Disallowed os.environ access for '{var}' in {path}; "
                        "only OUTPUT_DOCX_PATH is permitted",
                    ))
            else:
                errors.append(_render_error(
                    "SCRIPT_REJECTED_BY_POLICY",
                    f"Broad os.environ access in {path}; "
                    "only OUTPUT_DOCX_PATH is permitted",
                ))

        for m in re.finditer(
            r"""\bos\.getenv\s*\(\s*['"]([^'"]+)['"]""",
            content,
        ):
            var = m.group(1)
            if var not in ALLOWED_PYTHON_ENV_VARS:
                errors.append(_render_error(
                    "SCRIPT_REJECTED_BY_POLICY",
                    f"Disallowed os.getenv('{var}') in {path}; "
                    "only OUTPUT_DOCX_PATH is permitted",
                ))

        for m in re.finditer(
            r"\bos\.environ\b(?!\s*[\[.])",
            content,
        ):
            errors.append(_render_error(
                "SCRIPT_REJECTED_BY_POLICY",
                f"Bare os.environ enumeration in {path}; "
                "only os.environ[\"OUTPUT_DOCX_PATH\"] is permitted",
            ))

    return errors


# ---------------------------------------------------------------------------
# docx document-program envelope + import handling
# ---------------------------------------------------------------------------

_ENV_FENCE_OPEN = re.compile(r"^```[A-Za-z0-9_-]*\s*\n")
_ENV_FENCE_CLOSE = re.compile(r"\n```\s*$")
_DOCX_PROGRAM_REQUIRED = ("renderer_type", "source_filename", "source_code")
_DOCX_IMPORT_BLOCK = re.compile(r"import\s*\{([^}]*)\}\s*from\s*['\"]docx['\"]")


def _strip_one_fence(text: str) -> str:
    """Strip exactly one wrapping markdown code fence, if present."""
    s = text.strip()
    m = _ENV_FENCE_OPEN.match(s)
    if m and _ENV_FENCE_CLOSE.search(s):
        return _ENV_FENCE_CLOSE.sub("", s[m.end():]).strip()
    return s


def analyze_entrypoint_content(content: str) -> Dict[str, Any]:
    """Classify the entrypoint content: runnable program, docx document-program
    JSON envelope, or a broken in-between.

    Envelope detection tolerates trailing junk after the object: raw_decode
    parses exactly one balanced object, and a complete object is proof the
    envelope was not truncated, so anything after it is discarded. That is where
    the "grab the object, ignore the rest" tolerance lives -- but only the
    *trailing* axis; a body that does not parse to an envelope is 'broken', not
    silently treated as a program.

    Returns one of:
      {"kind": "program"}
      {"kind": "envelope", "source_code": str, "renderer_type": str|None,
       "trailing": str}
      {"kind": "broken", "detail": str}
    """
    stripped = _strip_one_fence(content).lstrip()
    if not stripped.startswith("{"):
        return {"kind": "program"}
    try:
        obj, end = json.JSONDecoder().raw_decode(stripped)
    except json.JSONDecodeError as exc:
        return {"kind": "broken",
                "detail": f"content begins with '{{' but is not parseable JSON ({exc})"}
    if not isinstance(obj, dict):
        return {"kind": "broken", "detail": "content is a JSON value but not an object"}
    missing = [k for k in _DOCX_PROGRAM_REQUIRED if k not in obj]
    if missing:
        return {"kind": "broken",
                "detail": "JSON object is not a docx document-program envelope "
                          f"(missing {', '.join(missing)})"}
    source_code = obj.get("source_code")
    if not isinstance(source_code, str) or not source_code.strip():
        return {"kind": "broken",
                "detail": "envelope 'source_code' is empty or not a string"}
    return {"kind": "envelope", "source_code": source_code,
            "renderer_type": obj.get("renderer_type"),
            "trailing": stripped[end:].strip()}


_DOCX_EXPORTS_CACHE: Optional[set] = None


def get_docx_exports(settings: "ServerSettings") -> Optional[set]:
    """Enumerate the docx npm package's named exports once, cached.

    Returns the set of export names, or None if it could not be determined -- in
    which case import existence-validation is skipped (fail open: never block a
    render just because the probe failed; known-alias repair still runs).
    """
    global _DOCX_EXPORTS_CACHE
    if _DOCX_EXPORTS_CACHE is not None:
        return _DOCX_EXPORTS_CACHE
    node_exe = shutil.which(settings.node_executable) or shutil.which("node")
    if not node_exe:
        return None
    runtime_root = (PROJECT_ROOT / settings.node_runtime_root).resolve()
    try:
        proc = subprocess.run(
            [node_exe, "--input-type=module", "-e",
             "import('docx').then(m=>process.stdout.write("
             "JSON.stringify(Object.keys(m))))"],
            cwd=str(runtime_root),
            capture_output=True, text=True, timeout=15, shell=False,
        )
    except Exception:
        return None
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 or not out.startswith("["):
        return None
    try:
        names = json.loads(out)
    except json.JSONDecodeError:
        return None
    if isinstance(names, list):
        _DOCX_EXPORTS_CACHE = {str(n) for n in names}
        return _DOCX_EXPORTS_CACHE
    return None


def _docx_imported_names(source: str) -> list:
    """All named specifiers imported from the 'docx' package (the name to the
    left of any 'as' alias), in source order, de-duplicated."""
    seen: list = []
    for block in _DOCX_IMPORT_BLOCK.findall(source):
        for spec in block.split(","):
            name = re.split(r"\s+as\s+", spec.strip(), maxsplit=1)[0].strip()
            if name and name not in seen:
                seen.append(name)
    return seen


def repair_and_validate_docx_imports(
    source: str, exports: Optional[set],
) -> Dict[str, Any]:
    """Auto-repair known-alias docx imports and reject unknown ones.

    - Any imported docx symbol in DOCX_IMPORT_ALIASES whose replacement is a
      real export is rewritten wherever it appears (import and usages) as a
      whole-identifier substitution, and reported as a warning.
    - After repair, any imported docx symbol that is still not a real export is
      rejected with a nearest-match suggestion.
    - When 'exports' is None the existence check is skipped (fail open); known
      aliases are still repaired.

    Returns {"source_code": str, "warnings": [str, ...]} on success, or
    {"error": <structured error dict>} if an unknown symbol survives.
    """
    warnings: list = []
    for bad in _docx_imported_names(source):
        good = DOCX_IMPORT_ALIASES.get(bad)
        if not good:
            continue
        if exports is not None and good not in exports:
            continue  # our own map is stale; let the unknown check report it
        source = re.sub(rf"\b{re.escape(bad)}\b", good, source)
        warnings.append(
            f"auto-repaired docx import '{bad}' -> '{good}' "
            f"(docx does not export '{bad}')"
        )

    if exports is not None:
        unknown = [n for n in _docx_imported_names(source) if n not in exports]
        if unknown:
            details = []
            for n in unknown:
                near = difflib.get_close_matches(n, list(exports), n=1)
                hint = f" (did you mean '{near[0]}'?)" if near else ""
                details.append(f"'{n}'{hint}")
            return {"error": _render_error(
                "DOCX_IMPORT_NOT_EXPORTED",
                "Renderer imports docx symbol(s) the installed docx package "
                "does not export: " + ", ".join(details) + ".",
            )}

    return {"source_code": source, "warnings": warnings}


# ---------------------------------------------------------------------------
# Runtime readiness check  (spec §4.2, §14.6)
# ---------------------------------------------------------------------------

def check_node_runtime(
    settings: ServerSettings,
) -> Tuple[bool, str, list[Dict[str, str]]]:
    """Return (ready, node_version, errors)."""
    errors: list[Dict[str, str]] = []
    node_exe = shutil.which(settings.node_executable) or shutil.which("node")

    if not node_exe:
        errors.append(_render_error(
            "NODE_NOT_FOUND",
            f"Node executable '{settings.node_executable}' not found in PATH",
        ))
        return False, "", errors

    # --- check version ---
    try:
        proc = subprocess.run(
            [node_exe, "--version"],
            capture_output=True, text=True, timeout=15, shell=False,
        )
        node_version = proc.stdout.strip()
        ver = re.match(r"v(\d+)\.", node_version)
        if ver and int(ver.group(1)) < 20:
            errors.append(_render_error(
                "NODE_NOT_FOUND",
                f"Node {node_version} is below the required minimum v20",
            ))
            return False, node_version, errors
    except Exception as exc:
        errors.append(_render_error(
            "NODE_NOT_FOUND", f"Failed to check Node version: {exc}",
        ))
        return False, "", errors

    # --- check runtime root exists ---
    runtime_root = (PROJECT_ROOT / settings.node_runtime_root).resolve()
    if not runtime_root.exists():
        errors.append(_render_error(
            "NODE_RUNTIME_NOT_READY",
            f"Runtime root '{settings.node_runtime_root}' does not exist",
            detail=f"Create '{settings.node_runtime_root}/' with package.json and run 'npm install'",
        ))
        return False, node_version, errors

    # --- check docx package importable ---
    try:
        proc = subprocess.run(
            [node_exe, "--input-type=module", "-e",
             "import { Document, Packer } from 'docx'; console.log('docx OK')"],
            cwd=str(runtime_root),
            capture_output=True, text=True, timeout=15, shell=False,
        )
        if proc.returncode != 0 or "docx OK" not in proc.stdout:
            stderr_tail = (proc.stderr or "").strip()[:500]
            errors.append(_render_error(
                "NODE_PACKAGE_MISSING",
                "The 'docx' package is not importable from the runtime root",
                detail=(
                    f"Run 'npm install' (or 'npm ci' if a lockfile exists) "
                    f"inside '{settings.node_runtime_root}/'.  stderr: {stderr_tail}"
                ),
            ))
            return False, node_version, errors
    except Exception as exc:
        errors.append(_render_error(
            "NODE_RUNTIME_NOT_READY",
            f"Failed to verify docx package: {exc}",
        ))
        return False, node_version, errors

    return True, node_version, errors


#: Memoized python-runtime probe, keyed by (executable, runtime root).
#:
#: The answer cannot change within a process lifetime, and this check is called
#: from `health_check` as well as from render. Without the cache, every
#: health_check spawns two subprocesses to verify a runtime that most callers
#: never use -- which is how a working server came to report
#: `python: ready: false` because its *check* timed out, not because anything
#: was missing.
_PYTHON_RUNTIME_PROBE: Dict[Tuple[str, str], Tuple[bool, str, list[Dict[str, str]]]] = {}

#: Generous because the failure it guards against is a cold native-extension
#: import (python-docx pulls in lxml) on a machine with real-time antivirus.
#: The probe runs once per process, so a large ceiling costs nothing.
PYTHON_PROBE_TIMEOUT_SECONDS = 60


def _probe(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    """Run a preflight probe without inheriting the server's stdio.

    `stdin=DEVNULL` matters here specifically: this server runs as an MCP stdio
    child, so its stdin is a live pipe owned by the client. Letting a probe
    inherit that handle is the difference between this command taking 0.15s
    from a shell and stalling under the server.
    """
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=False,
        stdin=subprocess.DEVNULL,
    )


def check_python_runtime(
    settings: ServerSettings,
) -> Tuple[bool, str, list[Dict[str, str]]]:
    """Return (ready, python_version, errors). Memoized per process."""
    cache_key = (settings.python_executable, settings.python_runtime_root)
    cached = _PYTHON_RUNTIME_PROBE.get(cache_key)
    if cached is not None:
        return cached

    result = _check_python_runtime_uncached(settings)
    _PYTHON_RUNTIME_PROBE[cache_key] = result
    return result


def _check_python_runtime_uncached(
    settings: ServerSettings,
) -> Tuple[bool, str, list[Dict[str, str]]]:
    errors: list[Dict[str, str]] = []
    py_exe = settings.python_executable

    if not shutil.which(py_exe):
        errors.append(_render_error(
            "PYTHON_NOT_FOUND",
            f"Python executable '{py_exe}' not found in PATH",
        ))
        return False, "", errors

    try:
        proc = _probe([py_exe, "--version"], PYTHON_PROBE_TIMEOUT_SECONDS)
        python_version = proc.stdout.strip() or proc.stderr.strip()
    except Exception as exc:
        errors.append(_render_error(
            "PYTHON_NOT_FOUND",
            f"Failed to check Python version: {exc}",
        ))
        return False, "", errors

    runtime_root = (PROJECT_ROOT / settings.python_runtime_root).resolve()
    if not runtime_root.exists():
        errors.append(_render_error(
            "PYTHON_RUNTIME_NOT_READY",
            f"Runtime root '{settings.python_runtime_root}' does not exist",
            detail=f"Create '{settings.python_runtime_root}/workspaces/' directory",
        ))
        return False, python_version, errors

    try:
        proc = _probe(
            [py_exe, "-c", "from docx import Document; print('python-docx OK')"],
            PYTHON_PROBE_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0 or "python-docx OK" not in proc.stdout:
            stderr_tail = (proc.stderr or "").strip()[:500]
            errors.append(_render_error(
                "PYTHON_PACKAGE_MISSING",
                "Python renderer requires python-docx.",
                detail=f"Install with: pip install python-docx.  stderr: {stderr_tail}",
            ))
            return False, python_version, errors
    except subprocess.TimeoutExpired:
        # A probe that ran out of time proves nothing about the runtime, so do
        # not report it as broken. python-docx may be perfectly importable and
        # merely slow to load cold. Rendering will find out for real, and will
        # say so with the actual import error rather than this one.
        errors.append(_render_error(
            "PYTHON_RUNTIME_UNVERIFIED",
            f"python-docx check did not finish within "
            f"{PYTHON_PROBE_TIMEOUT_SECONDS}s; the runtime was not verified.",
            detail="This is not evidence the runtime is broken. A cold "
                   "python-docx import (it loads lxml) can be slow under "
                   "real-time antivirus. Python rendering will report the real "
                   "error if the package is genuinely unusable.",
        ))
        return False, python_version, errors
    except Exception as exc:
        errors.append(_render_error(
            "PYTHON_RUNTIME_NOT_READY",
            f"Failed to verify python-docx: {exc}",
        ))
        return False, python_version, errors

    return True, python_version, errors


# ---------------------------------------------------------------------------
# Node execution  (spec §7.1 steps 13-14, §14.7)
# ---------------------------------------------------------------------------

def run_node_renderer(
    workspace: Path,
    entrypoint: str,
    output_path: Path,
    timeout_seconds: int,
    settings: ServerSettings,
    metadata_json_path: Path | None = None,
) -> Dict[str, Any]:
    node_exe = shutil.which(settings.node_executable) or shutil.which("node") or "node"

    env = {k: v for k, v in os.environ.items() if k.upper() not in SENSITIVE_ENV_VARS}
    env["OUTPUT_DOCX_PATH"] = str(output_path)
    env["DOCX_RENDER_WORKSPACE"] = str(workspace)
    if metadata_json_path and metadata_json_path.exists():
        env["DOCX_RENDER_METADATA_PATH"] = str(metadata_json_path)

    start = time.perf_counter()
    try:
        proc = subprocess.run(
            [node_exe, entrypoint],
            cwd=str(workspace),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            shell=False,
        )
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return {
            "command": [node_exe, entrypoint],
            "cwd": str(workspace),
            "exit_code": proc.returncode,
            "timeout_seconds": timeout_seconds,
            "duration_ms": elapsed_ms,
            "timed_out": False,
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
        }
    except subprocess.TimeoutExpired:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return {
            "command": [node_exe, entrypoint],
            "cwd": str(workspace),
            "exit_code": None,
            "timeout_seconds": timeout_seconds,
            "duration_ms": elapsed_ms,
            "timed_out": True,
            "stdout": "",
            "stderr": f"Process timed out after {timeout_seconds} seconds",
        }
    except FileNotFoundError:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return {
            "command": [node_exe, entrypoint],
            "cwd": str(workspace),
            "exit_code": None,
            "timeout_seconds": timeout_seconds,
            "duration_ms": elapsed_ms,
            "timed_out": False,
            "stdout": "",
            "stderr": f"Node executable not found: {node_exe}",
        }


def run_python_renderer(
    workspace: Path,
    entrypoint: str,
    output_path: Path,
    timeout_seconds: int,
    settings: ServerSettings,
    metadata_json_path: Path | None = None,
) -> Dict[str, Any]:
    py_exe = settings.python_executable

    env = {k: v for k, v in os.environ.items() if k.upper() not in SENSITIVE_ENV_VARS}
    env["OUTPUT_DOCX_PATH"] = str(output_path)
    env["DOCX_RENDER_WORKSPACE"] = str(workspace)
    if metadata_json_path and metadata_json_path.exists():
        env["DOCX_RENDER_METADATA_PATH"] = str(metadata_json_path)

    start = time.perf_counter()
    try:
        proc = subprocess.run(
            [py_exe, entrypoint],
            cwd=str(workspace),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            shell=False,
        )
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return {
            "command": [py_exe, entrypoint],
            "cwd": str(workspace),
            "exit_code": proc.returncode,
            "timeout_seconds": timeout_seconds,
            "duration_ms": elapsed_ms,
            "timed_out": False,
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
        }
    except subprocess.TimeoutExpired:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return {
            "command": [py_exe, entrypoint],
            "cwd": str(workspace),
            "exit_code": None,
            "timeout_seconds": timeout_seconds,
            "duration_ms": elapsed_ms,
            "timed_out": True,
            "stdout": "",
            "stderr": f"Process timed out after {timeout_seconds} seconds",
        }
    except FileNotFoundError:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return {
            "command": [py_exe, entrypoint],
            "cwd": str(workspace),
            "exit_code": None,
            "timeout_seconds": timeout_seconds,
            "duration_ms": elapsed_ms,
            "timed_out": False,
            "stdout": "",
            "stderr": f"Python executable not found: {py_exe}",
        }


# ---------------------------------------------------------------------------
# DOCX validation  (spec §8, §14.8)
# ---------------------------------------------------------------------------

def validate_docx(path: Path) -> Tuple[Dict[str, Any], list[str]]:
    """Return (validation_dict, warnings)."""
    result: Dict[str, Any] = {
        "is_zip": False,
        "has_content_types": False,
        "has_root_rels": False,
        "has_document_xml": False,
        "document_xml_bytes": 0,
    }
    warnings: list[str] = []

    if not path.exists():
        return result, warnings

    try:
        with zipfile.ZipFile(path) as zf:
            result["is_zip"] = True
            names = set(zf.namelist())

            if "[Content_Types].xml" in names:
                result["has_content_types"] = True
            if "_rels/.rels" in names:
                result["has_root_rels"] = True
            if "word/document.xml" in names:
                result["has_document_xml"] = True
                result["document_xml_bytes"] = zf.getinfo("word/document.xml").file_size

            # Optional deeper checks — warnings only
            if "word/styles.xml" not in names:
                warnings.append("word/styles.xml not found (optional)")
            if "docProps/core.xml" not in names:
                warnings.append("docProps/core.xml not found (optional)")

            if result["has_document_xml"]:
                try:
                    doc_xml = zf.read("word/document.xml").decode("utf-8", errors="ignore")
                    text = re.sub(r"<[^>]+>", " ", doc_xml)
                    for placeholder in ("{{company}}", "{{role}}", "TODO", "PLACEHOLDER"):
                        if placeholder in text:
                            warnings.append(
                                f"Possible unreplaced placeholder '{placeholder}' in document body"
                            )
                except Exception:
                    warnings.append("Could not introspect document.xml for placeholder scan")

    except zipfile.BadZipFile:
        result["is_zip"] = False
    except Exception:
        result["is_zip"] = False

    return result, warnings


# ---------------------------------------------------------------------------
# HTTP/HTTPS scaffolding
# ---------------------------------------------------------------------------

class FastAppSettings:
    def __init__(self, settings: ServerSettings, mcp_app: FastMCP) -> None:
        self.expose_url: str = str(settings.server_url)
        self.dns: str | None = None
        self.settings: ServerSettings = settings
        self.servers: list[FastMCP] = [mcp_app]


class FastApp:
    def __init__(self, fast_app_settings: FastAppSettings) -> None:
        self.app_settings = fast_app_settings

    def create_app(self) -> FastAPI:
        if not FASTAPI_AVAILABLE:
            raise ImportError(
                "FastAPI not available. Install with: pip install fastapi uvicorn"
            )

        servers = self.app_settings.servers

        @contextlib.asynccontextmanager
        async def lifespan(app: FastAPI) -> Any:
            async with contextlib.AsyncExitStack() as stack:
                for server in servers:
                    await stack.enter_async_context(server.session_manager.run())
                yield

        app = FastAPI(lifespan=lifespan)

        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        for server in servers:
            app.mount("/mcp", server.streamable_http_app())
            app.mount(f"/{server.name}", server.streamable_http_app())

        @app.get("/", include_in_schema=False)
        async def redirect_to_help() -> RedirectResponse:
            return RedirectResponse(url="/help")

        @app.get("/help", include_in_schema=False)
        async def help_page() -> HTMLResponse:
            try:
                html = (
                    "<html><head><title>DOCX Renderer MCP Server</title></head>"
                    "<body><h1>DOCX Renderer MCP Server</h1>"
                    "<p>Controlled execution harness for AI-generated DOCX renderer scripts.</p>"
                    "<h2>Servers</h2>"
                )
                for srv in servers:
                    tools: list[str] = []
                    try:
                        tools = [t.name for t in srv._tool_manager.list_tools()]
                    except Exception:
                        pass
                    html += (
                        f"<h3>{srv.name}</h3>"
                        f"<p><strong>Endpoint:</strong> <code>"
                        f"{self.app_settings.expose_url}/mcp</code></p>"
                        f"<p><strong>Tools:</strong> "
                        f"{', '.join(tools) if tools else 'None'}</p>"
                    )
                html += "</body></html>"
                return HTMLResponse(content=html, status_code=200)
            except Exception as exc:
                logger.error("Error generating help page: %s", exc)
                raise HTTPException(status_code=500, detail="Error generating help page")

        return app


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

class MCPServer:
    def __init__(self, settings: ServerSettings) -> None:
        self._settings = settings
        self._logger = logger

    def create_public_server(self) -> FastMCP | None:
        settings = self._settings
        try:
            output_root = (PROJECT_ROOT / settings.output_root).resolve()

            mcp = FastMCP(
                name=settings.server_name,
                instructions=(
                    "Controlled DOCX execution harness. Accepts an AI-generated "
                    "renderer script (Node.js or Python), executes it in a sandboxed "
                    "workspace, and returns a validated .docx file path."
                ),
                debug=True,
                stateless_http=True,
            )
        except Exception as exc:
            self._logger.error("Failed to initialise MCP server: %s", exc)
            return None

        # ------------------------------------------------------------------
        # health_check
        # ------------------------------------------------------------------
        @mcp.tool()
        def health_check() -> Dict[str, Any]:
            """Report runtime readiness for the renderer service."""
            node_ready, node_version, node_errors = check_node_runtime(settings)
            py_ready, py_version, py_errors = check_python_runtime(settings)
            runtime_ready = {
                "node": node_ready,
                "python": py_ready,
            }
            ready_renderer_types = [
                renderer_type
                for renderer_type in settings.allowed_renderer_types
                if runtime_ready.get(renderer_type, False)
            ]
            unavailable_renderer_types = [
                renderer_type
                for renderer_type in settings.allowed_renderer_types
                if not runtime_ready.get(renderer_type, False)
            ]
            service_ready = bool(ready_renderer_types)
            return {
                "status": "ready" if service_ready else "not_ready",
                "allowed_document_types": list(settings.allowed_document_types),
                "allowed_renderer_types": list(settings.allowed_renderer_types),
                "ready_renderer_types": ready_renderer_types,
                "unavailable_renderer_types": unavailable_renderer_types,
                "output_root": settings.output_root,
                "file_source_enabled": bool(parse_directory_aliases(settings.directory_aliases)),
                "directory_aliases": {
                    name: str(directory)
                    for name, directory in parse_directory_aliases(settings.directory_aliases).items()
                },
                "node": {
                    "ready": node_ready,
                    "version": node_version,
                    "runtime_root": settings.node_runtime_root,
                    "errors": node_errors,
                },
                "python": {
                    "ready": py_ready,
                    "version": py_version,
                    "runtime_root": settings.python_runtime_root,
                    "errors": py_errors,
                },
            }

        # ------------------------------------------------------------------
        # render_docx  (spec §5, §7, §13)
        # ------------------------------------------------------------------
        @mcp.tool(
            name="render_docx",
            description=(
                "Runs an approved AI-generated DOCX renderer script and returns "
                "a validated DOCX output file path. The renderer script must "
                "write to OUTPUT_DOCX_PATH (process.env in Node, os.environ in Python)."
            ),
        )
        def render_docx(
            document_type: Annotated[str, Field(
                description="Document kind: 'resume', 'cover_letter', or 'generic'",
            )],
            renderer_type: Annotated[str, Field(
                description="Renderer runtime: 'node' or 'python'.",
            )],
            filename_pattern: Annotated[str, Field(
                description=(
                    "Filename template with placeholders: "
                    "{document_type} {company} {role} {timestamp} {date} {slug}"
                ),
            )],
            files: Annotated[List[Dict[str, str]], Field(
                description=(
                    "Renderer files. Each entry is either {path, content} with "
                    "inline content, or {path, source_path} where source_path "
                    "is an 'alias/relative/path' reference the server reads "
                    "itself (resolved against the directories configured for "
                    "that alias in directory_aliases). An entry must not supply "
                    "both content and source_path."
                ),
            )],
            entrypoint: Annotated[str, Field(
                description="Must be 'render-document.mjs' (node) or 'render_document.py' (python)",
            )],
            filename_values: Annotated[Dict[str, str], Field(
                description="Values to substitute into filename_pattern, e.g. {company, role}",
            )],
            source_markdown: Annotated[Optional[str], Field(
                description="Optional source markdown for audit/debug; not rendered",
            )] = None,
            metadata: Annotated[Optional[Dict[str, Any]], Field(
                description="Optional metadata dict (company, role, job_id, generated_by, …)",
            )] = None,
            options: Annotated[Optional[Dict[str, Any]], Field(
                description="Optional: {timeout_seconds: int, keep_workspace: bool}",
            )] = None,
        ) -> Dict[str, Any]:
            render_id = ""
            ws_path: Path | None = None
            try:
                return _do_render(
                    document_type=document_type,
                    renderer_type=renderer_type,
                    filename_pattern=filename_pattern,
                    files=files,
                    entrypoint=entrypoint,
                    filename_values=filename_values,
                    source_markdown=source_markdown,
                    metadata=metadata,
                    options=options or {},
                    settings=settings,
                    output_root=output_root,
                )
            except Exception as exc:
                return _fail_result(
                    status="internal_error",
                    document_type=document_type,
                    renderer_type=renderer_type,
                    render_id=render_id,
                    errors=[_render_error(
                        "INTERNAL_ERROR",
                        str(exc),
                        detail=traceback.format_exc()[:2000],
                    )],
                )

        # Tool compatibility patch
        try:
            for tool in mcp._tool_manager.list_tools():
                if not hasattr(tool, "function"):
                    func = (
                        getattr(tool, "_callback", None)
                        or getattr(tool, "callback", None)
                        or getattr(tool, "call", None)
                        or getattr(tool, "fn", None)
                    )
                    if func is not None:
                        setattr(tool, "function", func)
        except Exception:
            pass

        return mcp

    # --- transport helpers ------------------------------------------------

    def run_stdio_server(self) -> None:
        server = self.create_public_server()
        if server is None:
            self._logger.error("Failed to create server")
            return
        self._logger.info("Starting MCP server in stdio mode")
        server.run()

    def run_http_server_with_mcp(self) -> int:
        server = self.create_public_server()
        if server is None:
            self._logger.error("Failed to create server")
            return 1
        self._run_http(server)
        return 0

    def _run_http(self, mcp_app: FastMCP) -> None:
        if not FASTAPI_AVAILABLE:
            self._logger.error(
                "FastAPI not available. Install with: pip install fastapi uvicorn"
            )
            return

        app_settings = FastAppSettings(self._settings, mcp_app)
        app = FastApp(app_settings).create_app()

        ssl_key = (
            self._settings.ssl_key_path
            if self._settings.transport == "https" else None
        )
        ssl_cert = (
            self._settings.ssl_cert_path
            if self._settings.transport == "https" else None
        )
        if self._settings.transport == "https" and (not ssl_key or not ssl_cert):
            self._logger.warning(
                "HTTPS requested but SSL cert/key not provided — falling back to HTTP"
            )
            ssl_key = ssl_cert = None

        self._logger.info(
            "Starting server on %s:%s (%s)",
            self._settings.host,
            self._settings.port,
            "HTTPS" if ssl_key else "HTTP",
        )
        uvicorn.run(
            app,
            host=self._settings.host,
            port=self._settings.port,
            ssl_keyfile=ssl_key,
            ssl_certfile=ssl_cert,
            log_level="info",
        )


# ---------------------------------------------------------------------------
# Core render logic (extracted so the tool handler stays small)
# ---------------------------------------------------------------------------

def _do_render(
    *,
    document_type: str,
    renderer_type: str,
    filename_pattern: str,
    files: list[Dict[str, str]],
    entrypoint: str,
    filename_values: Dict[str, str],
    source_markdown: str | None,
    metadata: Dict[str, Any] | None,
    options: Dict[str, Any],
    settings: ServerSettings,
    output_root: Path,
) -> Dict[str, Any]:
    """Full render pipeline.  (spec §7.1 steps 1-21)"""
    render_id = ""
    ws_path: Path | None = None
    ws_kept = False
    all_warnings: list[str] = []

    def fail(
        status: str,
        errs: list[Dict[str, str]],
        *,
        execution: Dict[str, Any] | None = None,
        stdout: str = "",
        stderr: str = "",
    ) -> Dict[str, Any]:
        nonlocal ws_kept
        keep = settings.keep_failed_workspaces
        if ws_path and ws_path.exists():
            if keep:
                ws_kept = True
            else:
                shutil.rmtree(ws_path, ignore_errors=True)
        return _fail_result(
            status=status,
            document_type=document_type,
            renderer_type=renderer_type,
            render_id=render_id,
            errors=errs,
            warnings=all_warnings,
            execution=execution,
            workspace_path=str(ws_path) if ws_kept else None,
            workspace_kept=ws_kept,
            stdout=stdout,
            stderr=stderr,
        )

    # 1-2. validate document_type
    if document_type not in settings.allowed_document_types:
        return fail("invalid_request", [_render_error(
            "INVALID_DOCUMENT_TYPE",
            f"Invalid document_type '{document_type}'. "
            f"Allowed: {list(settings.allowed_document_types)}",
        )])

    # 3. validate renderer_type
    if renderer_type not in settings.allowed_renderer_types:
        if renderer_type in FUTURE_RENDERER_TYPES:
            return fail("unsupported_renderer", [_render_error(
                "UNSUPPORTED_RENDERER_TYPE",
                f"Renderer type '{renderer_type}' is reserved for a future release",
            )])
        return fail("invalid_request", [_render_error(
            "UNSUPPORTED_RENDERER_TYPE",
            f"Unknown renderer_type '{renderer_type}'. "
            f"Allowed: {list(settings.allowed_renderer_types)}",
        )])

    # 4. validate entrypoint (strict per renderer_type)
    expected_entrypoint = REQUIRED_ENTRYPOINTS.get(renderer_type)
    if entrypoint != expected_entrypoint:
        return fail("invalid_request", [_render_error(
            "ENTRYPOINT_NOT_ALLOWED",
            f"Entrypoint for renderer_type='{renderer_type}' must be "
            f"'{expected_entrypoint}', got '{entrypoint}'",
        )])

    # 5. validate files
    if not files:
        return fail("invalid_request", [_render_error(
            "INVALID_FILE_PATH", "No renderer files supplied",
        )])

    # 5a. server-side file transport: read any entry that supplies a
    # 'source_path' instead of inline 'content' (fail closed on any problem).
    materialized, file_err, file_sources = materialize_file_entries(files, settings)
    if file_err is not None:
        return fail("invalid_request", [file_err])
    files = materialized

    if len(files) > settings.max_file_count:
        return fail("invalid_request", [_render_error(
            "TOO_MANY_FILES",
            f"Submitted {len(files)} files, max is {settings.max_file_count}",
        )])

    total_bytes = 0
    file_paths: set[str] = set()
    for entry in files:
        err = validate_file_entry(entry, settings)
        if err:
            return fail("invalid_request", [err])
        total_bytes += len(entry.get("content", "").encode("utf-8"))
        file_paths.add(entry["path"].replace("\\", "/"))

    if total_bytes > settings.max_total_file_bytes:
        return fail("invalid_request", [_render_error(
            "FILE_TOO_LARGE",
            f"Total payload is {total_bytes:,} bytes, "
            f"exceeds limit of {settings.max_total_file_bytes:,}",
        )])

    if entrypoint.replace("\\", "/") not in file_paths:
        return fail("invalid_request", [_render_error(
            "INVALID_FILE_PATH",
            f"Entrypoint '{entrypoint}' is not among the supplied files",
        )])

    # 5b. node: extract a docx document-program JSON envelope in-process, then
    #     repair/validate the program's docx imports before it is executed.
    #
    #     The entrypoint may arrive either as a runnable module OR as the raw
    #     JSON envelope the AI server returned ({renderer_type, source_filename,
    #     source_code}). Extracting source_code here -- rather than having the
    #     caller hand-write an intermediate .mjs -- removes the empty-file
    #     handoff race and tolerates trailing junk a strict JSON contract would
    #     reject. Two independent signals decide envelope-vs-module: the source
    #     file's extension and the content shape. They must agree; a mismatch is
    #     a hard error, because it means an upstream step misfired.
    if renderer_type == "node":
        ep_path = entrypoint.replace("\\", "/")
        ep_entry = next(
            (e for e in files if e.get("path", "").replace("\\", "/") == ep_path),
            None,
        )
        if ep_entry is not None:
            src_ext = os.path.splitext(
                str(file_sources.get(ep_entry["path"], "")))[1].lower()
            info = analyze_entrypoint_content(ep_entry.get("content", ""))
            kind = info["kind"]

            if kind == "broken":
                return fail("invalid_request", [_render_error(
                    "DOCX_PROGRAM_MALFORMED",
                    f"Entrypoint '{entrypoint}' is neither a runnable module nor "
                    f"a usable docx document-program envelope: {info['detail']}.",
                )])
            if src_ext == ".json" and kind == "program":
                return fail("invalid_request", [_render_error(
                    "DOCX_PROGRAM_TYPE_MISMATCH",
                    "Entrypoint source file is '.json' but its content is a "
                    "renderer module, not a docx document-program envelope. "
                    "Signals disagree; refusing to guess.",
                )])
            if src_ext in (".mjs", ".js") and kind == "envelope":
                return fail("invalid_request", [_render_error(
                    "DOCX_PROGRAM_TYPE_MISMATCH",
                    f"Entrypoint source file is '{src_ext}' but its content is a "
                    "docx document-program JSON envelope (was envelope extraction "
                    "skipped upstream?). Signals disagree; refusing to guess.",
                )])
            if kind == "envelope":
                ep_entry["content"] = info["source_code"]
                all_warnings.append(
                    "Extracted docx document program from JSON envelope "
                    f"({len(info['source_code'])} chars of source_code); the "
                    "envelope wrapper was not executed."
                )
                if info["trailing"]:
                    all_warnings.append(
                        f"Discarded {len(info['trailing'])} character(s) of "
                        "trailing content after the envelope object; used the "
                        "first complete object. Verify the rendered .docx."
                    )

        # repair known-alias docx imports and reject unknown ones, on every node
        # code file, before the policy scan sees the final JavaScript.
        docx_exports = get_docx_exports(settings)
        for entry in files:
            if os.path.splitext(
                    entry.get("path", ""))[1].lower() not in (".mjs", ".js"):
                continue
            rep = repair_and_validate_docx_imports(
                entry.get("content", ""), docx_exports)
            if "error" in rep:
                return fail("rejected_by_policy", [rep["error"]])
            entry["content"] = rep["source_code"]
            all_warnings.extend(rep["warnings"])

    # 6. validate script safety (renderer-specific scanner)
    if renderer_type == "node":
        policy_errors = validate_node_script_policy(files)
    elif renderer_type == "python":
        policy_errors = validate_python_script_policy(files)
    else:
        policy_errors = []
    if policy_errors:
        return fail("rejected_by_policy", policy_errors)

    # 7. confirm runtime readiness (renderer-specific)
    if renderer_type == "node":
        ready, _ver, rt_errors = check_node_runtime(settings)
    elif renderer_type == "python":
        ready, _ver, rt_errors = check_python_runtime(settings)
    else:
        ready, rt_errors = False, [_render_error(
            "UNSUPPORTED_RENDERER_TYPE",
            f"No runtime check for '{renderer_type}'",
        )]
    if not ready:
        return fail("runtime_not_ready", rt_errors)

    # 8. generate render ID
    render_id = generate_render_id()

    # 9. create workspace (under the renderer-specific runtime root)
    if renderer_type == "node":
        rt_root = (PROJECT_ROOT / settings.node_runtime_root).resolve()
    elif renderer_type == "python":
        rt_root = (PROJECT_ROOT / settings.python_runtime_root).resolve()
    else:
        rt_root = (PROJECT_ROOT / "renderer-runtime").resolve()
    workspace_base = rt_root / settings.workspace_root_name
    ws_path = workspace_base / render_id
    try:
        ws_path.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return fail("workspace_create_failed", [_render_error(
            "WORKSPACE_CREATE_FAILED",
            f"Could not create workspace: {exc}",
        )])

    # 10. write submitted files
    for entry in files:
        rel = entry["path"]
        target = (ws_path / rel).resolve()
        if not str(target).startswith(str(ws_path.resolve())):
            return fail("invalid_request", [_render_error(
                "INVALID_FILE_PATH",
                f"File path escapes workspace: {rel!r}",
            )])
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(entry["content"], encoding="utf-8")
        except Exception as exc:
            return fail("workspace_create_failed", [_render_error(
                "WORKSPACE_CREATE_FAILED",
                f"Failed to write file '{rel}': {exc}",
            )])

    # 11. optional artifacts
    metadata_json_path: Path | None = None
    if source_markdown:
        (ws_path / "source.md").write_text(source_markdown, encoding="utf-8")
    if metadata:
        metadata_json_path = ws_path / "metadata.json"
        metadata_json_path.write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # 12. compute final output path
    filename, fn_warnings = build_output_filename(
        filename_pattern, filename_values, document_type,
    )
    all_warnings.extend(fn_warnings)
    try:
        output_root.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return fail("output_write_failed", [_render_error(
            "OUTPUT_WRITE_FAILED",
            f"Could not create output directory: {exc}",
        )])
    output_path = (output_root / filename).resolve()

    # 13. resolve timeout
    timeout = settings.default_timeout_seconds
    raw_timeout = options.get("timeout_seconds")
    if raw_timeout is not None:
        try:
            timeout = max(int(raw_timeout), 1)
        except (TypeError, ValueError):
            pass
    if timeout > settings.max_timeout_seconds:
        all_warnings.append(
            f"timeout_seconds {timeout} capped to {settings.max_timeout_seconds}"
        )
        timeout = settings.max_timeout_seconds

    # 14. execute renderer (runtime-specific)
    runner = run_node_renderer if renderer_type == "node" else run_python_renderer
    exec_result = runner(
        workspace=ws_path,
        entrypoint=entrypoint,
        output_path=output_path,
        timeout_seconds=timeout,
        settings=settings,
        metadata_json_path=metadata_json_path,
    )

    execution = {
        "command": exec_result["command"],
        "cwd": exec_result["cwd"],
        "exit_code": exec_result["exit_code"],
        "timeout_seconds": exec_result["timeout_seconds"],
        "duration_ms": exec_result["duration_ms"],
    }

    # 15. check for timeout
    if exec_result["timed_out"]:
        return fail(
            "execution_timeout",
            [_render_error(
                "EXECUTION_TIMEOUT",
                f"Renderer timed out after {timeout} seconds",
            )],
            execution=execution,
            stdout=exec_result["stdout"],
            stderr=exec_result["stderr"],
        )

    # 16. check exit code
    if exec_result["exit_code"] != 0:
        stderr_tail = (exec_result["stderr"] or "").strip()
        if len(stderr_tail) > 4000:
            stderr_tail = stderr_tail[-4000:]
        return fail(
            "execution_failed",
            [_render_error(
                "EXECUTION_FAILED",
                f"Renderer ({renderer_type}) exited with code {exec_result['exit_code']}.",
                detail=stderr_tail,
            )],
            execution=execution,
            stdout=exec_result["stdout"],
            stderr=exec_result["stderr"],
        )

    # 17. output must exist
    if not output_path.exists():
        return fail(
            "output_not_created",
            [_render_error(
                "OUTPUT_NOT_CREATED",
                "Renderer completed but no DOCX was produced at OUTPUT_DOCX_PATH",
            )],
            execution=execution,
            stdout=exec_result["stdout"],
            stderr=exec_result["stderr"],
        )

    size_bytes = output_path.stat().st_size
    if size_bytes < 1024:
        return fail(
            "output_too_small",
            [_render_error(
                "OUTPUT_TOO_SMALL",
                f"Output file is only {size_bytes} bytes (minimum ~1 KB expected)",
            )],
            execution=execution,
        )

    # 18. validate DOCX structure
    validation, val_warnings = validate_docx(output_path)
    all_warnings.extend(val_warnings)

    if not validation["is_zip"]:
        return fail(
            "output_not_zip",
            [_render_error("OUTPUT_NOT_ZIP", "Output file is not a valid ZIP archive")],
            execution=execution,
        )

    missing_parts = []
    if not validation["has_content_types"]:
        missing_parts.append("[Content_Types].xml")
    if not validation["has_root_rels"]:
        missing_parts.append("_rels/.rels")
    if not validation["has_document_xml"]:
        missing_parts.append("word/document.xml")
    if missing_parts:
        return fail(
            "output_invalid_docx",
            [_render_error(
                "OUTPUT_INVALID_DOCX",
                f"DOCX is missing required parts: {', '.join(missing_parts)}",
            )],
            execution=execution,
        )

    # 19-20. cleanup workspace
    keep_ws = bool(options.get("keep_workspace", False))
    workspace_kept = keep_ws or settings.keep_successful_workspaces
    if ws_path and ws_path.exists() and not workspace_kept:
        shutil.rmtree(ws_path, ignore_errors=True)

    # 21. success
    return _success_result(
        document_type=document_type,
        renderer_type=renderer_type,
        render_id=render_id,
        filename=filename,
        output_path=str(output_path).replace("\\", "/"),
        relative_path=make_output_relative_path(output_path, output_root),
        size_bytes=size_bytes,
        execution=execution,
        validation=validation,
        workspace_path=str(ws_path).replace("\\", "/") if workspace_kept else None,
        workspace_kept=workspace_kept,
        warnings=all_warnings,
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> int:
    logger.info("DOCX Renderer MCP Server initializing...")

    # Pre-parse to load config file first.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument(
        "-c", "--config", type=str,
        default=os.getenv("MCP_CONFIG_PATH", "docx-renderer-server.yaml"),
    )
    pre_args, _ = pre.parse_known_args()

    config: Dict[str, Any] = load_yaml_config(Path(pre_args.config))
    cfg = config.get("defaults") or {}

    def _s(env: str, cfg_val: Any, default: str) -> str:
        v = os.getenv(env)
        return v if v is not None else (str(cfg_val) if cfg_val is not None else default)

    def _i(env: str, cfg_val: Any, default: int) -> int:
        v = os.getenv(env)
        return int(v) if v is not None else (int(cfg_val) if cfg_val is not None else default)

    def _tuple(env: str, cfg_val: Any, default: tuple[str, ...]) -> tuple[str, ...]:
        v = os.getenv(env)
        raw = v if v is not None else cfg_val
        if raw is None:
            return default
        if isinstance(raw, str):
            values = [part.strip() for part in raw.split(",")]
        else:
            values = [str(part).strip() for part in raw]
        return tuple(part for part in values if part)

    parser = argparse.ArgumentParser(
        description="DOCX Renderer MCP Server",
        epilog=(
            "Config precedence (lowest -> highest):\n"
            "  built-in defaults < YAML config < MCP_* env vars < CLI flags"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-d", "--debug", action="store_true")
    parser.add_argument(
        "--transport", choices=["stdio", "http", "https"],
        default=os.getenv("MCP_TRANSPORT", "stdio"),
    )
    parser.add_argument("--host", default=os.getenv("MCP_HOST", "localhost"))
    parser.add_argument(
        "--port", type=int, default=int(os.getenv("MCP_PORT", "8000")),
    )
    parser.add_argument("--ssl-cert", default=os.getenv("MCP_SSL_CERT_PATH"))
    parser.add_argument("--ssl-key", default=os.getenv("MCP_SSL_KEY_PATH"))
    parser.add_argument(
        "-n", "--server-name",
        default=os.getenv("MCP_SERVER_NAME", "docx-renderer-server"),
    )
    parser.add_argument(
        "--description",
        default=os.getenv(
            "MCP_DESCRIPTION",
            "DOCX renderer service for AI-generated renderer scripts",
        ),
    )
    parser.add_argument(
        "-c", "--config",
        default=os.getenv("MCP_CONFIG_PATH", "docx-renderer-server.yaml"),
    )
    parser.add_argument(
        "--output-root",
        default=_s("MCP_OUTPUT_ROOT", cfg.get("output_root"), "generated-docx"),
    )
    parser.add_argument(
        "--node-runtime-root",
        default=_s("MCP_NODE_RUNTIME_ROOT", cfg.get("node_runtime_root"), "node-renderer-runtime"),
    )
    parser.add_argument(
        "--python-runtime-root",
        default=_s("MCP_PYTHON_RUNTIME_ROOT", cfg.get("python_runtime_root"), "python-renderer-runtime"),
    )
    parser.add_argument(
        "--default-timeout", type=int,
        default=_i("MCP_DEFAULT_TIMEOUT_SECONDS", cfg.get("timeout_seconds"), 30),
    )
    parser.add_argument(
        "--max-timeout", type=int,
        default=_i("MCP_MAX_TIMEOUT_SECONDS", cfg.get("max_timeout_seconds"), 120),
    )
    parser.add_argument(
        "--allowed-renderer-types",
        default=",".join(_tuple(
            "MCP_ALLOWED_RENDERER_TYPES",
            cfg.get("allowed_renderer_types"),
            ALLOWED_RENDERER_TYPES,
        )),
        help="Comma-separated renderer runtimes to expose, e.g. 'node' or 'node,python'",
    )
    args = parser.parse_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)

    settings = ServerSettings(
        transport=args.transport,
        host=args.host,
        port=args.port,
        server_url=f"http{'s' if args.transport == 'https' else ''}://{args.host}:{args.port}",
        ssl_cert_path=args.ssl_cert,
        ssl_key_path=args.ssl_key,
        server_name=args.server_name,
        description=args.description,
        config_path=args.config,
        output_root=args.output_root,
        node_runtime_root=args.node_runtime_root,
        python_runtime_root=args.python_runtime_root,
        default_timeout_seconds=args.default_timeout,
        max_timeout_seconds=args.max_timeout,
        allowed_renderer_types=_tuple(
            "MCP_ALLOWED_RENDERER_TYPES",
            args.allowed_renderer_types,
            ALLOWED_RENDERER_TYPES,
        ),
        directory_aliases=config.get("directory_aliases") or {},
    )

    try:
        server = MCPServer(settings)
        if settings.transport == "stdio":
            server.run_stdio_server()
        elif settings.transport in ("http", "https"):
            return server.run_http_server_with_mcp()
        else:
            logger.error("Unknown transport '%s'", settings.transport)
            parser.print_help()
            return 1
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
        return 0
    except Exception as exc:
        logger.error("Server error: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
