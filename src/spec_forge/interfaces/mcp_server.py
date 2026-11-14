"""Model Context Protocol server (JSON-RPC 2.0 over stdio, newline-delimited).

Tools
  specforge_verify  infer properties and attack the code; returns counterexamples
  specforge_heal    full CEGIS loop; returns the verified patch (unified diff + source)
  specforge_audit   static firewall audit only (fast, no execution)

Implemented directly on the wire protocol (``initialize``, ``tools/list``, ``tools/call``,
``ping``) with no SDK dependency.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Optional, TextIO

from .. import __version__
from ..config import SpecForgeConfig

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

_SOURCE_SCHEMA = {
    "type": "object",
    "properties": {
        "source": {"type": "string", "description": "Python module source code to analyse"},
        "path": {"type": "string", "description": "Path to a Python file (alternative to `source`)"},
        "targets": {"type": "array", "items": {"type": "string"},
                    "description": "Restrict to these function/class names"},
        "trials": {"type": "integer", "minimum": 1, "maximum": 100000,
                   "description": "Adversarial inputs per target"},
    },
    "additionalProperties": False,
}

TOOLS = [
    {"name": "specforge_verify",
     "description": "Infer formal properties (contracts, idempotency, round-trip, conservation, purity, "
                    "atomicity...) for a Python module and adversarially falsify them in a sandbox. Returns "
                    "minimal counterexamples.",
     "inputSchema": _SOURCE_SCHEMA},
    {"name": "specforge_heal",
     "description": "Run the full CEGIS loop: find counterexamples, localise faults, search AST repairs "
                    "with MCTS, and return a patch verified against every counterexample plus a fresh "
                    "adversarial campaign.",
     "inputSchema": _SOURCE_SCHEMA},
    {"name": "specforge_audit",
     "description": "Static sandbox-escape / prompt-injection firewall audit of Python source. No execution.",
     "inputSchema": {"type": "object", "properties": {"source": {"type": "string"}, "path": {"type": "string"}},
                     "additionalProperties": False}},
]


class MCPServer:
    def __init__(self, config: Optional[SpecForgeConfig] = None) -> None:
        self.config = config or SpecForgeConfig()
        self._forge: Any = None
        self.initialized = False

    @property
    def forge(self) -> Any:
        if self._forge is None:
            from ..orchestrator.cegis import SpecForge

            self._forge = SpecForge(self.config)
        return self._forge

    def close(self) -> None:
        if self._forge is not None:
            self._forge.close()

    # ------------------------------------------------------------------ dispatch
    def handle(self, msg: dict[str, Any]) -> Optional[dict[str, Any]]:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
            return self._error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")
        method, mid, params = msg["method"], msg.get("id"), msg.get("params") or {}
        is_notification = "id" not in msg
        try:
            if method == "initialize":
                requested = params.get("protocolVersion")
                version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
                result: Any = {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                               "serverInfo": {"name": "spec-forge", "version": __version__},
                               "instructions": "Use specforge_audit before executing untrusted code; "
                                               "specforge_verify to falsify; specforge_heal to repair."}
            elif method in ("notifications/initialized", "initialized"):
                self.initialized = True
                return None
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                result = self._call_tool(params.get("name"), params.get("arguments") or {})
            elif method.startswith("notifications/"):
                return None
            else:
                return None if is_notification else self._error(mid, -32601, f"Method not found: {method}")
        except _InvalidParams as exc:
            return self._error(mid, -32602, str(exc))
        except Exception as exc:  # noqa: BLE001
            return self._error(mid, -32603, f"{type(exc).__name__}: {exc}")
        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    @staticmethod
    def _error(mid: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}

    def _source(self, args: dict[str, Any]) -> str:
        if isinstance(args.get("source"), str):
            return args["source"]
        if isinstance(args.get("path"), str):
            with open(args["path"], encoding="utf-8") as fh:
                return fh.read()
        raise _InvalidParams("provide `source` or `path`")

    def _call_tool(self, name: Optional[str], args: dict[str, Any]) -> dict[str, Any]:
        if name not in {t["name"] for t in TOOLS}:
            raise _InvalidParams(f"unknown tool {name!r}")
        source = self._source(args)
        if name == "specforge_audit":
            from ..sandbox.firewall import audit_source

            payload: dict[str, Any] = audit_source(source, self.config.sandbox).to_dict()
            is_error = False
        else:
            targets = args.get("targets")
            if args.get("trials"):
                self.forge.config.fuzz.trials = int(args["trials"])
            report = self.forge.verify(source, targets) if name == "specforge_verify" else \
                self.forge.heal(source, targets)
            payload = _compact(report)
            is_error = report["status"] in ("rejected", "error")
        return {"content": [{"type": "text", "text": json.dumps(payload, indent=2, default=str)}],
                "structuredContent": payload, "isError": is_error}


class _InvalidParams(ValueError):
    pass


def _compact(report: dict[str, Any]) -> dict[str, Any]:
    targets = []
    for t in report["targets"]:
        targets.append({
            "target": t["target"], "status": t["status"],
            "properties": [f"{p['name']} ({p['origin']}): {p['description']}" for p in t["properties"]],
            "counterexamples": [{"property": c["property"], "message": c["minimal_message"],
                                 "minimal_input": c["minimal_case"],
                                 "shrink": f"{c['initial_complexity']} -> {c['final_complexity']}"}
                                for c in t["counterexamples"]],
            "repairs": [{"success": r["success"], "edits": r["edits"]} for r in t["repairs"]],
        })
    return {"status": report["status"], "targets": targets, "diff": report.get("diff", ""),
            "patched_source": report.get("patched_source"), "audit": report.get("audit")}


def serve(stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout, config: Optional[SpecForgeConfig] = None) -> None:
    server = MCPServer(config)
    try:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                resp: Optional[dict[str, Any]] = MCPServer._error(None, -32700, "Parse error")
            else:
                if isinstance(msg, list):  # JSON-RPC batch
                    out = [r for r in (server.handle(m) for m in msg) if r is not None]
                    if out:
                        stdout.write(json.dumps(out) + "\n")
                        stdout.flush()
                    continue
                resp = server.handle(msg)
            if resp is not None:
                stdout.write(json.dumps(resp, default=str) + "\n")
                stdout.flush()
    finally:
        server.close()
