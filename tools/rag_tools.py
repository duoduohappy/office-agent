"""向量检索与引用溯源工具集（方案 4.3 / 4.6）。

与原 `knowledge_tools.search_knowledge_base`（BM25 关键词检索）并存：
- 原工具保持不变，两轮真实评测的口径与工具序列不受影响；
- 本文件的工具走 Chroma 向量索引，返回结构化 Citation（来源 / 片段号 /
  sha256 / 相似度 / 时间权重），并提供引文核验；
- 后端与 embedding 的真实身份会在结果里写明，不把 hashing 特征说成语义模型。

这些工具**未注册进 eval/evaluator.py 的评测 registry**，因此不会改变
20 条评测集的系统提示与已有成绩；要纳入正式评测须单独授权并全量重跑。
"""

from __future__ import annotations

import re
from pathlib import Path

from core.config import config
from core.tool_registry import ToolResult, tool
from memory.vector_store import get_store
from tools.doc_tools import _extract_text


def _safe_source(source: str) -> Path | None:
    """防止目录穿越：解析后必须仍在项目根目录内。"""
    if not source or source.startswith(("/", "\\")):
        return None
    path = (config.project_root / source).resolve()
    root = config.project_root.resolve()
    if root not in path.parents or not path.is_file():
        return None
    return path


@tool(
    name="vector_search",
    description=(
        "用本地向量库检索知识库，返回带 [n] 编号、来源文件路径、片段号、sha256 的证据清单。"
        "回答时只能用 [n] 指向清单里的引用，不得编造来源。首次调用会自动构建本地索引。"
        "注意：embedding 为本地字面特征，不是语义模型"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索问题或关键词"},
            "top_k": {"type": "integer", "description": "返回证据条数，默认 5"},
            "reindex": {"type": "boolean", "description": "是否强制重建索引，默认 false"},
        },
        "required": ["query"],
    },
)
def vector_search(query: str, top_k: int = 5, reindex: bool = False) -> ToolResult:
    store = get_store()
    try:
        if reindex or store.stats()["chunks"] == 0:
            store.index_directory()
        citations = store.search(query, top_k=top_k)
    except Exception as exc:  # noqa: BLE001 - 工具层统一收敛异常
        return ToolResult.failure("vector_search", f"{type(exc).__name__}: {exc}")
    stats = store.stats()
    head = (
        f"向量检索「{query}」：backend={stats['backend']}，"
        f"embedder={stats['embedder']}（非语义模型），索引片段 {stats['chunks']} 条，"
        f"命中 {len(citations)} 条。\n"
    )
    if not citations:
        return ToolResult(
            ok=True, content=head + "未命中证据。请勿编造来源。",
            tool_name="vector_search", meta={"hits": 0, "backend": stats["backend"]},
        )
    body = store.citation_block(citations)
    content = head + "\n" + body
    return ToolResult(
        ok=True, content=content, tool_name="vector_search",
        meta={
            "hits": len(citations), "backend": stats["backend"],
            "is_real_chroma": stats["is_real_chroma"],
            "sources": [c.source for c in citations],
            "citations": [c.to_dict() for c in citations],
        },
    )


@tool(
    name="verify_quote",
    description=(
        "核验一段引文是否真实存在于指定源文件中，用于拦截模型编造的引用。"
        "传入来源相对路径与引文原文，返回是否命中"
    ),
    parameters={
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "来源文件相对项目根的路径，如 data/inbox/xxx.txt"},
            "quote": {"type": "string", "description": "待核验的引文原文（可含换行）"},
        },
        "required": ["source", "quote"],
    },
)
def verify_quote(source: str, quote: str) -> ToolResult:
    path = _safe_source(source)
    if path is None:
        return ToolResult.failure("verify_quote", f"源文件不存在或路径越界：{source}")
    if not (quote or "").strip():
        return ToolResult.failure("verify_quote", "引文为空")
    try:
        full = _extract_text(path, path.suffix.lower())
    except Exception as exc:  # noqa: BLE001
        return ToolResult.failure("verify_quote", f"源文件解析失败：{type(exc).__name__}: {exc}")
    squash = lambda s: re.sub(r"\s+", "", s or "")  # noqa: E731
    found = squash(quote) in squash(full)
    preview = quote.replace("\n", " / ")[:120]
    content = (
        f"引文核验：{'通过（原文存在于源文件）' if found else '未通过（源文件中找不到该引文）'}\n"
        f"来源：{source}\n引文：{preview}"
    )
    return ToolResult(
        ok=True, content=content, tool_name="verify_quote",
        meta={"found": found, "source": source, "quote_chars": len(quote)},
    )


@tool(
    name="vector_index_status",
    description="查看本地向量库状态：真实后端、embedding 类型、索引片段数、Chroma 是否可用",
    parameters={"type": "object", "properties": {}, "required": []},
)
def vector_index_status() -> ToolResult:
    stats = get_store().stats()
    lines = [
        f"backend = {stats['backend']}（is_real_chroma = {stats['is_real_chroma']}）",
        f"chromadb_available = {stats['chromadb_available']}",
        f"embedder = {stats['embedder']}；embedding_is_semantic = {stats['embedding_is_semantic']}",
        f"indexed_chunks = {stats['chunks']}",
        f"persist_dir = {stats['persist_dir']}",
    ]
    if stats["init_error"]:
        lines.append(f"Chroma 初始化失败原因：{stats['init_error']}（已降级到标准库余弦索引）")
    lines.append("说明：非语义 embedding 只编码字面特征，同义改写召回不到；不能对外称为语义 RAG。")
    return ToolResult(ok=True, content="\n".join(lines), tool_name="vector_index_status", meta=stats)


RAG_TOOLS = [vector_search, verify_quote, vector_index_status]
