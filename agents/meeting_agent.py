"""Meeting Agent（方案 4.2）。

链路：录音转写（或已有转写稿）→ Map-Reduce 摘要 → 待办提取 → 纪要数据。

Map-Reduce 是端侧 7B 的务实选择：长文本一次塞进 8K 上下文会丢细节，
先分段摘要再合并，比直接截断的摘要质量明显更稳。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from core.llm_client import OllamaError
from core.output_parser import OutputParseError, extract_json_array
from core.tool_registry import ToolRegistry

MAP_PROMPT = """你在整理一段会议录音转写稿的片段。请只保留有信息量的内容，输出该片段的要点摘要。

要求：
- 保留：讨论的议题、形成的决定、提到的数字/时间/人名、分配的任务。
- 丢弃：寒暄、重复、口头禅、无意义语气词。
- 用简洁的中文条目列出，不要编造原文没有的内容。
- 直接输出摘要，不要任何前言。

片段内容：
{chunk}"""

REDUCE_PROMPT = """下面是同一场会议不同时间段的要点摘要。请合并成一份完整的会议主要内容。

要求：
- 按议题归类合并，同一件事在不同片段出现时只保留一次，取信息更完整的表述。
- 明确区分「讨论过程」和「已形成的决定」。
- 保留待办事项里提到的人名和时限。
- 用连贯的中文段落输出，可直接放进正式会议纪要。
- 不要编造摘要中没有的内容，不要加标题。

分段摘要：
{summaries}"""

TODO_PROMPT = """从会议转写稿里提取待办事项，只输出 JSON 数组。

格式：[{{"who": "责任人", "what": "具体要做的事", "when": "完成时限"}}]

规则：
- 只提取明确分配了责任人和动作的任务，讨论性内容不算。
- 原文没说责任人就填 "未指定"，没说时限就填 "未定期限"，不要猜。
- 没有待办就输出 []。
- 只输出 JSON 数组，不要 markdown 代码块以外的任何文字。

会议内容：
{text}"""


@dataclass
class MeetingResult:
    transcript: str = ""
    summary: str = ""
    todos: list[dict[str, str]] = field(default_factory=list)
    chunk_count: int = 0
    used_map_reduce: bool = False
    errors: list[str] = field(default_factory=list)

    def todos_json(self) -> str:
        return json.dumps(self.todos, ensure_ascii=False)

    @property
    def ok(self) -> bool:
        return bool(self.summary) and not self.errors


def split_text(text: str, chunk_chars: int = 1800, overlap: int = 150) -> list[str]:
    """按段落边界切分，尽量不切断句子；带少量重叠避免边界信息丢失。"""
    text = text.strip()
    if len(text) <= chunk_chars:
        return [text]
    paragraphs = [p.strip() for p in re.split(r"\n+", text) if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if len(current) + len(para) + 1 <= chunk_chars:
            current = f"{current}\n{para}".strip()
            continue
        if current:
            chunks.append(current)
        if len(para) > chunk_chars:
            # 超长段落按句号硬切
            sentences = re.split(r"(?<=[。！？；])", para)
            buf = ""
            for sent in sentences:
                if len(buf) + len(sent) > chunk_chars and buf:
                    chunks.append(buf)
                    buf = ""
                buf += sent
            current = buf
        else:
            current = para
    if current:
        chunks.append(current)
    if overlap and len(chunks) > 1:
        overlapped = []
        for idx, chunk in enumerate(chunks):
            tail = chunks[idx - 1][-overlap:] if idx > 0 else ""
            overlapped.append((tail + "\n" + chunk).strip() if tail else chunk)
        chunks = overlapped
    return chunks


class MeetingAgent:
    def __init__(self, llm, registry: ToolRegistry, chunk_chars: int = 1800) -> None:
        self.llm = llm
        self.registry = registry
        self.chunk_chars = chunk_chars

    def load_transcript(self, source: str) -> tuple[str, str | None]:
        """source 可以是文件路径，也可以是直接传入的文本。"""
        from pathlib import Path

        candidate = Path(source)
        if not candidate.is_absolute():
            candidate = (config_root() / source).resolve()
        if candidate.is_file() and candidate.stat().st_size < 2_000_000:
            result = self.registry.dispatch("read_document", {"path": str(candidate)})
            if result.ok:
                return result.content, None
            return "", result.error
        return source, None

    def process(self, transcript: str) -> MeetingResult:
        result = MeetingResult(transcript=transcript)
        if not transcript.strip():
            result.errors.append("转写稿为空")
            return result

        chunks = split_text(transcript, self.chunk_chars)
        result.chunk_count = len(chunks)

        try:
            if len(chunks) == 1:
                # 短文本跳过 Map，省一次端侧推理（首 Token 延迟优化）
                result.summary = self._llm_text(REDUCE_PROMPT.format(summaries=chunks[0]))
            else:
                result.used_map_reduce = True
                partials = []
                for idx, chunk in enumerate(chunks, 1):
                    partials.append(f"[片段 {idx}]\n{self._llm_text(MAP_PROMPT.format(chunk=chunk))}")
                result.summary = self._llm_text(REDUCE_PROMPT.format(summaries="\n\n".join(partials)))
        except OllamaError as exc:
            result.errors.append(f"摘要生成失败：{exc}")
            return result

        # 待办提取用全文（截断保护：只取前 6000 字）
        todo_input = transcript[:6000]
        try:
            resp = self.llm.chat(
                [{"role": "user", "content": TODO_PROMPT.format(text=todo_input)}],
                temperature=0.0,
                format_json=True,
            )
            raw_items = extract_json_array(resp.content)
            result.todos = [_normalize_todo(item) for item in raw_items if item]
        except (OllamaError, OutputParseError) as exc:
            # 待办提取失败不拖垮整条链路，纪要仍可交付
            result.errors.append(f"待办提取失败：{exc}")
        return result

    def _llm_text(self, prompt: str) -> str:
        resp = self.llm.chat([{"role": "user", "content": prompt}], temperature=0.1)
        return resp.content.strip()


def _normalize_todo(item: Any) -> dict[str, str]:
    if isinstance(item, str):
        return {"who": "未指定", "what": item, "when": "未定期限"}
    if not isinstance(item, dict):
        return {"who": "未指定", "what": str(item), "when": "未定期限"}
    return {
        "who": str(item.get("who") or "未指定").strip(),
        "what": str(item.get("what") or item.get("task") or "").strip(),
        "when": str(item.get("when") or item.get("deadline") or "未定期限").strip(),
    }


def config_root():
    from core.config import config

    return config.project_root
