# Security Controls: DOCX Renderer MCP Server and AI MCP Server

What each control checks, and where it stops. Written against the code as of
2026-09-23. Both servers are personal tools that run one person's document
pipeline on their own machine.

---

## DOCX Renderer MCP Server

**What it is.** A personal MCP server that runs a generated "docx document
program": a single-use Node (or Python) script with the document text embedded
in it, which writes exactly one `.docx`. The server doesn't write documents
itself. It checks the program, runs it in a scratch directory and validates
what comes out. Only the Node runtime is enabled in the live config
(`allowed_renderer_types: [node]`).

### 1. Static-policy checks before any generated code runs

**What it checks.** Before anything runs, every `.mjs`/`.js` file goes through
regex scans (`validate_node_script_policy`):

- **Static `import … from '<module>'`** against a blocklist: `child_process`,
  `worker_threads`, `cluster`, `vm`, `http`, `https`, `net`, `tls`, `dns`,
  `dgram`, `os`, with and without the `node:` prefix. Importing a module from
  an `http(s)://` URL is also rejected.
- **CommonJS `require('…')`** is rejected outright, so programs must be ESM.
- **Call patterns:** bare `exec(`, `execFile(`, `spawn(`, `fork(`, `eval(`,
  `import(`, `fetch(`; `eval(` and `fetch(` reached through `globalThis.`,
  `global.`, `window.` or `self.`; `new Function(`, `new XMLHttpRequest(`,
  `new WebSocket(`; `process.exit`. Method calls such as `regex.exec(text)` are
  allowed, and so are technology names inside string literals (resume text).
- **`process.env.X`** is allowed only for `OUTPUT_DOCX_PATH`,
  `DOCX_RENDER_WORKSPACE` and `DOCX_RENDER_METADATA_PATH`.
- **docx import check:** before the scan, every symbol imported from `docx` is
  compared with the real export list of the installed package. A small curated
  table repairs known hallucinated names (`TabStopLeader`→`LeaderType`). Any
  other unknown name is rejected with a "did you mean" hint.

Any violation returns `SCRIPT_REJECTED_BY_POLICY` and nothing runs.

**Limits.**

- **It matches text, not meaning.** It never parses the program, so it produces
  both false positives and false negatives.
- **False positives:** earlier rules rejected a resume that mentioned
  "WebSocket", and any URL in the contact details. The patterns were narrowed
  to match uses, not mentions.
- **False negatives:** all of these pass the checker:
  - `createRequire` from `node:module`, then `r('child_process')`, which
    bypasses both the import blocklist and the `require` ban
  - `Function('…')()` without `new`
  - `const f = fetch; f(url)`, or `globalThis['fetch'](url)`
  - `process['env']`, which skips the env-var rule
  - `export { execSync } from 'node:child_process'` in one file, imported
    locally from another
  - `execSync` and `spawnSync`, which aren't in the pattern list at all
- **`node:fs` isn't blocked, and can't be,** because the program needs it to
  write its output. So a program can read or write any file the user account
  can reach.
- **Python** (disabled in the live config) is weaker still.
  `import os, subprocess` only checks `os`, and
  `importlib.import_module('subprocess')` and `getattr(os, 'system')` both pass.

**Accurate framing:** it catches a well-behaved generator that reaches for
obvious dangerous APIs, and it catches model mistakes such as invented imports.
It does not stop an adversarial program, and it doesn't make arbitrary
generated code safe.

### 2. Per-render workspaces

**What it does.**

- Each render gets a fresh directory,
  `<runtime_root>/workspaces/<timestamp>-<random hex>/`.
- Files are written only after a check that each resolved path stays inside
  that directory.
- The program runs with
  `subprocess.run([node, entrypoint], cwd=workspace, shell=False)`, with a
  timeout (default 30s, maximum 120s), and stdout/stderr are captured.
- Placing the workspace inside the runtime root means Node finds exactly one
  pinned library (`docx`) through normal module resolution.
- Afterwards the output must exist, be at least 1 KB, be a ZIP, and contain
  `[Content_Types].xml`, `_rels/.rels` and `word/document.xml`.
- Successful workspaces are deleted. Failed ones are kept for debugging.

**Limits.** This is a separate working directory, not a sandbox. There is no
container, no separate OS user, no Node `--permission` flag and no network
isolation. The process runs with the user's full privileges. Nothing forces the
program to write only to `OUTPUT_DOCX_PATH`; the server just checks afterwards
that a valid file appeared there. The README says so itself: "The security
boundary is this server, not the runtime." The timeout limits runtime but not
memory or disk use. Kept failed workspaces hold full resume text and are never
cleaned up automatically.

### 3. Secret-stripped subprocess environment

**What it does.** The child process gets a copy of the server's environment with
8 named variables removed: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
`GEMINI_API_KEY`, `GOOGLE_API_KEY`, `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, `GITHUB_TOKEN`, `NPM_TOKEN`. The comparison ignores
case. The three render variables are then added.

**Limits.** It removes only a fixed list of known names. Everything else is
passed through, including PATH, profile directories and any other token such as
Azure, Hugging Face or a custom `*_KEY`. The rule that only three variables are
readable is a check on the program's text, and `process['env']` gets past it.
The accurate description is "known provider keys are removed from the child's
environment", not "the child gets a clean environment".

### 4. Fail-closed file-path handling

**What it checks (the server's own file operations).**

- **Submitted file paths:** no absolute paths, drive letters or `..`. Only
  `.mjs .js .json .txt .md .py` are allowed. `package.json`,
  `package-lock.json`, `.npmrc` and any `node_modules` path segment are
  rejected. There are limits of 20 files, 1 MB per file and 2 MB in total. The
  entrypoint must be the exact expected filename, and it must be among the
  submitted files.
- **Server-side file loading (`source_path: "alias/rel/path"`):** this is off
  unless aliases are configured. The alias must be known, and `..` is rejected.
  The path is fully resolved (which follows symlinks), and a case-normalized
  check confirms it's still inside the alias's directory. It must be a regular
  file within the size limit that decodes as UTF-8. Supplying both `content`
  and `source_path` is rejected as ambiguous.
- **JSON envelope vs. script:** if the file extension and the content disagree
  (a `.json` file holding a script, or a `.mjs` file holding a JSON envelope),
  the render is refused with `DOCX_PROGRAM_TYPE_MISMATCH` rather than guessed
  at.
- **Output filename:** built only from allowed placeholders, with each value
  turned into a slug and the result restricted to `[a-zA-Z0-9._-]`, so it can't
  point outside the output folder.

**Limits.** "Fail-closed" applies to what the server itself reads and writes. It
doesn't restrict what the executed program opens (see §1). There's also one
deliberate fail-open case: if the probe that lists the `docx` exports fails,
the import-existence check is skipped instead of blocking the render. The
known-name repairs still run.

---

## AI MCP Server (the upstream half)

A general-purpose server that fills in Markdown prompt templates and sends them
to OpenAI, Gemini or Anthropic at a low, middle or high tier. It produces the
document program that the renderer runs.

### Prompt contracts

**What they check.** A `*.contract.yaml` file next to a template pair declares
each placeholder as `scalar` or `document`, with `allow_empty` and
`min_characters`. At startup, a malformed contract or two contracts for the
same template pair stops the server. On each call:

- the contract's placeholders must match the template's placeholders exactly
- missing or extra keys are rejected
- `document` fields must be the actual text, not a reference to it. The whole
  value can't be a path, UNC path, bare filename, `READ_FROM_FILE:` style
  instruction, or a phrase like "see attached"; a path mentioned inside real
  document text is fine.
- documents must meet their minimum length

**Limits.** `require_prompt_contracts` defaults to **False**, so a template pair
without a contract gets no checks. The repo ships only two example contracts;
the personal prompt set lives elsewhere. These checks are about getting the
content to the model intact (they came out of a real failure where
`READ_FROM_FILE:<path>` was sent as if it were the resume). They aren't a
security control.

### Response contract (`docx_document_program_v1`)

**What it checks.** The model's reply must be exactly one JSON object with
non-empty `renderer_type`, `source_filename` and `source_code`, and no extra
fields in strict mode. It tolerates one wrapping code fence and at most 8
characters of stray closing punctuation, and it reports that as a warning.
Anything else outside the object is rejected. It returns `source_code_sha256`
so the caller can prove the bytes that ran are the bytes the model produced.

**Limits.**

- This validates shape only. It doesn't check that the code is correct or safe;
  that's the renderer's job.
- The renderer never checks the hash, so the proof holds only if the calling
  workflow compares the hashes.
- The renderer's own envelope extraction accepts any amount of trailing content
  (with a warning), which is looser than the AI server's 8-character rule.

### Audit logging

**What it records.** One JSONL line per `query_ai` / `query_ai_batch` call, in a
daily file:

- request and batch IDs
- the requested provider and tier, and the model actually used
- template names
- SHA-256 hashes of the raw templates and the filled-in prompts
- each replacement field's character count and hash
- the full response's character count and hash, plus a flag when the inline
  copy was truncated
- status, failure type, whether a provider was actually called, and duration

It never stores content. Top-level keys whose names contain `api_key`,
`authorization`, `token` or `secret` are redacted, and files older than 30 days
are deleted.

**Limits.**

- **Not tamper-evident:** these are plain local files with no signing or hash
  chain.
- **Never blocks requests:** a failed write is only logged as a warning.
- **Redaction:** it works on key names and only at the top level.
- **Reproducibility, not replay:** the hashes let you confirm that a specific
  template version and input produced a call. You can't replay a call from the
  log.
- **The renderer has no audit log of its own.** It only has stderr logging, its
  structured result, and the kept failed workspaces.

---

## What this is, and what it isn't

These are personal tools that run one person's document pipeline on their own
machine. The design uses layered guardrails for a cooperative generator: typed
input contracts, a strict output envelope, static checks before execution, a
scratch workspace with a timeout and output validation, and hash-based records
for reproducibility. That's a reasonable set of engineering controls for its
purpose. It is not a sandbox, it doesn't make arbitrary generated code safe,
and it isn't enterprise AI governance: there's no access control, policy
administration, tamper-evident audit, multi-user separation or independent
review.
