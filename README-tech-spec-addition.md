The spec I gave was mostly Node-first:

renderer_type: "node"
entrypoint: "render-document.mjs"
runtime: Node.js + docx package

It mentioned Python as a future reserved option, but it did not fully specify what to do with a .py renderer. So yes: we need to extend the spec so docx-renderer-server.py has two explicit execution paths:

.mjs → run with Node
.py  → run with Python
Revised core rule

The renderer server should accept either:

renderer_type = "node"
entrypoint = "render-document.mjs"

or:

renderer_type = "python"
entrypoint = "render_document.py"

Both must obey the same output contract:

The script must write the final .docx to the path in OUTPUT_DOCX_PATH.

So from the server’s point of view, Node and Python are just two different ways to satisfy the same contract.

What differs between .mjs and .py
Node renderer
{
  "renderer_type": "node",
  "entrypoint": "render-document.mjs",
  "files": [
    {
      "path": "render-document.mjs",
      "content": "import fs from 'node:fs'; import { Document, Packer } from 'docx'; ..."
    }
  ]
}

Server runs:

subprocess.run(
    ["node", "render-document.mjs"],
    cwd=workspace_dir,
    env=restricted_env,
    shell=False
)

Expected script behavior:

const outputPath = process.env.OUTPUT_DOCX_PATH;
// create docx
// write outputPath

Approved Node dependencies, initially:

docx
Python renderer
{
  "renderer_type": "python",
  "entrypoint": "render_document.py",
  "files": [
    {
      "path": "render_document.py",
      "content": "from docx import Document\nimport os\n..."
    }
  ]
}

Server runs:

subprocess.run(
    [python_executable, "render_document.py"],
    cwd=workspace_dir,
    env=restricted_env,
    shell=False
)

Expected script behavior:

import os

output_path = os.environ["OUTPUT_DOCX_PATH"]
# create docx
# save output_path

Approved Python dependencies, initially:

python-docx

Maybe later:

lxml
Pillow

But I would start with only python-docx.

Revised input schema

The render_docx tool should accept:

{
  "document_type": "resume",
  "renderer_type": "node",
  "filename_pattern": "resume-{company}-{timestamp}",
  "entrypoint": "render-document.mjs",
  "files": [
    {
      "path": "render-document.mjs",
      "content": "..."
    }
  ],
  "filename_values": {
    "company": "Acme Inc",
    "role": "Senior Backend Engineer"
  },
  "source_markdown": "... optional audit/debug copy ...",
  "metadata": {
    "company": "Acme Inc",
    "role": "Senior Backend Engineer"
  },
  "options": {
    "timeout_seconds": 30,
    "keep_workspace": false
  }
}

or:

{
  "document_type": "resume",
  "renderer_type": "python",
  "filename_pattern": "resume-{company}-{timestamp}",
  "entrypoint": "render_document.py",
  "files": [
    {
      "path": "render_document.py",
      "content": "..."
    }
  ],
  "filename_values": {
    "company": "Acme Inc",
    "role": "Senior Backend Engineer"
  }
}
Extension-specific validation

The server should enforce this matrix:

renderer_type	Allowed entrypoint	Runtime	Primary package
node	render-document.mjs	Node.js	docx
python	render_document.py	Python	python-docx

It should reject mismatches:

renderer_type=node + render_document.py     → reject
renderer_type=python + render-document.mjs  → reject
renderer_type=node + arbitrary-script.js    → reject in v1
renderer_type=python + arbitrary.py         → reject in v1
Server settings should be expanded

The spec should include:

@dataclass
class ServerSettings:
    output_root: str = "generated-docx"

    # Node runtime
    node_runtime_root: str = "node-renderer-runtime"
    node_executable: str = "node"
    allowed_node_packages: tuple[str, ...] = ("docx",)

    # Python runtime
    python_runtime_root: str = "python-renderer-runtime"
    python_executable: str = sys.executable
    allowed_python_packages: tuple[str, ...] = ("docx",)  # import name for python-docx

    # Shared
    default_timeout_seconds: int = 30
    max_timeout_seconds: int = 120
    keep_failed_workspaces: bool = True
    keep_successful_workspaces: bool = False
    allowed_renderer_types: tuple[str, ...] = ("node", "python")

Note: the PyPI package is called python-docx, but the import is:

from docx import Document
Runtime readiness checks

The server needs two readiness checks.

Node readiness
node --version
node --input-type=module -e "import { Document, Packer } from 'docx'; console.log('docx OK')"
Python readiness
python --version
python -c "from docx import Document; print('python-docx OK')"

If Python support is enabled but python-docx is missing, return:

{
  "ok": false,
  "status": "runtime_not_ready",
  "errors": [
    {
      "code": "PYTHON_PACKAGE_MISSING",
      "message": "Python renderer requires python-docx.",
      "detail": "Install with: pip install python-docx"
    }
  ]
}
Static policy should be separate

Do not use the same safety scanner for .mjs and .py.

Use:

validate_node_script_policy(...)
validate_python_script_policy(...)
Reject in Node scripts
child_process
eval
Function
dynamic import(...)
fetch
http/https/net/tls/dns
process.env except OUTPUT_DOCX_PATH
Reject in Python scripts
import subprocess
import socket
import requests
import urllib
import http.client
import shutil.rmtree
os.system
eval
exec
open absolute paths unless OUTPUT_DOCX_PATH
os.environ except OUTPUT_DOCX_PATH

Python is trickier because python-docx scripts often need normal file operations only to save the output. So initially the policy should be conservative.

Output validation remains shared

After either script runs, validation is identical:

Does OUTPUT_DOCX_PATH exist?
Is it a ZIP?
Does it contain [Content_Types].xml?
Does it contain _rels/.rels?
Does it contain word/document.xml?
Is word/document.xml non-empty?

So the architecture becomes:

render_docx()
  validate request
  create workspace
  stage files
  build output path

  if renderer_type == "node":
      check node runtime
      validate .mjs policy
      run node

  if renderer_type == "python":
      check python runtime
      validate .py policy
      run python

  validate produced .docx
  return structured result
Bottom line

My previous technical spec fully covered .mjs as v1 and only left .py as a future placeholder.

Given what you just clarified, the revised spec should explicitly support both:

render-document.mjs  → Node/docx renderer
render_document.py   → Python/python-docx renderer

with the same output contract:

write final .docx to OUTPUT_DOCX_PATH