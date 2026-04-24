#!/usr/bin/env python3
"""
standl PostToolUse hook — family handoff to stanobj.

Triggers after a Bash tool call. If the command was a successful invocation
of the ``standl`` CLI (``standl run`` / ``validate`` / ``meta-check``) and
exited 0, inject a handoff hint into the conversation so the main agent
knows the natural next step in the stan* family pipeline is ``stanobj``.

Design rules:
- Never block. This is PostToolUse — the tool already executed.
- No-op on any non-standl command or non-zero exit, silently.
- No-op on any parse error or missing field (hook must never break
  a working session).
- Exit 0 in all cases. Feedback is delivered via JSON stdout
  (``hookSpecificOutput.additionalContext``), with stderr as a
  best-effort fallback for older runtimes.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Optional

# Match `standl <verb>` as a stand-alone token — not a substring inside
# some other word. Covers command separators (`;`, `&&`, `|`, newline, SOL)
# and the supported verbs per standl's CLI.
_STANDL_CMD = re.compile(
    r"(?:^|[\s;|&\n])standl\s+(?:run|validate|meta-check)\b"
)

# Silence false positives from package-manager invocations.
_PKG_MGR_CMD = re.compile(
    r"\b(?:pip|pip3|conda|mamba|micromamba|uv|poetry|pipx)"
    r"\s+(?:install|uninstall|add|remove|show|list|search|info|update|upgrade|sync)\b"
)

_HANDOFF = (
    "✅ standl 执行成功。在 stan* 家族流水线里,standl 的下游是 **stanobj**。\n"
    "\n"
    "stanobj 消费 standl 的产物:\n"
    "  - `<outdir>/manifest.json` — 文件级 provenance\n"
    "  - `<outdir>/raw/` — 待转换的原始文件(mtx/csv/h5/loom/RDS 等)\n"
    "  - `<outdir>/design.yaml` — 用于填充 obs metadata\n"
    "并产出标准 h5ad(在 eca-curation 管线里就是 `00_ingested/adata.zarr`)。\n"
    "\n"
    "建议的下一步:\n"
    "  • 如果当前在 eca-curation pipeline session 里:运行\n"
    "      /eca-run <dataset>\n"
    "    推进到 00_ingested 阶段(内部会调 stanobj)。\n"
    "  • 否则直接告知用户:“接下来用 stanobj 把 raw 文件转成 h5ad”,\n"
    "    让 stanobj skill 接管。\n"
    "\n"
    "跳过 stanobj 的合理情形:standl 产物本身已经是 h5ad,或用户显式表示"
    "跳过格式转换。其余情况默认应继续到 stanobj。"
)


def _read_payload() -> dict:
    try:
        raw = sys.stdin.read()
    except OSError:
        return {}
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _get_bash_command(payload: dict) -> Optional[str]:
    """Extract the Bash command string across known payload shapes."""
    tool_name = (
        payload.get("tool_name")
        or (payload.get("tool_use") or {}).get("name")
        or (payload.get("toolUse") or {}).get("name")
    )
    if tool_name != "Bash":
        return None
    tool_input = (
        payload.get("tool_input")
        or (payload.get("tool_use") or {}).get("input")
        or (payload.get("toolUse") or {}).get("input")
        or {}
    )
    cmd = tool_input.get("command")
    return cmd if isinstance(cmd, str) else None


def _exit_code(payload: dict) -> int:
    """Extract the exit code across known payload shapes. Default 0 on ambiguity."""
    res = (
        payload.get("tool_response")
        or payload.get("tool_result")
        or payload.get("toolResult")
        or {}
    )
    if not isinstance(res, dict):
        return 0
    for key in ("exit_code", "exitCode", "returncode", "returnCode"):
        code = res.get(key)
        if code is not None:
            try:
                return int(code)
            except (TypeError, ValueError):
                return 1
    # Fall back to explicit error flags.
    if res.get("is_error") or res.get("isError"):
        return 1
    return 0


def main() -> int:
    payload = _read_payload()
    cmd = _get_bash_command(payload)
    if not cmd:
        return 0
    if _exit_code(payload) != 0:
        return 0

    # Split by shell operators so chained commands like
    # ``pip install standl && standl run ...`` still trigger on the run
    # segment while the install segment is skipped.
    matched = False
    for seg in re.split(r"(?:&&|\|\||;|\|)", cmd):
        if _PKG_MGR_CMD.search(seg):
            continue
        if _STANDL_CMD.search(seg):
            matched = True
            break
    if not matched:
        return 0

    out = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": _HANDOFF,
        }
    }
    try:
        print(json.dumps(out))
    except Exception:
        pass
    # Best-effort fallback for runtimes that surface stderr to the agent.
    try:
        print(_HANDOFF, file=sys.stderr)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
