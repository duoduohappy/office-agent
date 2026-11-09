"""文件工具集（方案 tools/file_tools.py）。

最小可用集：读写文本、列目录、找文件。所有路径被限制在
data/ 沙箱内，防止模型越权访问系统目录——安全边界的一部分。
"""

from __future__ import annotations

from pathlib import Path

from core.config import config
from core.tool_registry import ToolResult, tool

_SANDBOX = config.project_root


def _resolve_in_sandbox(path: str) -> Path:
    """把用户给的路径解析到项目沙箱内；越界直接抛错。"""
    p = Path(path)
    if not p.is_absolute():
        p = _SANDBOX / p
    resolved = p.resolve()
    sandbox = _SANDBOX.resolve()
    if sandbox != resolved and sandbox not in resolved.parents:
        raise PermissionError(f"路径越界，只允许访问 {sandbox} 内的文件：{path}")
    return resolved


@tool(
    name="read_file",
    description="读取一个文本文件的内容（txt/md/csv/json 等）",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径，相对路径以项目根为基准"},
            "max_chars": {"type": "integer", "description": "最多返回的字符数，默认 8000"},
        },
        "required": ["path"],
    },
)
def read_file(path: str, max_chars: int = 8000) -> ToolResult:
    resolved = _resolve_in_sandbox(path)
    if not resolved.is_file():
        return ToolResult.failure("read_file", f"文件不存在：{path}")
    text = resolved.read_text(encoding="utf-8", errors="replace")
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n...（已截断，原文 {len(text)} 字符）"
    return ToolResult(ok=True, content=text, tool_name="read_file", meta={"path": str(resolved)})


@tool(
    name="write_file",
    description="把文本内容写入文件（覆盖写入）。产出文档放到 data/outbox/ 下",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "目标文件路径，建议以 data/outbox/ 开头"},
            "content": {"type": "string", "description": "要写入的完整内容"},
        },
        "required": ["path", "content"],
    },
    dangerous=True,
)
def write_file(path: str, content: str) -> ToolResult:
    resolved = _resolve_in_sandbox(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    return ToolResult(
        ok=True,
        content=f"已写入 {len(content)} 字符到 {resolved}",
        tool_name="write_file",
        meta={"path": str(resolved)},
    )


@tool(
    name="list_dir",
    description="列出目录下的文件和子目录",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string", "description": "目录路径，默认项目根"}},
        "required": [],
    },
)
def list_dir(path: str = ".") -> ToolResult:
    resolved = _resolve_in_sandbox(path)
    if not resolved.is_dir():
        return ToolResult.failure("list_dir", f"目录不存在：{path}")
    entries = sorted(resolved.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    lines = []
    for entry in entries[:200]:
        kind = "DIR " if entry.is_dir() else "FILE"
        size = entry.stat().st_size if entry.is_file() else 0
        lines.append(f"{kind}  {entry.name}  {size}")
    return ToolResult(
        ok=True,
        content="\n".join(lines) or "（空目录）",
        tool_name="list_dir",
        meta={"path": str(resolved), "count": len(entries)},
    )


FILE_TOOLS = [read_file, write_file, list_dir]
