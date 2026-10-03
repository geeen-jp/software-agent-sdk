"""Per-turn outputSchema transport for the built-in Codex ACP path.

codex-acp does not forward a caller schema to the Codex App Server, which
accepts outputSchema on turn/start rather than thread/start. This module points
CODEX_PATH at a small JSON-RPC proxy that adds the caller schema to every
turn/start without one. codex-acp's own title-generation turn already carries a
schema and remains unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any


CODEX_PATH_ENV = "CODEX_PATH"
CODEX_BRIDGE_PATH_ENV = "OH_CODEX_BRIDGE_PATH"
CODEX_RUNTIME_PATH_ENV = "OH_CODEX_RUNTIME_PATH"
CODEX_OUTPUT_SCHEMA_ENV = "OH_CODEX_OUTPUT_SCHEMA"

_BRIDGE_JS = """\
#!/usr/bin/env node
"use strict";
const { spawn } = require("node:child_process");

function fail(message) {
  process.stderr.write("codex output-schema bridge: " + message + "\\n");
  process.exit(2);
}

const runtime = process.env.OH_CODEX_RUNTIME_PATH;
const schemaText = process.env.OH_CODEX_OUTPUT_SCHEMA;
if (!runtime) fail("OH_CODEX_RUNTIME_PATH is not set");
if (!schemaText) fail("OH_CODEX_OUTPUT_SCHEMA is not set");
try {
  JSON.parse(schemaText);
} catch (error) {
  fail("OH_CODEX_OUTPUT_SCHEMA is not valid JSON");
}

const env = { ...process.env };
for (const name of [
  "OH_CODEX_RUNTIME_PATH",
  "OH_CODEX_OUTPUT_SCHEMA",
  "OH_CODEX_BRIDGE_PATH",
]) {
  delete env[name];
}

const child = spawn(runtime, process.argv.slice(2), {
  stdio: ["pipe", "pipe", "inherit"],
  env,
});
child.on("error", (error) => fail("cannot start " + runtime + ": " + error.message));
child.stdin.on("error", () => {});
child.stdout.pipe(process.stdout);

function rewrite(line) {
  let message;
  try {
    message = JSON.parse(line);
  } catch (error) {
    return line;
  }
  if (
    message &&
    message.method === "turn/start" &&
    message.params &&
    message.params.outputSchema == null
  ) {
    message.params.outputSchema = JSON.parse(schemaText);
    return JSON.stringify(message);
  }
  return line;
}

let pending = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  pending += chunk;
  let index;
  while ((index = pending.indexOf("\\n")) >= 0) {
    child.stdin.write(rewrite(pending.slice(0, index)) + "\\n");
    pending = pending.slice(index + 1);
  }
});
process.stdin.on("end", () => {
  if (pending) child.stdin.write(rewrite(pending));
  child.stdin.end();
});

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => child.kill(signal));
}
child.on("close", (code) => process.exit(code === null ? 1 : code));
"""


def ensure_bridge_script() -> Path:
    """Write the proxy launcher once (content-addressed) and return its path."""
    digest = hashlib.sha256(_BRIDGE_JS.encode("utf-8")).hexdigest()[:16]
    directory = Path(tempfile.gettempdir()) / "openhands-codex-bridge"
    path = directory / f"{digest}.cjs"
    if path.exists():
        return path
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = directory / f"{digest}.{os.getpid()}.tmp"
    staging.write_text(_BRIDGE_JS, encoding="utf-8")
    staging.chmod(0o700)
    os.replace(staging, path)
    return path


def build_codex_output_schema_env(
    schema: Mapping[str, Any], env: Mapping[str, str]
) -> dict[str, str]:
    """Return environment overrides that route codex-acp through the proxy.

    The runtime is OH_CODEX_RUNTIME_PATH, else the CODEX_PATH codex-acp would
    have used, else codex on PATH. When none is known the variable stays unset
    and the proxy refuses to start instead of falling back to prompt-only.
    """
    bridge = str(ensure_bridge_script())
    overrides = {
        CODEX_PATH_ENV: bridge,
        CODEX_BRIDGE_PATH_ENV: bridge,
        CODEX_OUTPUT_SCHEMA_ENV: json.dumps(schema, allow_nan=False),
    }
    runtime = (
        env.get(CODEX_RUNTIME_PATH_ENV)
        or env.get(CODEX_PATH_ENV)
        or shutil.which("codex", path=env.get("PATH"))
    )
    if runtime:
        overrides[CODEX_RUNTIME_PATH_ENV] = runtime
    return overrides
