"""Model-provider abstraction and routing.

MVP: a deterministic ``stub`` provider generates explicit, verifiable plans for
supported task types. Cloud/local LLM providers are pluggable behind the same
interface but are NOT called unless explicitly configured; when unconfigured
they fail closed with a clear error instead of pretending to work.
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List


class ProviderError(Exception):
    pass


class NotConfigured(ProviderError):
    pass


class UnsupportedTask(ProviderError):
    pass


class StubProvider:
    """Deterministic planner for the MVP's supported task types."""

    name = "stub"

    def generate_plan(
        self, goal: str, task_type: str, params: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        if task_type == "file_report":
            for key in ("root", "pattern", "output"):
                if key not in params:
                    raise UnsupportedTask(f"file_report needs param '{key}'")
            root = params["root"]
            pattern = params["pattern"]
            output = params["output"]
            return [
                {
                    "name": "collect_files",
                    "tool": "fs.list",
                    "args": {"path": root, "pattern": pattern},
                    "verify": {"kind": "is_list"},
                },
                {
                    "name": "measure_files",
                    "tool": "fs.measure",
                    "args": {"path": root, "pattern": pattern},
                    "verify": {"kind": "totals_match", "ref_step": "collect_files"},
                },
                {
                    "name": "write_report",
                    "tool": "fs.write",
                    "args": {"path": output, "content": "{{file_report}}"},
                    "verify": {"kind": "file_exists", "path": output},
                },
                {
                    "name": "verify_report",
                    "tool": "verify.file_report",
                    "args": {"report_path": output, "root": root, "pattern": pattern},
                    "verify": {"kind": "verify_ok"},
                },
            ]
        if task_type == "file_write":
            for key in ("path", "content"):
                if key not in params:
                    raise UnsupportedTask(f"file_write needs param '{key}'")
            digest = hashlib.sha256(str(params["content"]).encode("utf-8")).hexdigest()
            return [
                {
                    "name": "write_file",
                    "tool": "fs.write",
                    "args": {"path": params["path"], "content": params["content"]},
                    "verify": {"kind": "file_exists", "path": params["path"]},
                },
                {
                    "name": "verify_write",
                    "tool": "verify.file_write",
                    "args": {"path": params["path"], "sha256": digest},
                    "verify": {"kind": "verify_ok"},
                },
            ]
        if task_type == "browser_research":
            for key in ("url", "output"):
                if key not in params:
                    raise UnsupportedTask(f"browser_research needs param '{key}'")
            return [
                {
                    "name": "fetch_page",
                    "tool": "browser.fetch",
                    "args": {"url": params["url"]},
                    "verify": {"kind": "fetch_ok"},
                },
                {
                    "name": "write_report",
                    "tool": "fs.write",
                    "args": {"path": params["output"], "content": "{{research_report}}"},
                    "verify": {"kind": "file_exists", "path": params["output"]},
                },
                {
                    "name": "verify_report",
                    "tool": "verify.file_write",
                    "args": {"path": params["output"], "sha256": "{{research_report_sha256}}"},
                    "verify": {"kind": "verify_ok"},
                },
            ]
        raise UnsupportedTask(f"stub provider has no plan for task_type '{task_type}'")


class OllamaProvider:
    """Local LLM provider (optional; fails closed when unconfigured)."""

    name = "ollama"

    def generate_plan(self, goal: str, task_type: str, params: Dict[str, Any]):
        raise NotConfigured(
            "Ollama provider is not configured. Set NEXTCLAW_OLLAMA_URL and "
            "NEXTCLAW_OLLAMA_MODEL, or use the default 'stub' provider."
        )


class OpenAICompatibleProvider:
    """Cloud OpenAI-compatible provider (optional; fails closed when unconfigured)."""

    name = "openai_compatible"

    def generate_plan(self, goal: str, task_type: str, params: Dict[str, Any]):
        raise NotConfigured(
            "OpenAI-compatible provider is not configured. Set NEXTCLAW_API_BASE and "
            "NEXTCLAW_API_KEY, or use the default 'stub' provider."
        )


def route(task_type: str, goal: str, cfg):
    """Select a provider based on configuration (MVP: explicit, no guessing)."""
    name = (cfg.model_provider or "stub").lower()
    if name == "stub":
        return StubProvider()
    if name == "ollama":
        return OllamaProvider()
    if name in ("openai_compatible", "openai", "cloud"):
        return OpenAICompatibleProvider()
    raise ProviderError(f"unknown model provider '{cfg.model_provider}'")
