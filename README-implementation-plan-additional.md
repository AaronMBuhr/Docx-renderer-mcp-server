# Implementation Plan: Add Python Renderer Support

## Goal

Extend `docx-renderer-server.py` so that `render_docx` accepts **two** renderer types:

| renderer_type | entrypoint             | runtime  | package      |
|---------------|------------------------|----------|--------------|
| `node`        | `render-document.mjs`  | Node.js  | `docx`       |
| `python`      | `render_document.py`   | Python   | `python-docx`|

Both write the final `.docx` to `OUTPUT_DOCX_PATH`. Everything after execution (DOCX validation, result shape, cleanup) stays shared.

---

## File map

Only one source file changes: `docx-renderer-server.py`.

Supporting files to create:

| Path | Purpose |
|------|---------|
| `python-renderer-runtime/workspaces/` | Empty dir; Python workspaces land here |
| `docx-renderer-server.yaml` | Update `allowed_renderer_types` |

No new Python dependencies are added to the server itself. The renderer scripts need `python-docx` installed in the active environment, but the server only shell-invokes the Python executable; it does not import `python-docx`.

---

## Step-by-step changes

### Step 1 — Update constants

**Where:** The `# Constants` section (currently lines 62-115).

#### 1a. Move `"python"` from future to allowed

Change:

```python
ALLOWED_RENDERER_TYPES = ("node",)
FUTURE_RENDERER_TYPES = ("python",)
```

To:

```python
ALLOWED_RENDERER_TYPES = ("node", "python")
FUTURE_RENDERER_TYPES: tuple[str, ...] = ()
```

`FUTURE_RENDERER_TYPES` becomes empty. Keep it as a constant so no references break.

#### 1b. Add `.py` to the file extension allowlist

Change:

```python
ALLOWED_FILE_EXTENSIONS = (".mjs", ".js", ".json", ".txt", ".md")
```

To:

```python
ALLOWED_FILE_EXTENSIONS = (".mjs", ".js", ".json", ".txt", ".md", ".py")
```

#### 1c. Add the entrypoint validation map

Add a new constant directly below `ALLOWED_FILE_EXTENSIONS`:

```python
REQUIRED_ENTRYPOINTS: dict[str, str] = {
    "node": "render-document.mjs",
    "python": "render_document.py",
}
```

This is the single source of truth for entrypoint enforcement. Every renderer_type has exactly one allowed entrypoint in v1.

#### 1d. Add Python-specific blocked modules

Add directly below `BLOCKED_NODE_MODULES`:

```python
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
```

**Why `shutil` is blocked entirely:** the spec calls out `shutil.rmtree` specifically, but a renderer script has no legitimate use for any `shutil` function. Blocking the whole module is simpler and equally safe.

#### 1e. Add Python-specific disallowed code patterns

Add directly below `DISALLOWED_CODE_PATTERNS`:

```python
DISALLOWED_PYTHON_PATTERNS: list[tuple[str, str]] = [
    (r"\bos\.system\s*\(", "os.system( call"),
    (r"\bos\.popen\s*\(", "os.popen( call"),
    (r"\bos\.exec\w*\s*\(", "os.exec*( call"),
    (r"\bos\.spawn\w*\s*\(", "os.spawn*( call"),
    (r"\beval\s*\(", "eval( call"),
    (r"\bexec\s*\(", "exec( call"),
    (r"\b__import__\s*\(", "__import__( call"),
    (r"\bshutil\.rmtree\s*\(", "shutil.rmtree( call"),
    (r"https?://", "HTTP URL literal"),
]
```

**Design note:** `exec(` and `eval(` overlap with the Node list, but each list is only applied to its own file type, so duplication is fine and keeps each list self-contained.

#### 1f. Add allowed Python env vars

Add directly below `ALLOWED_PROCESS_ENV_VARS`:

```python
ALLOWED_PYTHON_ENV_VARS = frozenset({
    "OUTPUT_DOCX_PATH", "DOCX_RENDER_WORKSPACE", "DOCX_RENDER_METADATA_PATH",
})
```

These are the same three vars the server sets. The constant has a different name so the Python policy function references it independently from the Node one.

---

### Step 2 — Update `ServerSettings`

**Where:** The `class ServerSettings` block (currently lines 159-190).

Replace the single `runtime_root` field with separate Node and Python fields. Add `python_executable`.

Current fields to **remove**:

```python
runtime_root: str = Field(default="node-renderer-runtime")
```

Fields to **add** (insert after `output_root` and before `workspace_root_name`):

```python
    # Node runtime
    node_runtime_root: str = Field(default="node-renderer-runtime")

    # Python runtime
    python_runtime_root: str = Field(default="python-renderer-runtime")
    python_executable: str = Field(default=sys.executable)
```

`sys.executable` points to the same Python running the server, which is where `python-docx` must be installed.

Update the default for `allowed_renderer_types`:

```python
    allowed_renderer_types: tuple[str, ...] = Field(default=ALLOWED_RENDERER_TYPES)
```

This now picks up `("node", "python")` from the updated constant.

**Keep all other fields unchanged.**

---

### Step 3 — Add `validate_python_script_policy`

**Where:** Directly below the existing `validate_node_script_policy` function (after the current line 465).

Create a new function. The structure mirrors `validate_node_script_policy` but uses Python import syntax and the Python-specific constants.

```python
def validate_python_script_policy(files: list[Dict[str, str]]) -> list[Dict[str, str]]:
    """Inspect .py files for disallowed patterns before execution."""
    errors: list[Dict[str, str]] = []

    for entry in files:
        path = entry.get("path", "")
        ext = os.path.splitext(path)[1].lower()
        if ext != ".py":
            continue
        content = entry.get("content", "")

        # --- blocked module imports ---
        # Matches:  import subprocess  /  from subprocess import ...
        #           import subprocess as sp
        for m in re.finditer(
            r"^\s*(?:import|from)\s+([a-zA-Z_][\w.]*)",
            content,
            re.MULTILINE,
        ):
            module = m.group(1)
            # Check the module itself and every dotted prefix.
            # "from http.client import ..." -> check "http.client" and "http".
            parts = module.split(".")
            for i in range(len(parts)):
                prefix = ".".join(parts[: i + 1])
                if prefix in BLOCKED_PYTHON_MODULES:
                    errors.append(_render_error(
                        "SCRIPT_REJECTED_BY_POLICY",
                        f"Blocked import of '{prefix}' in {path}",
                        detail=m.group(0).strip(),
                    ))
                    break  # one error per import line is enough

        # --- dangerous code patterns ---
        for pattern_re, label in DISALLOWED_PYTHON_PATTERNS:
            for m in re.finditer(pattern_re, content):
                errors.append(_render_error(
                    "SCRIPT_REJECTED_BY_POLICY",
                    f"Disallowed pattern '{label}' found in {path}",
                    detail=m.group(0).strip(),
                ))

        # --- os.environ / os.getenv access ---
        # Allow only the three server-set vars.
        allowed_alt = "|".join(ALLOWED_PYTHON_ENV_VARS)

        # os.environ["VAR"] or os.environ.get("VAR")
        for m in re.finditer(
            r"""\bos\.environ\s*[\[.]""",
            content,
        ):
            # Grab the var name that follows
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

        # os.getenv("VAR")
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

        # Bare os.environ without subscript (e.g. dict(os.environ))
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
```

**Algorithm walkthrough (for the import checker):**

1. Regex `^\s*(?:import|from)\s+([a-zA-Z_][\w.]*)` anchors to line start (with optional leading whitespace). It captures the first dotted name after `import` or `from`.
2. For `from http.client import HTTPConnection`, the captured group is `http.client`.
3. We split on `.` to get `["http", "client"]` and check every prefix: `"http"`, then `"http.client"`. If either is in `BLOCKED_PYTHON_MODULES`, reject.
4. This handles all forms: `import subprocess`, `from subprocess import run`, `import http.client`, `from http import client`.

---

### Step 4 — Add `check_python_runtime`

**Where:** Directly below `check_node_runtime` (after its closing `return` around current line 542).

```python
def check_python_runtime(
    settings: ServerSettings,
) -> Tuple[bool, str, list[Dict[str, str]]]:
    """Return (ready, python_version, errors)."""
    errors: list[Dict[str, str]] = []
    py_exe = settings.python_executable

    # --- check executable ---
    if not shutil.which(py_exe):
        errors.append(_render_error(
            "PYTHON_NOT_FOUND",
            f"Python executable '{py_exe}' not found in PATH",
        ))
        return False, "", errors

    # --- check version ---
    try:
        proc = subprocess.run(
            [py_exe, "--version"],
            capture_output=True, text=True, timeout=15, shell=False,
        )
        python_version = proc.stdout.strip() or proc.stderr.strip()
    except Exception as exc:
        errors.append(_render_error(
            "PYTHON_NOT_FOUND",
            f"Failed to check Python version: {exc}",
        ))
        return False, "", errors

    # --- check runtime root exists ---
    runtime_root = (PROJECT_ROOT / settings.python_runtime_root).resolve()
    if not runtime_root.exists():
        errors.append(_render_error(
            "PYTHON_RUNTIME_NOT_READY",
            f"Runtime root '{settings.python_runtime_root}' does not exist",
            detail=f"Create '{settings.python_runtime_root}/workspaces/' directory",
        ))
        return False, python_version, errors

    # --- check python-docx importable ---
    try:
        proc = subprocess.run(
            [py_exe, "-c", "from docx import Document; print('python-docx OK')"],
            capture_output=True, text=True, timeout=15, shell=False,
        )
        if proc.returncode != 0 or "python-docx OK" not in proc.stdout:
            stderr_tail = (proc.stderr or "").strip()[:500]
            errors.append(_render_error(
                "PYTHON_PACKAGE_MISSING",
                "Python renderer requires python-docx.",
                detail=f"Install with: pip install python-docx.  stderr: {stderr_tail}",
            ))
            return False, python_version, errors
    except Exception as exc:
        errors.append(_render_error(
            "PYTHON_RUNTIME_NOT_READY",
            f"Failed to verify python-docx: {exc}",
        ))
        return False, python_version, errors

    return True, python_version, errors
```

**Key difference from Node check:** Python does not need `cwd` to resolve packages — they come from the environment's site-packages. The runtime root is only for workspaces.

---

### Step 5 — Add `run_python_renderer`

**Where:** Directly below `run_node_renderer` (after its closing except block around current line 610).

```python
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
```

This is structurally identical to `run_node_renderer`. The only difference is the executable (`py_exe` vs `node_exe`). The return dict shape is the same so the rest of `_do_render` needs no changes.

---

### Step 6 — Update `MCPServer.create_public_server`

**Where:** Inside `create_public_server` (currently lines 759-868).

#### 6a. Remove the hardcoded single workspace_base

The current code computes one workspace base from `settings.runtime_root`:

```python
output_root = (PROJECT_ROOT / settings.output_root).resolve()
runtime_root = (PROJECT_ROOT / settings.runtime_root).resolve()
workspace_base = runtime_root / settings.workspace_root_name
```

Replace with:

```python
output_root = (PROJECT_ROOT / settings.output_root).resolve()
```

Delete the `runtime_root` and `workspace_base` lines. The workspace base is now determined per-call inside `_do_render` based on `renderer_type`.

#### 6b. Remove `workspace_base` from the `_do_render` call

In the `render_docx` tool function, change the call:

```python
return _do_render(
    ...
    settings=settings,
    output_root=output_root,
    workspace_base=workspace_base,   # ← delete this line
)
```

To:

```python
return _do_render(
    ...
    settings=settings,
    output_root=output_root,
)
```

#### 6c. Update the MCP instructions string

Change:

```python
"Controlled DOCX execution harness. Accepts an AI-generated "
"Node.js renderer script, executes it in a sandboxed workspace, "
"and returns a validated .docx file path."
```

To:

```python
"Controlled DOCX execution harness. Accepts an AI-generated "
"renderer script (Node.js or Python), executes it in a sandboxed "
"workspace, and returns a validated .docx file path."
```

#### 6d. Update the `render_docx` tool description

Change:

```python
"Runs an approved AI-generated DOCX renderer script and returns "
"a validated DOCX output file path. The renderer script must "
"write to process.env.OUTPUT_DOCX_PATH."
```

To:

```python
"Runs an approved AI-generated DOCX renderer script and returns "
"a validated DOCX output file path. The renderer script must "
"write to OUTPUT_DOCX_PATH (process.env in Node, os.environ in Python)."
```

#### 6e. Update the `renderer_type` parameter description

Change:

```python
description="Renderer runtime. Currently only 'node' is supported.",
```

To:

```python
description="Renderer runtime: 'node' or 'python'.",
```

#### 6f. Update the `entrypoint` parameter description

Change:

```python
description="Must be 'render-document.mjs' in v1",
```

To:

```python
description="Must be 'render-document.mjs' (node) or 'render_document.py' (python)",
```

#### 6g. Update `health_check` to report both runtimes

Replace the current `health_check` body:

```python
@mcp.tool()
def health_check() -> Dict[str, Any]:
    """Report runtime readiness for the renderer service."""
    ready, node_version, errors = check_node_runtime(settings)
    return {
        "status": "ready" if ready else "not_ready",
        "node_version": node_version,
        "allowed_document_types": list(settings.allowed_document_types),
        "allowed_renderer_types": list(settings.allowed_renderer_types),
        "output_root": settings.output_root,
        "runtime_root": settings.runtime_root,
        "errors": errors,
    }
```

With:

```python
@mcp.tool()
def health_check() -> Dict[str, Any]:
    """Report runtime readiness for the renderer service."""
    node_ready, node_version, node_errors = check_node_runtime(settings)
    py_ready, py_version, py_errors = check_python_runtime(settings)
    all_ready = node_ready and py_ready
    return {
        "status": "ready" if all_ready else "not_ready",
        "allowed_document_types": list(settings.allowed_document_types),
        "allowed_renderer_types": list(settings.allowed_renderer_types),
        "output_root": settings.output_root,
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
```

---

### Step 7 — Update `_do_render`

**Where:** The `_do_render` function (currently lines 949-1261).

#### 7a. Remove `workspace_base` parameter

Change the function signature from:

```python
def _do_render(
    *,
    ...
    settings: ServerSettings,
    output_root: Path,
    workspace_base: Path,
) -> Dict[str, Any]:
```

To:

```python
def _do_render(
    *,
    ...
    settings: ServerSettings,
    output_root: Path,
) -> Dict[str, Any]:
```

#### 7b. Replace the hardcoded entrypoint check

Replace:

```python
# 4. validate entrypoint (strict in v1)
if entrypoint != "render-document.mjs":
    return fail("invalid_request", [_render_error(
        "ENTRYPOINT_NOT_ALLOWED",
        f"Entrypoint must be 'render-document.mjs' in v1, got '{entrypoint}'",
    )])
```

With:

```python
# 4. validate entrypoint (strict per renderer_type)
expected_entrypoint = REQUIRED_ENTRYPOINTS.get(renderer_type)
if entrypoint != expected_entrypoint:
    return fail("invalid_request", [_render_error(
        "ENTRYPOINT_NOT_ALLOWED",
        f"Entrypoint for renderer_type='{renderer_type}' must be "
        f"'{expected_entrypoint}', got '{entrypoint}'",
    )])
```

This uses the `REQUIRED_ENTRYPOINTS` map from Step 1c. If someone passes `renderer_type="node"` with `entrypoint="render_document.py"`, the mismatch is caught here.

#### 7c. Branch the policy check by renderer_type

Replace:

```python
# 6. validate script safety
policy_errors = validate_node_script_policy(files)
if policy_errors:
    return fail("rejected_by_policy", policy_errors)
```

With:

```python
# 6. validate script safety (renderer-specific scanner)
if renderer_type == "node":
    policy_errors = validate_node_script_policy(files)
elif renderer_type == "python":
    policy_errors = validate_python_script_policy(files)
else:
    policy_errors = []
if policy_errors:
    return fail("rejected_by_policy", policy_errors)
```

#### 7d. Branch the runtime readiness check

Replace:

```python
# 7. confirm runtime readiness
ready, _node_ver, rt_errors = check_node_runtime(settings)
if not ready:
    return fail("runtime_not_ready", rt_errors)
```

With:

```python
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
```

#### 7e. Compute workspace_base from renderer_type

Replace:

```python
# 9. create workspace
ws_path = workspace_base / render_id
```

With:

```python
# 9. create workspace (under the renderer-specific runtime root)
if renderer_type == "node":
    rt_root = (PROJECT_ROOT / settings.node_runtime_root).resolve()
elif renderer_type == "python":
    rt_root = (PROJECT_ROOT / settings.python_runtime_root).resolve()
else:
    rt_root = (PROJECT_ROOT / "renderer-runtime").resolve()
workspace_base = rt_root / settings.workspace_root_name
ws_path = workspace_base / render_id
```

**Why this lives here instead of being precomputed:** we need `renderer_type` (validated earlier) to pick the right directory, and `render_id` (generated at step 8) for the leaf.

#### 7f. Branch the execution call

Replace:

```python
# 14. execute renderer
exec_result = run_node_renderer(
    workspace=ws_path,
    entrypoint=entrypoint,
    output_path=output_path,
    timeout_seconds=timeout,
    settings=settings,
    metadata_json_path=metadata_json_path,
)
```

With:

```python
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
```

This works because both runner functions have the same signature and return the same dict shape.

#### 7g. Make error codes runtime-generic

Replace:

```python
[_render_error(
    "NODE_EXECUTION_TIMEOUT",
    f"Renderer timed out after {timeout} seconds",
)]
```

With:

```python
[_render_error(
    "EXECUTION_TIMEOUT",
    f"Renderer timed out after {timeout} seconds",
)]
```

Replace:

```python
[_render_error(
    "NODE_EXECUTION_FAILED",
    f"Node renderer exited with code {exec_result['exit_code']}.",
    detail=stderr_tail,
)]
```

With:

```python
[_render_error(
    "EXECUTION_FAILED",
    f"Renderer ({renderer_type}) exited with code {exec_result['exit_code']}.",
    detail=stderr_tail,
)]
```

The error codes `NODE_EXECUTION_TIMEOUT` and `NODE_EXECUTION_FAILED` become `EXECUTION_TIMEOUT` and `EXECUTION_FAILED` because they now apply to both runtimes.

---

### Step 8 — Update CLI argument parsing

**Where:** The `main()` function, in the argument parser section (around current lines 1268-1360).

#### 8a. Replace `--runtime-root` with two separate flags

Remove:

```python
parser.add_argument(
    "--runtime-root",
    default=_s("MCP_RUNTIME_ROOT", cfg.get("runtime_root"), "node-renderer-runtime"),
)
```

Add in its place:

```python
parser.add_argument(
    "--node-runtime-root",
    default=_s("MCP_NODE_RUNTIME_ROOT", cfg.get("node_runtime_root"), "node-renderer-runtime"),
)
parser.add_argument(
    "--python-runtime-root",
    default=_s("MCP_PYTHON_RUNTIME_ROOT", cfg.get("python_runtime_root"), "python-renderer-runtime"),
)
```

#### 8b. Update the `ServerSettings` construction

In the `settings = ServerSettings(...)` call, replace:

```python
runtime_root=args.runtime_root,
```

With:

```python
node_runtime_root=args.node_runtime_root,
python_runtime_root=args.python_runtime_root,
```

---

### Step 9 — Update `docx-renderer-server.yaml`

Replace:

```yaml
defaults:
  output_root: generated-docx
  runtime_root: node-renderer-runtime
  timeout_seconds: 30
  max_timeout_seconds: 120
```

With:

```yaml
defaults:
  output_root: generated-docx
  node_runtime_root: node-renderer-runtime
  python_runtime_root: python-renderer-runtime
  timeout_seconds: 30
  max_timeout_seconds: 120
```

---

### Step 10 — Create the Python runtime directory

Create these directories (they can be empty; Git needs a `.gitkeep` or the `workspaces/` directory):

```
python-renderer-runtime/
  workspaces/
```

No `package.json`, no `requirements.txt`. The server uses the active Python environment's packages directly.

---

## Reference: complete data flow after all changes

```text
render_docx(renderer_type, entrypoint, files, ...)
  │
  ├─ validate document_type, renderer_type, entrypoint ← uses REQUIRED_ENTRYPOINTS map
  ├─ validate files (extensions, sizes)
  │
  ├─ if renderer_type == "node":
  │     validate_node_script_policy(files)   ← scans .mjs/.js only
  │     check_node_runtime(settings)         ← node --version, import docx
  │     workspace under: node-renderer-runtime/workspaces/<id>/
  │     run_node_renderer(...)               ← subprocess [node, render-document.mjs]
  │
  ├─ if renderer_type == "python":
  │     validate_python_script_policy(files)  ← scans .py only
  │     check_python_runtime(settings)        ← python --version, from docx import Document
  │     workspace under: python-renderer-runtime/workspaces/<id>/
  │     run_python_renderer(...)              ← subprocess [python, render_document.py]
  │
  ├─ validate_docx(output_path)              ← shared: ZIP, content types, rels, document.xml
  ├─ cleanup workspace
  └─ return structured result JSON           ← shared shape, same for both runtimes
```

---

## Testing checklist

After implementing, verify these scenarios. All can be run via pytest.

### Happy path — Node (existing)

Same as before. Should still pass unchanged.

### Happy path — Python

```python
render_docx(
    document_type="resume",
    renderer_type="python",
    filename_pattern="resume-{company}-{timestamp}",
    entrypoint="render_document.py",
    files=[{
        "path": "render_document.py",
        "content": (
            "import os\n"
            "from docx import Document\n"
            "\n"
            "output_path = os.environ['OUTPUT_DOCX_PATH']\n"
            "doc = Document()\n"
            "doc.add_heading('Jane Doe', level=0)\n"
            "doc.add_paragraph('Senior Backend Engineer')\n"
            "doc.save(output_path)\n"
        ),
    }],
    filename_values={"company": "Acme Inc", "role": "Senior Backend Engineer"},
)
```

Expected: `ok: true`, valid `.docx` at the output path.

### Entrypoint mismatch — node + .py

```python
render_docx(
    renderer_type="node",
    entrypoint="render_document.py",
    ...
)
```

Expected: `ENTRYPOINT_NOT_ALLOWED`, message says expected `render-document.mjs`.

### Entrypoint mismatch — python + .mjs

```python
render_docx(
    renderer_type="python",
    entrypoint="render-document.mjs",
    ...
)
```

Expected: `ENTRYPOINT_NOT_ALLOWED`, message says expected `render_document.py`.

### Python policy rejection — subprocess

```python
files=[{
    "path": "render_document.py",
    "content": "import subprocess\nimport os\nsubprocess.run(['ls'])\n",
}]
```

Expected: `SCRIPT_REJECTED_BY_POLICY`, detail mentions `import subprocess`.

### Python policy rejection — os.system

```python
files=[{
    "path": "render_document.py",
    "content": "import os\nos.system('rm -rf /')\n",
}]
```

Expected: `SCRIPT_REJECTED_BY_POLICY`, detail mentions `os.system(`.

### Python policy rejection — os.environ enumeration

```python
files=[{
    "path": "render_document.py",
    "content": "import os\nfor k, v in os.environ.items(): print(k, v)\n",
}]
```

Expected: `SCRIPT_REJECTED_BY_POLICY`, mentions bare `os.environ` enumeration.

### Python policy allows — OUTPUT_DOCX_PATH

```python
files=[{
    "path": "render_document.py",
    "content": (
        "import os\n"
        "from docx import Document\n"
        "path = os.environ['OUTPUT_DOCX_PATH']\n"
        "doc = Document()\n"
        "doc.add_paragraph('hello')\n"
        "doc.save(path)\n"
    ),
}]
```

Expected: passes policy check (no `SCRIPT_REJECTED_BY_POLICY`).

### Python package missing

If `python-docx` is not installed, `check_python_runtime` should return:

```json
{
  "code": "PYTHON_PACKAGE_MISSING",
  "message": "Python renderer requires python-docx.",
  "detail": "Install with: pip install python-docx. ..."
}
```

### Health check shows both runtimes

Call `health_check()`. Expected shape:

```json
{
  "status": "ready",
  "node": { "ready": true, "version": "v22.x.x", ... },
  "python": { "ready": true, "version": "Python 3.12.x", ... }
}
```

---

## Changes NOT required

- **DOCX validation:** no changes. `validate_docx` is already runtime-agnostic.
- **Result schema:** no changes. `_success_result` and `_fail_result` are already runtime-agnostic.
- **Filename builder:** no changes.
- **HTTP/FastAPI scaffolding:** no changes.
- **`node-renderer-runtime/`:** no changes to existing Node setup.

---

## Summary of new error codes

| Code | When |
|------|------|
| `PYTHON_NOT_FOUND` | `python_executable` not in PATH |
| `PYTHON_RUNTIME_NOT_READY` | `python-renderer-runtime/` missing or check failed |
| `PYTHON_PACKAGE_MISSING` | `from docx import Document` fails |
| `EXECUTION_TIMEOUT` | Replaces `NODE_EXECUTION_TIMEOUT` (generic) |
| `EXECUTION_FAILED` | Replaces `NODE_EXECUTION_FAILED` (generic) |

Existing Node-specific error codes (`NODE_NOT_FOUND`, `NODE_RUNTIME_NOT_READY`, `NODE_PACKAGE_MISSING`) remain unchanged inside `check_node_runtime`.
