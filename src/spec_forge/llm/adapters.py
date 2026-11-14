"""Minimal, dependency-free LLM adapters (stdlib ``urllib``; no SDKs, no agent frameworks).

* ``AnthropicAdapter``  -> ``POST {base}/v1/messages``        (default model ``claude-opus-5-5``)
* ``OpenAIAdapter``     -> ``POST {base}/v1/chat/completions``
* ``OllamaAdapter``     -> ``POST {base}/api/chat``

LLMs are optional: the engine is fully functional offline with the symbolic repair grammar.
When an adapter is configured, its proposals become additional MCTS actions; every proposal is
re-audited by the firewall and must pass the same verifier as grammar edits.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional


class LLMError(RuntimeError):
    pass


class LLMRefusal(LLMError):
    pass


def _post(url: str, headers: dict[str, str], body: dict[str, Any], timeout: float) -> dict[str, Any]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"content-type": "application/json", **headers},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https/http endpoint
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise LLMError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise LLMError(f"cannot reach {url}: {exc.reason}") from exc


@dataclass
class AnthropicAdapter:
    model: str = "claude-opus-5-5"
    api_key: Optional[str] = None
    base_url: str = field(default_factory=lambda: os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"))
    max_tokens: int = 16000
    timeout_s: float = 300.0
    # Server-side refusal fallback (beta). Set to None to disable.
    fallbacks: Optional[str] = "default"

    def headers(self) -> dict[str, str]:
        key = self.api_key or os.environ.get("ANTHROPIC_API_KEY")
        h = {"anthropic-version": "2023-06-01"}
        if key:
            h["x-api-key"] = key
        elif os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            h["authorization"] = f"Bearer {os.environ['ANTHROPIC_AUTH_TOKEN']}"
        else:
            raise LLMError("set ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) to use the Anthropic adapter")
        if self.fallbacks:
            h["anthropic-beta"] = "server-side-fallback-2026-07-01"
        return h

    def build_body(self, system: str, prompt: str) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self.model, "max_tokens": self.max_tokens, "system": system,
                                "messages": [{"role": "user", "content": prompt}]}
        if self.fallbacks:
            body["fallbacks"] = self.fallbacks
        return body

    @staticmethod
    def parse(resp: dict[str, Any]) -> str:
        if resp.get("stop_reason") == "refusal":
            details = resp.get("stop_details") or {}
            raise LLMRefusal(f"model declined the request (category={details.get('category')})")
        return "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")

    def complete(self, system: str, prompt: str) -> str:
        resp = _post(f"{self.base_url.rstrip('/')}/v1/messages", self.headers(), self.build_body(system, prompt),
                     self.timeout_s)
        return self.parse(resp)


@dataclass
class OpenAIAdapter:
    model: str = field(default_factory=lambda: os.environ.get("SPECFORGE_OPENAI_MODEL", ""))
    api_key: Optional[str] = None
    base_url: str = field(default_factory=lambda: os.environ.get("OPENAI_BASE_URL", "https://api.openai.com"))
    timeout_s: float = 300.0

    def build_body(self, system: str, prompt: str) -> dict[str, Any]:
        if not self.model:
            raise LLMError("pass --model (or set SPECFORGE_OPENAI_MODEL) for the OpenAI adapter")
        return {"model": self.model, "messages": [{"role": "system", "content": system},
                                                  {"role": "user", "content": prompt}]}

    @staticmethod
    def parse(resp: dict[str, Any]) -> str:
        try:
            return resp["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError) as exc:
            raise LLMError(f"unexpected OpenAI response shape: {str(resp)[:200]}") from exc

    def complete(self, system: str, prompt: str) -> str:
        key = self.api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise LLMError("set OPENAI_API_KEY to use the OpenAI adapter")
        base = self.base_url.rstrip("/")
        url = f"{base}/chat/completions" if base.endswith("/v1") else f"{base}/v1/chat/completions"
        return self.parse(_post(url, {"authorization": f"Bearer {key}"}, self.build_body(system, prompt),
                                self.timeout_s))


@dataclass
class OllamaAdapter:
    model: str = field(default_factory=lambda: os.environ.get("SPECFORGE_OLLAMA_MODEL", "llama3.1"))
    base_url: str = field(default_factory=lambda: os.environ.get("OLLAMA_HOST", "http://localhost:11434"))
    timeout_s: float = 600.0

    def build_body(self, system: str, prompt: str) -> dict[str, Any]:
        return {"model": self.model, "stream": False, "messages": [{"role": "system", "content": system},
                                                                   {"role": "user", "content": prompt}]}

    @staticmethod
    def parse(resp: dict[str, Any]) -> str:
        return (resp.get("message") or {}).get("content", "")

    def complete(self, system: str, prompt: str) -> str:
        base = self.base_url if self.base_url.startswith("http") else f"http://{self.base_url}"
        return self.parse(_post(f"{base.rstrip('/')}/api/chat", {}, self.build_body(system, prompt), self.timeout_s))


def make_adapter(provider: str, model: Optional[str] = None) -> Any:
    provider = provider.lower()
    if provider == "anthropic":
        return AnthropicAdapter(model=model) if model else AnthropicAdapter()
    if provider == "openai":
        return OpenAIAdapter(model=model) if model else OpenAIAdapter()
    if provider == "ollama":
        return OllamaAdapter(model=model) if model else OllamaAdapter()
    raise LLMError(f"unknown provider {provider!r} (anthropic | openai | ollama)")
