"""本地知识检索（端侧轻量检索层，tools/knowledge_tools.py）。

不引入 embedding 模型 / 向量库：端侧只有 7B 主模型，再塞一个向量模型会把
内存与显存预算吃穿。这里用纯标准库实现「中文 bigram 倒排 + 片段级打分」，
对 data/inbox 下的文档做检索，返回**带来源文件路径**的证据片段，供模型
在回答里标注引用出处。

能力边界（必须在报告里如实说明）：
- 这是关键词/字面相关度检索，不是语义向量检索；
- 不做跨文档实体消歧，只做完全相同的片段去重；
- 未接入 citations 的结构化溯源链，来源标注依赖模型复述工具返回的路径。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path

from core.config import config
from core.tool_registry import ToolResult, tool
from tools.doc_tools import _extract_text

SUPPORTED = {".txt", ".md", ".csv", ".json", ".docx", ".xlsx", ".pptx", ".pdf"}
_CHUNK_CHARS = 320
_HAN = re.compile(r"[\u4e00-\u9fff]+")
_LATIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
# 只认中文年月日 / ISO 日期，避免把 "Qwen2.5-7B" 里的 "5-7" 当成时间线索
_DATE = re.compile(
    r"(?:20\d{2}\s*年\s*)?\d{1,2}\s*月\s*\d{1,2}\s*日|20\d{2}-\d{1,2}-\d{1,2}"
)


def _terms(text: str) -> list[str]:
    """中文按字 + 相邻二元切分，英文数字按词切分；不做分词器依赖。"""
    lowered = text.lower()
    out = list(_LATIN.findall(lowered))
    for seg in _HAN.findall(lowered):
        out.extend(seg)
        out.extend(seg[i : i + 2] for i in range(len(seg) - 1))
    return out


def _chunks(text: str) -> list[str]:
    """按行聚合为不超过 _CHUNK_CHARS 的片段，尽量不切断段落。"""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    chunks: list[str] = []
    buf = ""
    for line in lines:
        if buf and len(buf) + len(line) + 1 > _CHUNK_CHARS:
            chunks.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        chunks.append(buf)
    return chunks


def _score(query_terms: Counter, chunk: str, df: Counter, n_docs: int) -> float:
    """简化的 BM25：字面命中即给分，罕见词权重更高。"""
    tf = Counter(_terms(chunk))
    if not tf:
        return 0.0
    length_norm = 1.0 - min(0.35, len(chunk) / 2000.0)
    total = 0.0
    for term, qtf in query_terms.items():
        hit = tf.get(term, 0)
        if not hit:
            continue
        idf = math.log(1.0 + (n_docs - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5))
        total += qtf * idf * (1.0 + math.log(hit)) * (2.0 if len(term) > 1 else 1.0)
    return round(total * length_norm, 3)


def _collect(inbox: Path) -> list[tuple[str, str]]:
    """返回 (来源相对路径, 片段文本)。解析失败的文件跳过并留痕，不中断检索。"""
    docs: list[tuple[str, str]] = []
    if not inbox.is_dir():
        return docs
    for path in sorted(inbox.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED:
            continue
        if any(part.startswith(".") for part in path.parts):
            continue
        try:
            text = _extract_text(path, path.suffix.lower())
        except Exception:  # noqa: BLE001 - 单个损坏文档不应让整次检索失败
            continue
        if not text.strip():
            continue
        try:
            rel = path.relative_to(config.project_root).as_posix()
        except ValueError:
            rel = path.as_posix()
        for chunk in _chunks(text):
            docs.append((rel, chunk))
    return docs


@tool(
    name="search_knowledge_base",
    description=(
        "在本地知识库 data/inbox 中检索与问题相关的段落，返回带来源文件路径的证据片段。"
        "用于汇报提纲、政策问答、跨文档检索。回答时必须标注每条材料的来源文件与引用出处"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索问题或关键词"},
            "top_k": {"type": "integer", "description": "返回片段数，默认 5"},
        },
        "required": ["query"],
    },
)
def search_knowledge_base(query: str, top_k: int = 5) -> ToolResult:
    try:
        k = max(1, min(int(top_k), 12))
    except (TypeError, ValueError):
        k = 5
    docs = _collect(config.inbox_dir)
    if not docs:
        return ToolResult.failure("search_knowledge_base", "data/inbox 下没有可解析的文档")

    df: Counter = Counter()
    for _, chunk in docs:
        df.update(set(_terms(chunk)))

    q = Counter(_terms(query))
    if not q:
        return ToolResult.failure("search_knowledge_base", "检索词为空")

    scored: list[tuple[float, str, str]] = []
    seen: set[str] = set()
    for rel, chunk in docs:
        key = re.sub(r"\s+", "", chunk)
        if key in seen:  # 跨文档重复片段去重（方案 8.1 C05 要求）
            continue
        seen.add(key)
        s = _score(q, chunk, df, len(docs))
        if s > 0:
            scored.append((s, rel, chunk))
    scored.sort(key=lambda x: (-x[0], x[1]))

    if not scored:
        return ToolResult(
            ok=True,
            content=f"检索「{query}」未命中相关片段。知识库共 {len(docs)} 个片段。",
            tool_name="search_knowledge_base",
            meta={"hits": 0, "corpus_chunks": len(docs)},
        )

    lines = [f"检索「{query}」命中 {len(scored)} 个片段，返回前 {min(k, len(scored))} 个：", ""]
    for idx, (score, rel, chunk) in enumerate(scored[:k], 1):
        dates = _DATE.findall(chunk)
        date_hint = f"  时间线索：{'、'.join(dict.fromkeys(dates[:3]))}" if dates else ""
        lines.append(f"[{idx}] 来源：{rel}  （相关度 {score}）{date_hint}")
        lines.append(chunk)
        lines.append("")
    lines.append("引用要求：回答中逐条标注「来源：文件路径」，并写明引用出处；不要编造未出现在上面的文件。")
    content = "\n".join(lines)
    return ToolResult(
        ok=True,
        content=content,
        tool_name="search_knowledge_base",
        meta={"hits": len(scored), "returned": min(k, len(scored)), "corpus_chunks": len(docs)},
    )


KNOWLEDGE_TOOLS = [search_knowledge_base]
