"""Local fake-model control test of Codex exec_command credential exclusion."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SENTINEL = "NON_SECRET_ENVIRONMENT_PROBE"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


class Endpoint(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        called = any(x.get("type") == "function_call_output" for x in request.get("input", []))
        if called:
            item = {"type": "message", "id": "msg_probe", "role": "assistant", "content": [
                {"type": "output_text", "text": "Probe done.", "annotations": []}]}
        else:
            item = {"type": "function_call", "id": "fc_probe", "call_id": "call_probe", "name": "exec_command",
                    "arguments": json.dumps({"cmd": "printenv ARK_API_KEY || true", "max_output_tokens": 100})}
        response = {"id": "resp_probe", "object": "response", "status": "completed", "model": "probe",
                    "output": [item], "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
        events = [{"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                  {"type": "response.output_item.added", "output_index": 0, "item": item},
                  {"type": "response.output_item.done", "output_index": 0, "item": item},
                  {"type": "response.completed", "response": response}]
        data = "".join("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n" for e in events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    service = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    threading.Thread(target=service.serve_forever, daemon=True).start()
    result = {"real_credentials_used": False, "external_requests": False, "codex": subprocess.check_output(["codex", "--version"], text=True).strip(), "cases": []}
    try:
        for exclude in (False, True):
            with tempfile.TemporaryDirectory(prefix="gp-env-") as folder:
                env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TMPDIR")}
                env.update(CODEX_HOME=folder, ARK_API_KEY=SENTINEL)
                settings = ['model_provider="probe"', 'model="probe"', 'model_providers.probe.name="local"',
                            'model_providers.probe.wire_api="responses"',
                            f'model_providers.probe.base_url="http://127.0.0.1:{service.server_port}/v1"',
                            'approval_policy="never"', 'sandbox_mode="danger-full-access"',
                            'features.hooks=false', 'features.plugins=false', 'features.apps=false',
                            'features.skip_host_skill_discovery=true']
                if exclude:
                    settings.append('shell_environment_policy.exclude=["ARK_API_KEY"]')
                argv = ["codex", "--no-daemon"]
                for value in settings:
                    argv += ["-c", value]
                argv += ["exec", "--ignore-user-config", "--skip-git-repo-check", "--json", "-"]
                proc = subprocess.run(argv, input=b"Run the local environment probe.", env=env, cwd=folder,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=40)
                events = [json.loads(x) for x in proc.stdout.decode().splitlines() if x.startswith("{")]
                commands = [x["item"] for x in events if x.get("type") == "item.completed" and
                            x.get("item", {}).get("type") == "command_execution"]
                require(proc.returncode == 0 and commands, "Codex did not execute the environment probe successfully")
                visible = any(SENTINEL in x.get("aggregated_output", "") for x in commands)
                require(visible == (not exclude), "Shell credential exclusion did not match the configured policy")
                result["cases"].append({"exclude_enabled": exclude, "command_executed": True,
                                        "fake_key_visible_to_command": visible, "returncode": proc.returncode})
    finally:
        service.shutdown()
    Path(os.environ.get("TELOS_ENV_REPORT", "worker-env-probe.json")).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
