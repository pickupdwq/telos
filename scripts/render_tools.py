#!/usr/bin/env python3
"""Generate MCP reference directly from the runtime catalog (no model calls)."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
from telos import TOOLS

lines = ["# MCP tools / MCP 工具", "", "[English README](../README.md) · [中文 README](../README.zh-CN.md)", "",
         f"Generated from `runtime/telos.py`. These {len(TOOLS)} tools are intended for the original Codex supervisor; "
         "task ownership requires trusted MCP thread metadata. See [machine-readable schemas](tools.schema.json) for all fields.", "",
         f"由运行时自动生成，共 {len(TOOLS)} 个工具，供原始 Codex 大总管使用；任务绑定依赖可信会话元数据。完整参数见 JSON schema。", ""]
for tool in TOOLS:
    schema = tool["inputSchema"]
    required = ", ".join(f"`{name}`" for name in schema.get("required", [])) or "None"
    lines += [f"## `{tool['name']}`", "", tool["description"], "", f"Required / 必填：{required}", ""]
(ROOT / "docs/tools.md").write_text("\n".join(lines))
(ROOT / "docs/tools.schema.json").write_text(json.dumps({"tools": TOOLS}, ensure_ascii=False, indent=2) + "\n")
