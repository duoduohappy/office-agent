"""全局配置。所有路径集中在 D 盘，避免污染系统盘。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# D:/Projects/office-agent（core/ 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _env_path(key: str, default: Path) -> Path:
    raw = os.getenv(key)
    return Path(raw).expanduser() if raw else default


@dataclass
class Config:
    # ---- 模型 ----
    model: str = os.getenv("OA_MODEL", "qwen2.5:7b-instruct-q4_K_M")
    ollama_host: str = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    temperature: float = float(os.getenv("OA_TEMPERATURE", "0.2"))
    num_ctx: int = int(os.getenv("OA_NUM_CTX", "8192"))
    request_timeout: float = float(os.getenv("OA_TIMEOUT", "180"))

    # ---- harness ----
    max_steps: int = int(os.getenv("OA_MAX_STEPS", "12"))
    max_plan_steps: int = int(os.getenv("OA_MAX_PLAN_STEPS", "8"))

    # ---- 路径 ----
    project_root: Path = PROJECT_ROOT
    data_dir: Path = field(default_factory=lambda: _env_path("OA_DATA_DIR", PROJECT_ROOT / "data"))

    @property
    def inbox_dir(self) -> Path:
        return self.data_dir / "inbox"

    @property
    def outbox_dir(self) -> Path:
        return self.data_dir / "outbox"

    @property
    def workspace_dir(self) -> Path:
        return self.data_dir / "workspace"

    @property
    def memory_db(self) -> Path:
        return self.data_dir / "workspace" / "memory.sqlite3"

    def ensure_dirs(self) -> None:
        for d in (self.inbox_dir, self.outbox_dir, self.workspace_dir):
            d.mkdir(parents=True, exist_ok=True)


config = Config()
