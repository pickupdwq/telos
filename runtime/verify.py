#!/usr/bin/env python3
"""Verify the target Mac without external model calls or real credentials."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from identity import VERSION

ROOT = Path(__file__).resolve().parent


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-dir", type=Path, default=Path.home() / ".local/share/telos")
    parser.add_argument("--report", type=Path, default=Path("telos-verification.json"))
    args = parser.parse_args()
    install = args.install_dir.expanduser().resolve()
    if sys.version_info < (3, 12) or sys.platform != "darwin":
        raise SystemExit("需要 macOS 与 Python 3.12+。")
    manifest = json.loads((install / "install-manifest.json").read_text())
    python = manifest.get("python", sys.executable)
    codex = manifest.get("codex_executable") or shutil.which("codex")
    if not codex:
        raise SystemExit("未找到 Codex CLI；请检查安装清单或 PATH。")
    command_launcher = Path(manifest["codex_launcher"])
    require(command_launcher.is_file(), "未找到安装时生成的 Codex 启动器，请重新运行安装程序。")
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "portable-verify", "version": "1"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "telos_open", "arguments": {}}},
    ]
    with tempfile.TemporaryDirectory(prefix="glm-verify-") as folder:
        temp = Path(folder)
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join([str(command_launcher.parent), str(Path(codex).parent), str(Path(python).parent), env.get("PATH", "/usr/bin:/bin")])
        env.update(TELOS_STATE_DIR=str(temp / "private"), TELOS_CACHE_DIR=str(temp / "cache"))
        probe = subprocess.run([python, str(install / "telos.py"), "serve"],
                               input="".join(json.dumps(r) + "\n" for r in requests), env=env,
                               capture_output=True, text=True, timeout=20, check=True)
        responses = {r["id"]: r["result"] for r in map(json.loads, probe.stdout.splitlines())}
        tools = [x["name"] for x in responses[2]["tools"]]
        require(responses[1]["serverInfo"]["name"] == "telos" and responses[1]["serverInfo"]["version"] == VERSION, "MCP 名称或版本不匹配。")
        require(len(tools) == 24 and "telos_receive" in tools and "telos_return_control" in tools, "MCP 工具不完整。")
        require(responses[3]["isError"] and "trusted MCP" in responses[3]["content"][0]["text"], "来源绑定没有拒绝无来源请求。")
        print("MCP 版本、24 个工具和来源绑定检查通过。", flush=True)
        tests = subprocess.run([python, str(install / "test_callbacks.py")],
                               env=env, capture_output=True, text=True, timeout=90)
        if tests.returncode:
            sys.stderr.write(tests.stderr[-5000:])
            raise SystemExit("回归失败。尚不能确认该 Mac 可运行此流程。")
        print("验收门禁和回传回归通过。", flush=True)
        repairs = subprocess.run([python, str(install / "test_repairs.py")],
                                 env=env, capture_output=True, text=True, timeout=90)
        if repairs.returncode:
            sys.stderr.write(repairs.stderr[-5000:])
            raise SystemExit("本机修复回归失败。")
        print("桌面适配和考试格式修复回归通过。", flush=True)
        subprocess.run([python, str(ROOT / "verify_native.py"), "--source", str(install),
                        "--report", str(temp / "native.json")], check=True, timeout=100, env=env)
        subprocess.run([python, str(ROOT / "verify_worker_env.py")], check=True, timeout=100,
                       env={**env, "TELOS_ENV_REPORT": str(temp / "environment.json")})
        report = {"name": "telos", "version": VERSION, "passed": True, "tool_count": len(tools), "tools": tools,
                  "regression_log": tests.stderr, "repair_regression_log": repairs.stderr,
                  "native": json.loads((temp / "native.json").read_text()),
                  "worker_environment": json.loads((temp / "environment.json").read_text()),
                  "external_model_calls": False, "real_credential_validation": False,
                  "scope": "Local macOS CLI/MCP/native queue only; real credentials and Desktop automatic return are not verified."}
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print("本机离线验证通过；真实服务商需另行运行 verify_live_providers.py --live。报告：" + str(args.report))


if __name__ == "__main__":
    main()
