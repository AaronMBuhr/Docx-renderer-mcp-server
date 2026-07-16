# Technical Spec: `docx-renderer-server.py` Revisions

## 1. Purpose

`DocxRendererMcpServer` is a controlled DOCX execution harness.

It does **not** write resumes, choose formatting, convert markdown, or infer document design. Those tasks happen before this server is called.

The server’s job is to:

1. Accept an AI-generated renderer script.
2. Stage it in a controlled temporary workspace.
3. Execute it with a known runtime.
4. Require it to write a `.docx` file to a provided output path.
5. Validate the output.
6. Return a structured result to the caller.

The expected pipeline is:

```text
Step 1: Resume Writer AI
  Input: master resume + job posting + preferences
  Output: resume.md

Step 2: DOCX Script Writer AI
  Input: resume.md + renderer contract
  Output: render-document.mjs

Step 3: DocxRendererMcpServer
  Input: render-document.mjs + filename metadata
  Output: final .docx
```

This spec covers Step 3 only.

---

## 2. High-Level Design

### 2.1 Current direction

The server should support this primary use case:

```text
AI-generated Node.js script
  -> uses approved `docx` package
  -> writes final document to process.env.OUTPUT_DOCX_PATH
  -> server returns path/result JSON
```

### 2.2 Initial runtime target

Version 1 should support:

```text
renderer_type: "node"
entrypoint: "render-document.mjs"
Node.js: 20+
External package: docx only
```

Python rendering may be reserved for later, but should not be required for the initial implementation.

---

## 3. Runtime Contract for AI-Generated Node Renderers

The Step 2 AI must generate a complete executable ES module named:

```text
render-document.mjs
```

The script must:

1. Use Node.js 20+.
2. Use ES modules.
3. Import from the approved package set only.
4. Write the finished DOCX to:

```javascript
process.env.OUTPUT_DOCX_PATH
```

5. Exit successfully only after the DOCX file has been written.
6. Not choose its own output filename.
7. Not require network access.
8. Not install packages.
9. Not call external programs.

### 3.1 Approved external packages

Initial allowlist:

```text
docx
```

### 3.2 Approved built-in modules

Initial allowlist:

```text
node:fs
node:path
node:crypto
node:url
```

The script may not import or require:

```text
child_process
worker_threads
cluster
vm
http
https
net
tls
dns
dgram
os
process as a module import
```

The script may access `process.env.OUTPUT_DOCX_PATH`.

### 3.3 Disallowed patterns

Reject scripts containing obvious dangerous or uncontrolled behavior, including:

```text
child_process
exec(
execFile(
spawn(
fork(
eval(
new Function(
import(
require(
http://
https://
fetch(
XMLHttpRequest
WebSocket
process.env other than OUTPUT_DOCX_PATH
process.exit
```

Note: `import { ... } from "docx"` is allowed. Dynamic `import(...)` should be disallowed.

---

## 4. Node Runtime Setup

The server should maintain a fixed Node renderer runtime directory, for example:

```text
node-renderer-runtime/
  package.json
  package-lock.json
  node_modules/
  workspaces/
```

The runtime’s `package.json` should be controlled by the project, not by the AI-generated script.

Example:

```json
{
  "name": "docx-renderer-runtime",
  "private": true,
  "type": "module",
  "dependencies": {
    "docx": "9.5.1"
  }
}
```

The server should not accept arbitrary submitted `package.json` files in Version 1.

### 4.1 Why workspaces should live under runtime root

Each render workspace should be created under:

```text
node-renderer-runtime/workspaces/<render_id>/
```

That allows Node module resolution to find:

```text
node-renderer-runtime/node_modules/docx
```

when the staged script does:

```javascript
import { Document, Packer } from "docx";
```

### 4.2 Runtime readiness check

On startup, or before first render, the server should verify:

```powershell
node --version
```

and verify the package is importable:

```powershell
node --input-type=module -e "import { Document, Packer } from 'docx'; console.log('docx OK')"
```

If this fails, the MCP tool should return a structured error telling the user to run:

```powershell
cd node-renderer-runtime
npm install
```

or, if a lockfile exists:

```powershell
npm ci
```

---

## 5. MCP Tool: `render_docx`

### 5.1 Tool purpose

`render_docx` executes an AI-generated renderer script and produces a `.docx`.

### 5.2 Input schema

The tool should accept:

```json
{
  "document_type": "resume",
  "renderer_type": "node",
  "filename_pattern": "resume-{company}-{timestamp}",
  "files": [
    {
      "path": "render-document.mjs",
      "content": "..."
    }
  ],
  "entrypoint": "render-document.mjs",
  "filename_values": {
    "company": "Acme Inc",
    "role": "Senior Backend Engineer"
  },
  "source_markdown": "... optional, for audit/debug only ...",
  "metadata": {
    "company": "Acme Inc",
    "role": "Senior Backend Engineer",
    "job_id": "optional",
    "generated_by": "optional"
  },
  "options": {
    "timeout_seconds": 30,
    "keep_workspace": false
  }
}
```

### 5.3 Required fields

Required:

```text
document_type
renderer_type
filename_pattern
files
entrypoint
filename_values
```

### 5.4 Optional fields

Optional:

```text
source_markdown
metadata
options.timeout_seconds
options.keep_workspace
```

`source_markdown` is not rendered by the server. It is only stored as an artifact for traceability/debugging if supplied.

### 5.5 `document_type`

Allowed values initially:

```text
resume
cover_letter
generic
```

Unknown values should be rejected.

### 5.6 `renderer_type`

Allowed values initially:

```text
node
```

Future reserved values:

```text
python
```

If `python` is passed before implementation, return a structured unsupported-renderer error.

### 5.7 `filename_pattern`

A safe filename template, for example:

```text
resume-{company}-{timestamp}
cover-letter-{company}-{timestamp}
```

Allowed placeholders should be limited to:

```text
company
role
document_type
timestamp
date
slug
```

The server should sanitize the final filename.

Rules:

1. Remove or replace path separators.
2. Remove invalid Windows filename characters:

```text
< > : " / \ | ? *
```

3. Collapse whitespace.
4. Limit final filename length.
5. Always append `.docx`.
6. Never allow caller-controlled directory traversal.

Example:

```json
{
  "filename_pattern": "resume-{company}-{timestamp}",
  "filename_values": {
    "company": "Acme, Inc."
  }
}
```

Output filename could become:

```text
resume-Acme-Inc-20260617-135500.docx
```

### 5.8 `files`

Each file object:

```json
{
  "path": "render-document.mjs",
  "content": "..."
}
```

Rules:

1. Paths must be relative.
2. No absolute paths.
3. No `..`.
4. No path traversal.
5. No writing outside the render workspace.
6. Maximum file count should be enforced.
7. Maximum total byte size should be enforced.
8. In v1, reject `package.json`, `package-lock.json`, `.npmrc`, shell scripts, executables, binaries, and nested `node_modules`.

Allowed extensions in v1:

```text
.mjs
.js
.json
.txt
.md
```

But the only required executable is:

```text
render-document.mjs
```

### 5.9 `entrypoint`

For v1, require:

```text
render-document.mjs
```

Later the server can support arbitrary safe relative entrypoints, but the initial version should be strict.

### 5.10 `metadata`

Metadata should be written to the workspace as:

```text
metadata.json
```

The renderer script may read `metadata.json` only if the prompt contract allows it.

For the current design, metadata is mostly for audit/debugging and result reporting.

### 5.11 `options`

Supported options:

```json
{
  "timeout_seconds": 30,
  "keep_workspace": false
}
```

Rules:

1. `timeout_seconds` must be capped by server maximum.
2. `keep_workspace` may only preserve failed workspaces or only be allowed in development mode.
3. Default timeout should be 30 seconds.
4. Hard maximum should be 120 seconds unless explicitly configured.

---

## 6. Server Settings

`ServerSettings` should include:

```python
@dataclass
class ServerSettings:
    output_root: str = "generated-docx"
    runtime_root: str = "node-renderer-runtime"
    workspace_root_name: str = "workspaces"
    node_executable: str = "node"
    default_timeout_seconds: int = 30
    max_timeout_seconds: int = 120
    keep_failed_workspaces: bool = True
    keep_successful_workspaces: bool = False
    max_file_count: int = 20
    max_total_file_bytes: int = 2_000_000
    max_single_file_bytes: int = 1_000_000
    allowed_document_types: tuple[str, ...] = ("resume", "cover_letter", "generic")
    allowed_renderer_types: tuple[str, ...] = ("node",)
```

Optional future setting:

```python
allow_python_renderer: bool = False
```

---

## 7. Execution Flow

### 7.1 Tool call flow

When `render_docx` is called:

1. Validate request schema.
2. Validate `document_type`.
3. Validate `renderer_type`.
4. Validate `entrypoint`.
5. Validate file paths and sizes.
6. Validate script safety using static checks.
7. Confirm runtime readiness.
8. Generate render ID.
9. Create workspace:

```text
node-renderer-runtime/workspaces/<render_id>/
```

10. Write submitted files into workspace.
11. Optionally write:

```text
source.md
metadata.json
```

12. Compute final output path under `output_root`.
13. Set environment variables.
14. Run Node without shell:

```python
subprocess.run(
    [node_executable, entrypoint],
    cwd=workspace_dir,
    env=restricted_env,
    capture_output=True,
    text=True,
    timeout=timeout_seconds,
    shell=False
)
```

15. Validate process exit code.
16. Validate the `.docx` file exists.
17. Validate the `.docx` is a ZIP.
18. Validate required DOCX internal parts exist.
19. Optionally collect warnings.
20. Clean or preserve workspace according to settings/options.
21. Return result JSON.

### 7.2 Environment variables passed to renderer

The renderer should receive a minimal environment.

Required:

```text
OUTPUT_DOCX_PATH=<absolute final output path>
```

Optional:

```text
DOCX_RENDER_WORKSPACE=<absolute workspace path>
DOCX_RENDER_METADATA_PATH=<absolute metadata.json path>
```

Avoid passing the full inherited user environment if possible.

At minimum, scrub sensitive variables such as:

```text
OPENAI_API_KEY
ANTHROPIC_API_KEY
GEMINI_API_KEY
GOOGLE_API_KEY
AWS_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY
GITHUB_TOKEN
NPM_TOKEN
```

Because Node needs `PATH` to run normally, a minimal `PATH` may need to be preserved.

---

## 8. Output Validation

A successful render requires:

1. Process exits with code `0`.
2. Output path exists.
3. Output file size is greater than a small threshold, for example 1 KB.
4. Output file extension is `.docx`.
5. Output file opens as ZIP.
6. ZIP contains at least:

```text
[Content_Types].xml
_rels/.rels
word/document.xml
```

7. `word/document.xml` is non-empty.
8. No obvious placeholder failure text is present unless expected.

Optional deeper validation:

1. Check for `word/styles.xml`.
2. Check for `docProps/core.xml`.
3. Check for paragraph count.
4. Check for no unreplaced template placeholders like:

```text
{{company}}
{{role}}
TODO
PLACEHOLDER
```

These optional checks should return warnings, not necessarily fail the render.

---

## 9. Result JSON Schema

The tool should always return a structured result object.

### 9.1 Success result

Example:

```json
{
  "ok": true,
  "status": "success",
  "document_type": "resume",
  "renderer_type": "node",
  "render_id": "20260617-135500-a1b2c3",
  "output": {
    "filename": "resume-Acme-Inc-20260617-135500.docx",
    "path": "E:/Source/Mine/DocxRendererMcpServer/generated-docx/resume-Acme-Inc-20260617-135500.docx",
    "relative_path": "generated-docx/resume-Acme-Inc-20260617-135500.docx",
    "size_bytes": 18234
  },
  "workspace": {
    "path": null,
    "kept": false
  },
  "execution": {
    "command": [
      "node",
      "render-document.mjs"
    ],
    "cwd": "E:/Source/Mine/DocxRendererMcpServer/node-renderer-runtime/workspaces/20260617-135500-a1b2c3",
    "exit_code": 0,
    "timeout_seconds": 30,
    "duration_ms": 842
  },
  "validation": {
    "is_zip": true,
    "has_content_types": true,
    "has_root_rels": true,
    "has_document_xml": true,
    "document_xml_bytes": 43892
  },
  "warnings": [],
  "errors": []
}
```

### 9.2 Failure result

Example:

```json
{
  "ok": false,
  "status": "execution_failed",
  "document_type": "resume",
  "renderer_type": "node",
  "render_id": "20260617-135500-a1b2c3",
  "output": null,
  "workspace": {
    "path": "E:/Source/Mine/DocxRendererMcpServer/node-renderer-runtime/workspaces/20260617-135500-a1b2c3",
    "kept": true
  },
  "execution": {
    "command": [
      "node",
      "render-document.mjs"
    ],
    "cwd": "E:/Source/Mine/DocxRendererMcpServer/node-renderer-runtime/workspaces/20260617-135500-a1b2c3",
    "exit_code": 1,
    "timeout_seconds": 30,
    "duration_ms": 215
  },
  "validation": null,
  "warnings": [],
  "errors": [
    {
      "code": "NODE_EXECUTION_FAILED",
      "message": "Node renderer exited with code 1.",
      "detail": "ReferenceError: Paragraph is not defined"
    }
  ],
  "stdout": "",
  "stderr": "ReferenceError: Paragraph is not defined\n..."
}
```

### 9.3 Error object schema

Each error:

```json
{
  "code": "STRING_CODE",
  "message": "Human-readable summary.",
  "detail": "Optional technical details."
}
```

Suggested error codes:

```text
INVALID_DOCUMENT_TYPE
UNSUPPORTED_RENDERER_TYPE
INVALID_FILENAME_PATTERN
INVALID_FILENAME_VALUE
INVALID_FILE_PATH
FILE_TOO_LARGE
TOO_MANY_FILES
ENTRYPOINT_NOT_ALLOWED
SCRIPT_REJECTED_BY_POLICY
NODE_NOT_FOUND
NODE_RUNTIME_NOT_READY
NODE_PACKAGE_MISSING
NODE_EXECUTION_TIMEOUT
NODE_EXECUTION_FAILED
OUTPUT_NOT_CREATED
OUTPUT_TOO_SMALL
OUTPUT_NOT_ZIP
OUTPUT_INVALID_DOCX
WORKSPACE_CREATE_FAILED
OUTPUT_WRITE_FAILED
INTERNAL_ERROR
```

---

## 10. Static Script Policy

Before executing the Node script, inspect the contents of `.mjs` and `.js` files.

### 10.1 Allow imports

Allow:

```javascript
import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import { Document, Packer, Paragraph } from "docx";
```

### 10.2 Reject imports

Reject:

```javascript
import child_process from "node:child_process";
import http from "node:http";
import https from "node:https";
import net from "node:net";
import dns from "node:dns";
import os from "node:os";
```

Reject CommonJS `require(...)` entirely in v1.

Reject dynamic imports:

```javascript
await import(...)
import(...)
```

### 10.3 Reject suspicious environment access

Allow:

```javascript
process.env.OUTPUT_DOCX_PATH
```

Reject or warn on:

```javascript
process.env.OPENAI_API_KEY
process.env.ANTHROPIC_API_KEY
process.env
```

Broad `process.env` enumeration should be rejected.

---

## 11. Filesystem Policy

The AI-generated script needs to write the final DOCX, but should not write arbitrary files elsewhere.

Best-effort controls:

1. Use a dedicated workspace.
2. Give the script only `OUTPUT_DOCX_PATH`.
3. Validate submitted paths.
4. Do not pass secrets.
5. Do not run with shell.
6. Use a timeout.
7. Reject obvious malicious code.

Important limitation:

This is **not a strong sandbox**. A Node script running as the current user can still potentially access local files if it uses allowed filesystem APIs. For stronger security, later run the renderer in:

```text
Docker
restricted Windows user
job object / process isolation
VM
```

For current local development, treat AI-generated scripts as semi-trusted but still validate them.

---

## 12. Directory Layout

Recommended project layout:

```text
DocxRendererMcpServer/
  docx-renderer-server.py
  generated-docx/
  node-renderer-runtime/
    package.json
    package-lock.json
    node_modules/
    workspaces/
  tests/
    test_render_docx_node_success.py
    test_render_docx_policy.py
    test_render_docx_validation.py
```

Generated output:

```text
generated-docx/
  resume-Acme-Inc-20260617-135500.docx
```

Temporary workspaces:

```text
node-renderer-runtime/workspaces/
  20260617-135500-a1b2c3/
    render-document.mjs
    metadata.json
```

Successful workspaces may be deleted by default.

Failed workspaces should be preserved during development.

---

## 13. MCP Tool API Details

The public MCP tool should be named:

```text
render_docx
```

Tool description:

```text
Runs an approved AI-generated DOCX renderer script and returns a validated DOCX output file path. The renderer script must write to process.env.OUTPUT_DOCX_PATH.
```

Input fields:

```python
document_type: str
renderer_type: str
filename_pattern: str
files: list[dict[str, str]]
entrypoint: str
filename_values: dict[str, str]
source_markdown: str | None = None
metadata: dict[str, Any] | None = None
options: dict[str, Any] | None = None
```

Return type:

```python
dict[str, Any]
```

The tool should not throw raw exceptions to the caller for expected failures. It should return structured failure JSON.

Unexpected internal exceptions should be caught and converted to:

```json
{
  "ok": false,
  "status": "internal_error",
  "errors": [
    {
      "code": "INTERNAL_ERROR",
      "message": "...",
      "detail": "..."
    }
  ]
}
```

---

## 14. Required Implementation Changes

### 14.1 Add request validation

Implement validation for:

```text
document_type
renderer_type
filename_pattern
filename_values
files
entrypoint
options
```

### 14.2 Add safe filename builder

Create function:

```python
def build_output_filename(pattern: str, values: dict[str, str], document_type: str) -> str:
    ...
```

Requirements:

1. Insert timestamp automatically.
2. Slugify values.
3. Remove invalid filename characters.
4. Append `.docx`.
5. Prevent empty filename.
6. Enforce max length.

### 14.3 Add workspace manager

Create:

```python
def create_workspace(render_id: str) -> Path:
    ...
```

Create workspace under:

```text
node-renderer-runtime/workspaces/<render_id>
```

### 14.4 Add file staging

Create:

```python
def stage_files(workspace: Path, files: list[RendererFile]) -> list[Path]:
    ...
```

### 14.5 Add static script policy

Create:

```python
def validate_node_script_policy(files: list[RendererFile]) -> list[Error]:
    ...
```

This function should reject dangerous imports and patterns before execution.

### 14.6 Add runtime readiness check

Create:

```python
def check_node_runtime(settings: ServerSettings) -> RuntimeCheckResult:
    ...
```

Check:

1. Node executable exists.
2. Node version is acceptable.
3. `docx` can be imported from runtime context.

### 14.7 Add Node execution function

Create:

```python
def run_node_renderer(
    workspace: Path,
    entrypoint: str,
    output_path: Path,
    timeout_seconds: int,
    settings: ServerSettings,
) -> ExecutionResult:
    ...
```

Must use:

```python
shell=False
```

Must capture:

```text
stdout
stderr
exit_code
duration_ms
timeout
```

### 14.8 Add DOCX validation

Create:

```python
def validate_docx(path: Path) -> DocxValidationResult:
    ...
```

Checks:

```text
exists
size
zip validity
[Content_Types].xml
_rels/.rels
word/document.xml
```

### 14.9 Add cleanup behavior

Rules:

```text
success + keep_successful_workspaces false -> delete workspace
failure + keep_failed_workspaces true -> keep workspace
options.keep_workspace true -> keep workspace if development mode allows it
```

### 14.10 Add structured result builder

Create one success and one failure builder so every return has consistent shape.

---

## 15. Testing Requirements

### 15.1 Success test

A minimal AI-generated Node script should:

1. Import `docx`.
2. Create a document with one paragraph.
3. Write to `OUTPUT_DOCX_PATH`.
4. Return success.
5. Produce valid `.docx`.

### 15.2 Filename test

Input:

```json
{
  "company": "Acme, Inc.",
  "role": "Senior Backend Engineer"
}
```

Should produce safe filename with no comma, slash, colon, etc.

### 15.3 Missing output test

Script exits `0` but does not create output.

Expected:

```text
ok: false
status: output_not_created
error code: OUTPUT_NOT_CREATED
```

### 15.4 Runtime package missing test

If `docx` is not importable, return:

```text
NODE_PACKAGE_MISSING
```

with install instructions.

### 15.5 Dangerous script tests

Reject scripts containing:

```javascript
import { exec } from "node:child_process";
await import("node:child_process");
const cp = require("child_process");
eval("...");
fetch("https://example.com");
```

Expected:

```text
ok: false
status: rejected_by_policy
error code: SCRIPT_REJECTED_BY_POLICY
```

### 15.6 Timeout test

A script that never exits should return:

```text
NODE_EXECUTION_TIMEOUT
```

### 15.7 Invalid DOCX test

A script writes plain text to `.docx`.

Expected:

```text
ok: false
status: output_invalid_docx
error code: OUTPUT_NOT_ZIP or OUTPUT_INVALID_DOCX
```

---

## 16. Example Valid Renderer Script

```javascript
import fs from "node:fs";
import {
  Document,
  Packer,
  Paragraph,
  TextRun,
  HeadingLevel,
  AlignmentType,
} from "docx";

const outputPath = process.env.OUTPUT_DOCX_PATH;

if (!outputPath) {
  throw new Error("OUTPUT_DOCX_PATH is required");
}

const doc = new Document({
  sections: [
    {
      properties: {
        page: {
          margin: {
            top: 720,
            right: 720,
            bottom: 720,
            left: 720,
          },
        },
      },
      children: [
        new Paragraph({
          alignment: AlignmentType.CENTER,
          children: [
            new TextRun({
              text: "Jane Doe",
              bold: true,
              size: 32,
            }),
          ],
        }),
        new Paragraph({
          alignment: AlignmentType.CENTER,
          children: [
            new TextRun({
              text: "Senior Backend Engineer",
              size: 22,
            }),
          ],
        }),
        new Paragraph({
          text: "Professional Experience",
          heading: HeadingLevel.HEADING_1,
          spacing: { before: 240, after: 120 },
        }),
        new Paragraph({
          children: [
            new TextRun({
              text: "Built production backend systems using Python, C#, Docker, and AWS.",
            }),
          ],
        }),
      ],
    },
  ],
});

const buffer = await Packer.toBuffer(doc);
fs.writeFileSync(outputPath, buffer);
```

---

## 17. Example MCP Call

```python
result = render_docx(
    document_type="resume",
    renderer_type="node",
    filename_pattern="resume-{company}-{timestamp}",
    files=[
        {
            "path": "render-document.mjs",
            "content": ai_generated_script,
        }
    ],
    entrypoint="render-document.mjs",
    filename_values={
        "company": "Acme Inc",
        "role": "Senior Backend Engineer",
    },
    metadata={
        "company": "Acme Inc",
        "role": "Senior Backend Engineer",
    },
    options={
        "timeout_seconds": 30,
        "keep_workspace": False,
    },
)
```

---

## 18. Acceptance Criteria

The revision is complete when:

1. `render_docx` accepts an AI-generated `render-document.mjs`.
2. The server runs it with Node from a controlled workspace.
3. The renderer can import the approved `docx` package.
4. The script writes to `OUTPUT_DOCX_PATH`.
5. The server returns a valid `.docx` path on success.
6. The server returns structured JSON on failure.
7. Dangerous scripts are rejected before execution.
8. Missing Node/package setup produces a clear actionable error.
9. Invalid DOCX output is detected.
10. Failed workspaces are preserved for debugging.
11. Successful temporary workspaces are cleaned up by default.
12. The test suite covers success, policy rejection, timeout, invalid output, and filename sanitization.

---

## 19. Non-Goals

This revision does not need to:

1. Convert markdown to DOCX.
2. Generate layout decisions.
3. Call an AI provider.
4. Install arbitrary npm packages per render.
5. Support browser rendering.
6. Support LibreOffice.
7. Support arbitrary templates.
8. Provide strong OS-level sandboxing.
9. Support Python renderers in v1.

Those can be future features.

---

## 20. Summary

`docx-renderer-server.py` should become a predictable, controlled execution harness for AI-generated DOCX renderer scripts.

Its core contract is:

```text
Input:
  render-document.mjs
  filename metadata
  renderer_type="node"

Execution:
  run Node in controlled workspace
  allow approved docx package
  provide OUTPUT_DOCX_PATH

Output:
  validated .docx file
  structured result JSON
```

The AI still controls the final document formatting by writing the render script. The server controls execution, validation, output naming, cleanup, and error reporting.
