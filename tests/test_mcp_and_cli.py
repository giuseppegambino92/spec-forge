"""Interfaces: MCP JSON-RPC server, Typer CLI, and the LLM HTTP adapters (against a local wire server)."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from typer.testing import CliRunner

from spec_forge.interfaces.cli import app
from spec_forge.interfaces.mcp_server import MCPServer, serve
from spec_forge.llm.adapters import AnthropicAdapter, LLMRefusal, OllamaAdapter, OpenAIAdapter
from spec_forge.llm.policy import LLMRepairPolicy, extract_code

QUOTA = '''def deduct_quota(remaining: int, cost: int) -> int:
    """
    :requires: 0 <= remaining <= 2**32 - 1
    :requires: 0 <= cost <= 2**32 - 1
    :ensures: 0 <= result <= remaining
    """
    return remaining - cost
'''


def rpc(server: MCPServer, method: str, params=None, mid=1):
    msg = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None:
        msg["params"] = params
    return server.handle(msg)


# ----------------------------------------------------------------------------- MCP

@pytest.fixture
def mcp(inprocess_config):
    server = MCPServer(inprocess_config)
    yield server
    server.close()


def test_mcp_handshake_and_tool_listing(mcp):
    init = rpc(mcp, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "t", "version": "0"}})
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert init["result"]["serverInfo"]["name"] == "spec-forge"
    assert mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    names = {t["name"] for t in rpc(mcp, "tools/list")["result"]["tools"]}
    assert names == {"specforge_verify", "specforge_heal", "specforge_audit"}
    assert rpc(mcp, "ping")["result"] == {}


def test_mcp_tools_verify_heal_audit(mcp):
    v = rpc(mcp, "tools/call", {"name": "specforge_verify", "arguments": {"source": QUOTA}})["result"]
    assert v["structuredContent"]["status"] == "falsified"
    assert v["structuredContent"]["targets"][0]["counterexamples"][0]["minimal_input"] == {"remaining": 0, "cost": 1}
    h = rpc(mcp, "tools/call", {"name": "specforge_heal", "arguments": {"source": QUOTA}})["result"]
    assert h["structuredContent"]["status"] == "repaired" and "max(0, remaining - cost)" in h["structuredContent"]["diff"]
    a = rpc(mcp, "tools/call", {"name": "specforge_audit", "arguments": {"source": "import os"}})["result"]
    assert json.loads(a["content"][0]["text"])["allowed"] is False


def test_mcp_errors(mcp):
    assert rpc(mcp, "nope")["error"]["code"] == -32601
    assert rpc(mcp, "tools/call", {"name": "missing", "arguments": {"source": "x=1"}})["error"]["code"] == -32602
    assert rpc(mcp, "tools/call", {"name": "specforge_verify", "arguments": {}})["error"]["code"] == -32602
    assert mcp.handle({"id": 3, "method": "ping"})["error"]["code"] == -32600


def test_mcp_stdio_loop_including_parse_errors_and_batches(inprocess_config):
    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}}),
        "{not json",
        json.dumps([{"jsonrpc": "2.0", "id": 2, "method": "ping"},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"}]),
    ]
    out = io.StringIO()
    serve(io.StringIO("\n".join(lines) + "\n"), out, inprocess_config)
    replies = [json.loads(x) for x in out.getvalue().splitlines()]
    assert replies[0]["result"]["protocolVersion"] == "2024-11-05"
    assert replies[1]["error"]["code"] == -32700
    assert replies[2] == [{"jsonrpc": "2.0", "id": 2, "result": {}}]


def test_mcp_server_as_real_subprocess(tmp_path):
    reqs = "\n".join(json.dumps(m) for m in [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "specforge_audit", "arguments": {"source": "().__class__.__base__.__subclasses__()"}}},
    ]) + "\n"
    proc = subprocess.run([sys.executable, "-m", "spec_forge.interfaces.cli", "mcp"], input=reqs,
                          capture_output=True, text=True, timeout=60, cwd=tmp_path)
    replies = [json.loads(x) for x in proc.stdout.splitlines()]
    assert [r["id"] for r in replies] == [1, 2]
    assert replies[1]["result"]["structuredContent"]["allowed"] is False


# ----------------------------------------------------------------------------- CLI

def test_cli_audit_verify_heal(tmp_path):
    runner = CliRunner()
    bad = tmp_path / "quota.py"
    bad.write_text(QUOTA)
    evil = tmp_path / "evil.py"
    evil.write_text("import subprocess\n")
    ledger = str(tmp_path / "l.db")
    assert runner.invoke(app, ["audit", str(evil)]).exit_code == 1
    assert runner.invoke(app, ["audit", str(bad)]).exit_code == 0
    r = runner.invoke(app, ["verify", str(bad), "--json", "--inprocess", "--ledger", ledger])
    assert r.exit_code == 1 and json.loads(r.stdout)["status"] == "falsified"
    out = tmp_path / "fixed.py"
    r = runner.invoke(app, ["heal", str(bad), "--out", str(out), "--inprocess", "--ledger", ledger])
    assert r.exit_code == 0, r.stdout
    assert "independent validation: PASS" in r.stdout
    assert "max(0, remaining - cost)" in out.read_text()
    r = runner.invoke(app, ["verify", str(out), "--inprocess", "--ledger", ledger])
    assert r.exit_code == 0
    assert runner.invoke(app, ["version"]).exit_code == 0
    assert runner.invoke(app, ["verify", str(tmp_path / "missing.py")]).exit_code == 2


def test_dashboard_renders_a_full_run(inprocess_config):
    from rich.console import Console

    from spec_forge.interfaces.tui import run_dashboard
    from spec_forge.orchestrator.cegis import SpecForge

    console = Console(file=io.StringIO(), width=160, force_terminal=False)
    with SpecForge(inprocess_config) as sf:
        rep = run_dashboard(QUOTA, forge=sf, console=console)
    assert rep["status"] == "repaired"
    assert "verified patch" in console.file.getvalue()


# ----------------------------------------------------------------------------- LLM adapters (wire format)

class _WireServer(BaseHTTPRequestHandler):
    """Local HTTP endpoint speaking the providers' JSON wire formats."""

    captured: list[dict] = []
    refuse = False

    def log_message(self, *args):  # silence
        pass

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        type(self).captured.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
        fixed = "```python\n" + QUOTA.replace("return remaining - cost", "return max(0, remaining - cost)") + "```"
        if self.path == "/v1/messages":
            payload = ({"stop_reason": "refusal", "stop_details": {"category": "cyber"}, "content": []}
                       if type(self).refuse else
                       {"stop_reason": "end_turn", "content": [{"type": "text", "text": fixed}]})
        elif self.path == "/v1/chat/completions":
            payload = {"choices": [{"message": {"content": fixed}}]}
        else:
            payload = {"message": {"content": fixed}}
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def wire():
    srv = HTTPServer(("127.0.0.1", 0), _WireServer)
    _WireServer.captured = []
    _WireServer.refuse = False
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_anthropic_adapter_wire_format(wire, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    a = AnthropicAdapter(base_url=wire)
    text = a.complete("sys", "fix it")
    req = _WireServer.captured[-1]
    assert req["path"] == "/v1/messages"
    assert req["headers"]["x-api-key"] == "test-key" and req["headers"]["anthropic-version"] == "2023-06-01"
    assert req["body"]["model"] == "claude-opus-5-5" and req["body"]["system"] == "sys"
    assert req["body"]["messages"] == [{"role": "user", "content": "fix it"}]
    assert extract_code(text)
    _WireServer.refuse = True
    with pytest.raises(LLMRefusal):
        a.complete("sys", "fix it")


def test_openai_and_ollama_adapters_wire_format(wire, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert extract_code(OpenAIAdapter(model="some-model", base_url=wire).complete("s", "p"))
    req = _WireServer.captured[-1]
    assert req["path"] == "/v1/chat/completions" and req["headers"]["authorization"] == "Bearer sk-test"
    assert req["body"]["messages"][0] == {"role": "system", "content": "s"}
    assert extract_code(OllamaAdapter(model="llama3.1", base_url=wire).complete("s", "p"))
    assert _WireServer.captured[-1]["path"] == "/api/chat" and _WireServer.captured[-1]["body"]["stream"] is False


def test_llm_policy_drives_a_heal_end_to_end(wire, monkeypatch, inprocess_config):
    from spec_forge.orchestrator.cegis import SpecForge

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    policy = LLMRepairPolicy(AnthropicAdapter(base_url=wire))
    prompt = policy.build_prompt(QUOTA, "deduct_quota", ["postcondition:bounds"], ["0 <= result <= remaining"])
    assert "<untrusted_module>" in prompt
    with SpecForge(inprocess_config, policy=policy) as sf:
        rep = sf.heal(QUOTA)
    assert rep["status"] == "repaired"
    assert _WireServer.captured, "policy was consulted over HTTP"
