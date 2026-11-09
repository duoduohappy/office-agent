"""语音转写工具（方案 4.2 / tools/audio_tools.py）。

封装 whisper.cpp 的本地命令行（CPU 推理，不需要 GPU）。
未安装时如实报缺失，不伪造转写结果——生产环境里沉默比幻觉便宜。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from core.config import config
from core.tool_registry import ToolResult, tool

SANDBOX = config.project_root.resolve()


def _resolve(path: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = SANDBOX / p
    resolved = p.resolve()
    if SANDBOX != resolved and SANDBOX not in resolved.parents:
        raise PermissionError(f"路径越界：{path}")
    return resolved


def whisper_available() -> bool:
    """探测 whisper.cpp 可执行文件。"""
    configured = os.getenv("WHISPER_CPP_BIN")
    if configured and Path(configured).is_file():
        return True
    return shutil.which("whisper-cli") is not None or shutil.which("main") is not None


def _whisper_bin() -> str:
    configured = os.getenv("WHISPER_CPP_BIN")
    if configured and Path(configured).is_file():
        return configured
    for name in ("whisper-cli", "main"):
        found = shutil.which(name)
        if found:
            return found
    raise FileNotFoundError("未找到 whisper.cpp，可设置环境变量 WHISPER_CPP_BIN 指向 main.exe")


@tool(
    name="load_transcript",
    description="加载已有的会议转写稿（txt/md 文本）。也用于读取录音转写后的结果文件",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string", "description": "转写稿路径，通常位于 data/inbox/"}},
        "required": ["path"],
    },
)
def load_transcript(path: str) -> ToolResult:
    resolved = _resolve(path)
    if not resolved.is_file():
        return ToolResult.failure("load_transcript", f"转写稿不存在：{path}")
    text = resolved.read_text(encoding="utf-8", errors="replace")
    return ToolResult(
        ok=True, content=text, tool_name="load_transcript",
        meta={"path": str(resolved), "chars": len(text)},
    )


@tool(
    name="transcribe_audio",
    description="用本地 whisper.cpp 把会议录音（wav/mp3）转写成文字，CPU 即可运行",
    parameters={
        "type": "object",
        "properties": {
            "audio_path": {"type": "string", "description": "录音文件路径（16kHz wav 最佳）"},
            "model_path": {"type": "string", "description": "ggml 模型路径，默认取 WHISPER_CPP_MODEL 环境变量"},
        },
        "required": ["audio_path"],
    },
)
def transcribe_audio(audio_path: str, model_path: str = "") -> ToolResult:
    if not whisper_available():
        return ToolResult.failure(
            "transcribe_audio",
            "whisper.cpp 未安装（缺少 WHISPER_CPP_BIN）。可先用 load_transcript 加载已有转写稿，"
            "或安装 whisper.cpp 后重试。",
        )
    audio = _resolve(audio_path)
    if not audio.is_file():
        return ToolResult.failure("transcribe_audio", f"录音文件不存在：{audio_path}")
    model = model_path or os.getenv("WHISPER_CPP_MODEL", "")
    if not model or not Path(model).is_file():
        return ToolResult.failure("transcribe_audio", "未配置 WHISPER_CPP_MODEL 或模型文件不存在")

    out_prefix = config.workspace_dir / f"whisper_{audio.stem}"
    config.workspace_dir.mkdir(parents=True, exist_ok=True)
    cmd = [_whisper_bin(), "-m", model, "-f", str(audio), "-l", "zh", "-otxt", "-of", str(out_prefix)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900, check=False)
    except subprocess.TimeoutExpired:
        return ToolResult.failure("transcribe_audio", "转写超时（>15 分钟）")
    txt_file = Path(str(out_prefix) + ".txt")
    if not txt_file.is_file():
        return ToolResult.failure("transcribe_audio", f"whisper.cpp 未产出文本：{proc.stderr[-500:]}")
    text = txt_file.read_text(encoding="utf-8", errors="replace")
    return ToolResult(
        ok=True, content=text, tool_name="transcribe_audio",
        meta={"path": str(txt_file), "chars": len(text)},
    )


AUDIO_TOOLS = [load_transcript, transcribe_audio]
