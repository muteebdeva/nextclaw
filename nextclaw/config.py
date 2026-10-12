"""Configuration for NextClaw.

Zero-dependency, environment-overridable configuration. No secrets are read
from chat or files here; providers read credentials only from the environment
when explicitly configured.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


def _default_data_dir() -> str:
    return os.environ.get(
        "NEXTCLAW_DATA_DIR", os.path.join(os.path.expanduser("~"), ".nextclaw")
    )


def _default_workspace() -> str:
    return os.environ.get(
        "NEXTCLAW_WORKSPACE", os.path.join(os.path.expanduser("~"), "workspace")
    )


@dataclass
class Config:
    data_dir: str = field(default_factory=_default_data_dir)
    workspace_root: str = field(default_factory=_default_workspace)
    allowed_tools: tuple = (
        "fs.list",
        "fs.read",
        "fs.write",
        "fs.hash",
        "fs.measure",
        "verify.file_report",
        "exec.run",
    )
    max_step_attempts: int = 3
    step_timeout_s: int = 30
    model_provider: str = "stub"
    max_report_chars: int = 200_000
    # Real-LLM settings (milestone 1). Defaults are explicit; unconfigured
    # providers fail closed instead of pretending to work.
    llm_provider: str = "gemini"
    llm_model: str = "gemini-3.7-flash"
    llm_timeout_s: int = 60
    max_turns: int = 12
    task_timeout_s: int = 600


def load_config(path: str | None = None, overrides: dict | None = None) -> Config:
    data: dict = {}
    cfg_path = path or os.path.join(_default_data_dir(), "nextclaw.json")
    if os.path.exists(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    if overrides:
        data.update(overrides)

    env_provider = os.environ.get("NEXTCLAW_MODEL_PROVIDER")
    return Config(
        data_dir=data.get("data_dir", _default_data_dir()),
        workspace_root=data.get("workspace_root", _default_workspace()),
        allowed_tools=tuple(
            data.get(
                "allowed_tools",
                (
                    "fs.list",
                    "fs.read",
                    "fs.write",
                    "fs.hash",
                    "fs.measure",
                    "verify.file_report",
                    "exec.run",
                ),
            )
        ),
        max_step_attempts=int(data.get("max_step_attempts", 3)),
        step_timeout_s=int(data.get("step_timeout_s", 30)),
        model_provider=(env_provider or data.get("model_provider", "stub")),
        max_report_chars=int(data.get("max_report_chars", 200_000)),
        llm_provider=os.environ.get("NEXTCLAW_LLM_PROVIDER", data.get("llm_provider", "gemini")),
        llm_model=os.environ.get("NEXTCLAW_LLM_MODEL", data.get("llm_model", "gemini-3.7-flash")),
        llm_timeout_s=int(os.environ.get("NEXTCLAW_LLM_TIMEOUT_S", data.get("llm_timeout_s", 60))),
        max_turns=int(data.get("max_turns", 12)),
        task_timeout_s=int(data.get("task_timeout_s", 600)),
    )


def ensure_data_dir(cfg: Config) -> str:
    os.makedirs(cfg.data_dir, exist_ok=True)
    return cfg.data_dir
