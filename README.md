# DOCX Renderer MCP Server

A controlled DOCX execution harness, exposed over MCP.

It accepts a **docx document program** — a complete, self-contained program that produces exactly one `.docx` — validates it against a static policy, executes it in an isolated workspace, and returns the path to the created file.

The server does not write documents. It runs a program that does, and it refuses to run one that breaks policy.

## What a docx document program is

A generated program whose execution emits a single `.docx`. Content and formatting are **fused into it** — the document's text lives in the program as string literals, alongside the calls that format it:

```js
import { Document, Packer, Paragraph, TextRun } from 'docx';
import { writeFile } from 'node:fs/promises';

const doc = new Document({ sections: [{ children: [
  new Paragraph({ children: [new TextRun({ text: 'Jane Doe', bold: true, size: 24 })] }),
]}]});

const buffer = await Packer.toBuffer(doc);
await writeFile(process.env.OUTPUT_DOCX_PATH, buffer);
```

This is deliberate. A resume where any character may carry its own formatting cannot be expressed as a template plus data, so the document is expressed as code instead — the same reasoning behind PostScript, where a page is described by a program rather than filled into a form.

Two consequences worth understanding:

- **It is single-use.** Each program renders one specific document and is then discarded. It is not a reusable renderer that takes input.
- **It is format-bound.** The program is written against the `docx` object model in that library's own units (half-points, twips). Producing a PDF would require a different program class, not a retarget.

The program's only interface to the outside world is the `OUTPUT_DOCX_PATH` environment variable. It takes no arguments and reads no config.

## Requirements

- **Python 3.11+** for the server itself
- **Node.js ≥ 20** for the `node` runtime (the server rejects older versions)
- **`python-docx`** for the `python` runtime, if enabled

## Setup

```bash
# Node runtime — installs the pinned `docx` package
cd node-renderer-runtime && npm ci

# Python runtime (only if you enable renderer_type: python)
pip install python-docx
```

Then check the server agrees:

```
health_check
```

It probes each runtime for real — `import { Document, Packer } from 'docx'` for Node, `from docx import Document` for Python — and reports `ready_renderer_types`. A missing package surfaces as `NODE_PACKAGE_MISSING` / `PYTHON_RUNTIME_NOT_READY`, not as a render failure later.

## Configuration

Copy the example and edit it — the live config is gitignored, since its paths are machine-specific:

```bash
cp docx-renderer-server.yaml.example docx-renderer-server.yaml
```

`docx-renderer-server.yaml`:

```yaml
defaults:
  output_root: generated-docx
  node_runtime_root: node-renderer-runtime
  python_runtime_root: python-renderer-runtime
  allowed_renderer_types:
    - node
  timeout_seconds: 30
  max_timeout_seconds: 120

# Each alias names exactly one directory.
directory_aliases:
  job_applications: C:\path\to\your\documents
```

`directory_aliases` enables **server-side file transport**: a `render_docx` file entry may carry `source_path: "alias/relative/path"` instead of inline `content`, and the server reads that file itself. Resolution is fail-closed — an unknown alias, a `..` segment, a path escaping the alias's directory, an oversize file, or a missing file is rejected before anything executes. Absent or empty config disables the feature entirely.

## The `render_docx` tool

| parameter | meaning |
| --- | --- |
| `document_type` | `resume`, `cover_letter`, or `generic` |
| `renderer_type` | the runtime that executes the program: `node` or `python` |
| `entrypoint` | `render-document.mjs` (node) or `render_document.py` (python) |
| `files` | each entry is `{path, content}` **or** `{path, source_path}` — never both |
| `filename_pattern` | placeholders: `{document_type} {company} {role} {timestamp} {date} {slug}` |
| `filename_values` | values substituted into the pattern |
| `source_markdown` | optional, for audit only — never rendered |
| `metadata` | optional (company, role, job_id, …) |
| `options` | optional `{timeout_seconds, keep_workspace}` |

`renderer_type` names the runtime, not the program. The same document, in the same concepts, can be expressed for either.

## How execution works

1. The submitted files are written into a fresh workspace under `<runtime_root>/workspaces/<render_id>/`.
2. Static policy runs against the source. **Nothing has executed yet.**
3. The program runs via `subprocess.run([node, entrypoint], cwd=workspace, shell=False)` with `OUTPUT_DOCX_PATH` injected.
4. The created `.docx` is validated and moved to `output_root`.

Module resolution is the trick worth knowing: workspaces live *inside* the runtime root, so Node walks up from `workspaces/<id>/` and finds `node-renderer-runtime/node_modules/docx`. The program gets exactly one library, by virtue of where its file sits. The parent `package.json`'s `"type": "module"` is what makes `.mjs` load as ESM.

Nothing is bundled or compiled. `docx` and its dependencies (`jszip`, `xml`, `xml-js`) are pure JavaScript with no native code, so the entire path from objects to valid OOXML bytes runs in-process. No Word, no Office, no system libraries.

## Security model

Policy is enforced by **static analysis before execution**, plus workspace placement. A violation returns `SCRIPT_REJECTED_BY_POLICY` and nothing runs.

**Blocked Node imports:** `child_process`, `worker_threads`, `cluster`, `vm`, `http`, `https`, `net`, `tls`, `dns`, `dgram`, `os`
**Blocked Python imports:** `subprocess`, `socket`, `requests`, `urllib`, `http`, `shutil`, `multiprocessing`, `ctypes`
**Blocked patterns:** `eval(`, `exec(`, `spawn(`, `fork(`, `new Function(`, dynamic `import(`, `fetch(`, `XMLHttpRequest`, `WebSocket`, `process.exit`, and **any `http://` or `https://` literal**
**Also rejected:** `require()` (ESM only), `node_modules` in any submitted file path
**Environment:** only `OUTPUT_DOCX_PATH`, `DOCX_RENDER_WORKSPACE`, `DOCX_RENDER_METADATA_PATH` are readable; known secret-bearing variables are stripped from the child environment

Two things to be clear-eyed about:

- **The URL rule is a string match, not a semantic one.** A URL in a comment, or a hyperlink hardcoded into contact details, rejects the program even though nothing fetches anything. Visible links must arrive as plain text.
- **The security boundary is this server, not the runtime.** A docx document program executed by hand is ordinary Node or Python code with your full privileges. Running one directly is fine for debugging your own output; it is not a sandbox.

`process.exit` is banned, which is why a program should let errors throw naturally rather than catching and exiting — the harness needs the real failure.

## Workspaces

Failed renders are kept (`keep_failed_workspaces: true`), successful ones are deleted (`keep_successful_workspaces: false`). A populated `workspaces/` directory is therefore a record of failures worth reading — and one nothing prunes automatically. It is gitignored: each workspace holds a document program with its full content embedded.

## Where the program comes from

Generation is out of scope for this server — it accepts a program from any source. In the intended pipeline, an AI MCP server produces one under the `docx_document_program_v1` response contract, which returns:

```json
{
  "renderer_type": "node",
  "source_filename": "render-document.mjs",
  "source_code": "<the complete docx document program>"
}
```

The JSON is only an envelope for shipping the program through a model intact. It is unwrapped before it reaches `render_docx`, whose `files` entry carries the program as plain text. Hash `source_code` before submitting and compare against the generator's `source_code_sha256` to prove the executed bytes are the generated bytes, unedited.

## Further reading

- [`README-tech-spec.md`](README-tech-spec.md) — the full technical specification
- [`README-tech-spec-addition.md`](README-tech-spec-addition.md) — dual-runtime spec notes
- [`README-implementation-plan-additional.md`](README-implementation-plan-additional.md) — Python renderer support plan
