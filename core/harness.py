"""Agent Harness 主循环（方案 3.2）。

闭环：System Prompt 构造 → 模型调用（Ollama）→ 输出解析 →
工具执行 → 结果回灌 → 循环，直到模型给出最终回答或步数耗尽。

刻意不依赖 LangChain / AutoGen：整个核心 200 行以内，每步可打断、
可观测、可替换模型后端——信创环境里可控可解释比框架堆叠重要。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from core.config import PROJECT_ROOT
from core.llm_client import LLMResponse, OllamaError
from core.tool_registry import ToolRegistry

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = """你是运行在国产 PC 端、完全离线的办公智能体。
你可以调用工具来读写文档、处理会议录音、起草邮件、操作桌面应用。

工作原则：
1. 先想清楚再动手：一次只调用当前必需的步骤，拿到结果再决定下一步。
2. 所有事实必须来自工具返回，不要编造文件内容、收件人地址或会议结论。
3. 工具返回 [TOOL_ERROR] 时，读懂错误、修正参数后重试；连续失败两次就换方案或如实说明。
4. 涉及发送邮件、删除文件、修改既有文档等有副作用的操作，先向用户复述确认。
5. 任务完成后用简洁的中文给出结论，并列出产出的文件路径。
6. 汇总或转写源数据时，原样保留源数据中的字段名、行标签、部门名和数值，不要改写成同义词。
7. 写工具返回后系统会回读文件真实内容；最终结论只能复述其中确实存在的内容。"""


@dataclass
class StepRecord:
    """一次工具调用的完整记录，用于反思校验、评测和调试回放。"""

    index: int
    tool_name: str
    arguments: dict[str, Any]
    ok: bool
    content: str = ""
    error: str | None = None
    elapsed_ms: int = 0


@dataclass
class RunResult:
    answer: str
    steps: list[StepRecord] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str = "final"  # final | max_steps | error

    @property
    def tool_count(self) -> int:
        return len(self.steps)

    @property
    def failed_steps(self) -> list[StepRecord]:
        return [s for s in self.steps if not s.ok]

    def summary(self) -> str:
        ok = sum(1 for s in self.steps if s.ok)
        return f"stop={self.stop_reason} tools={len(self.steps)} ok={ok} failed={len(self.steps) - ok}"


StepHook = Callable[[StepRecord], None]


class AgentLoop:
    """单个 Agent 的执行循环。Planner 调度多个 AgentLoop 实例完成协作。"""

    def __init__(
        self,
        llm,
        registry: ToolRegistry,
        max_steps: int = 12,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        on_step: StepHook | None = None,
        verify_writes: bool = True,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.max_steps = max_steps
        self.system_prompt = system_prompt
        self.on_step = on_step
        self.verify_writes = verify_writes

    # ------------------------------------------------------------------
    @staticmethod
    def _read_back(result: Any) -> str:
        """写工具成功后回读真实落盘内容，抑制「回答与文件不一致」。

        7B 最常见的失配不是算错，而是把「打算写的内容」当成「已经写的内容」
        来复述（首轮 M02/M04 均如此）。这里由 harness 直接读文件，不经
        registry.dispatch，因此不会污染工具调用序列与工具准确率统计。
        """
        path = (result.meta or {}).get("path")
        if not path:
            return ""
        p = Path(path)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        # 二进制文档由各自工具返回结构化摘要，不在此处引入解析依赖
        if p.suffix.lower() not in {".txt", ".md", ".csv", ".json"}:
            return ""
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        truncated = len(text) > 1500
        if truncated:
            text = text[:1500] + "\n...（回读已截断）"
        try:
            rel = p.relative_to(PROJECT_ROOT).as_posix()
        except ValueError:
            rel = p.name
        return (
            f"\n\n[WRITE_VERIFY] 系统已回读刚写入的文件 `{rel}`，以下是实际落盘内容"
            "（不是你的记忆，不要凭印象复述）：\n"
            f"--- BEGIN ---\n{text}\n--- END ---\n"
            "请核对任务要求的字段名、行标签、落款、收件部门、数值是否确实出现在上面；"
            "若有缺失，先调用写工具补写，再给结论。最终结论只能复述上面实际存在的内容。"
        )

    # ------------------------------------------------------------------
    def run(self, task: str, tool_schemas: list[dict] | None = None, history: list[dict] | None = None) -> RunResult:
        """执行一个任务。history 允许上游（Planner）注入之前的中间结果。"""
        tools = tool_schemas if tool_schemas is not None else self.registry.schemas()
        messages: list[dict[str, Any]] = [{"role": "system", "content": self.system_prompt}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": task})

        steps: list[StepRecord] = []
        empty_retries = 0

        for step_idx in range(1, self.max_steps + 1):
            try:
                resp: LLMResponse = self.llm.chat(messages, tools=tools)
            except OllamaError as exc:
                logger.error("LLM 调用失败：%s", exc)
                return RunResult(answer=f"模型调用失败：{exc}", steps=steps, messages=messages, stop_reason="error")

            if not resp.has_tool_calls:
                answer = resp.content.strip()
                # 7B 偶发返回空 content（首轮 M04 曾 1 步 0 工具直接结束）。
                # 空回答不是「最终结论」，给一次明确的续跑机会，避免把
                # 模型侧的瞬时抖动记成任务失败。
                if not answer and empty_retries < 1:
                    empty_retries += 1
                    messages.append({"role": "assistant", "content": ""})
                    messages.append({
                        "role": "user",
                        "content": "你刚才没有输出任何内容，也没有调用工具。请继续执行任务："
                                   "先调用工具读取所需材料，再按任务要求产出文件并给出结论。",
                    })
                    continue
                # 模型给出最终回答
                return RunResult(answer=answer, steps=steps, messages=messages, stop_reason="final")

            # 记录 assistant 的工具调用意图，再逐个执行并回灌
            messages.append(
                {
                    "role": "assistant",
                    "content": resp.content or "",
                    "tool_calls": [call.to_message() for call in resp.tool_calls],
                }
            )

            for call in resp.tool_calls:
                result = self.registry.dispatch(call.name, call.arguments)
                record = StepRecord(
                    index=len(steps) + 1,
                    tool_name=call.name,
                    arguments=call.arguments,
                    ok=result.ok,
                    content=result.content[:2000],
                    error=result.error,
                    elapsed_ms=result.elapsed_ms,
                )
                steps.append(record)
                if self.on_step:
                    self.on_step(record)
                # 结果回灌：失败也以结构化文本喂回，让模型自我修正
                feed = result.to_message_content()
                if self.verify_writes and result.ok and call.name.startswith("write_"):
                    feed += self._read_back(result)
                messages.append({"role": "tool", "content": feed})

        # 步数耗尽：给模型最后一次不带工具的收尾机会，避免半截结果
        logger.warning("达到最大步数 %d，进入强制收尾", self.max_steps)
        messages.append(
            {
                "role": "user",
                "content": "已达到最大执行步数。请基于目前拿到的全部工具结果，直接给出阶段性结论和已完成的工作，不要再调用工具。",
            }
        )
        try:
            resp = self.llm.chat(messages)
            answer = resp.content.strip()
        except OllamaError as exc:
            answer = f"任务在收尾阶段失败：{exc}"
        return RunResult(answer=answer, steps=steps, messages=messages, stop_reason="max_steps")
