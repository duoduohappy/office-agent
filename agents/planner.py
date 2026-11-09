"""Planner Agent（规划调度器）。

对应方案 4.1：接收用户原始指令，拆解为子任务序列，调度下游专职 Agent 执行。
两段式 PlanAct：先让模型输出 JSON 计划（不做任何动作），再逐步执行——
计划可审查、可回滚、执行中可根据中间结果动态调整。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.llm_client import LLMResponse
from core.output_parser import OutputParseError, extract_json

PLAN_SYSTEM_PROMPT = """你是一个任务规划器。把用户的办公任务拆成可执行步骤，只输出 JSON。

输出格式（严格遵守）：
{
  "goal": "一句话概括最终要交付的东西",
  "steps": [
    {{"id": 1, "title": "简短动作描述", "agent": "{agents}", "tool_hint": "预期使用的工具名或空字符串", "expected": "这一步的产出是什么"}}
  ]
}

规则：
- 步骤数量控制在 2 到 {max_steps} 步，能合并的合并，太细则调用开销大。
- agent 只能从给定清单里选。
- 不要臆造不存在的工具；不确定就留空字符串。
- 只输出 JSON，不要任何解释文字。"""


class PlanParseError(ValueError):
    pass


@dataclass
class PlanStep:
    id: int
    title: str
    agent: str = "GeneralAgent"
    tool_hint: str = ""
    expected: str = ""
    # 执行期状态（动态重规划用）
    status: str = "pending"  # pending | running | done | failed | skipped
    result_note: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any], fallback_id: int) -> "PlanStep":
        try:
            step_id = int(data.get("id", fallback_id))
        except (TypeError, ValueError):
            step_id = fallback_id
        return cls(
            id=step_id,
            title=str(data.get("title", "")).strip() or f"步骤{fallback_id}",
            agent=str(data.get("agent", "GeneralAgent")).strip() or "GeneralAgent",
            tool_hint=str(data.get("tool_hint", "") or "").strip(),
            expected=str(data.get("expected", "") or "").strip(),
        )

    def mark(self, status: str, note: str = "") -> None:
        self.status = status
        if note:
            self.result_note = note


@dataclass
class Plan:
    goal: str
    steps: list[PlanStep] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any], max_steps: int = 8) -> "Plan":
        raw_steps = data.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise PlanParseError("计划里没有有效的 steps 数组")
        steps: list[PlanStep] = []
        for idx, item in enumerate(raw_steps[:max_steps], start=1):
            if isinstance(item, dict):
                steps.append(PlanStep.from_dict(item, idx))
        if not steps:
            raise PlanParseError("steps 解析后为空")
        return cls(goal=str(data.get("goal", "")).strip(), steps=steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "steps": [
                {
                    "id": s.id,
                    "title": s.title,
                    "agent": s.agent,
                    "tool_hint": s.tool_hint,
                    "expected": s.expected,
                    "status": s.status,
                    "result_note": s.result_note,
                }
                for s in self.steps
            ],
        }

    def to_prompt(self) -> str:
        """把计划压缩成给执行器看的文本（含已完成步骤的中间结果）。"""
        lines = [f"总目标：{self.goal}"] if self.goal else []
        for s in self.steps:
            hint = f"（建议工具：{s.tool_hint}）" if s.tool_hint else ""
            note = f" 已完成：{s.result_note}" if s.result_note else ""
            lines.append(f"{s.id}. [{s.agent}][{s.status}] {s.title}{hint} → 产出：{s.expected or '无明确说明'}{note}")
        return "\n".join(lines)

    @property
    def next_pending(self) -> PlanStep | None:
        for s in self.steps:
            if s.status == "pending":
                return s
        return None


class PlanActPlanner:
    def __init__(self, llm, max_steps: int = 8, temperature: float = 0.1) -> None:
        self.llm = llm
        self.max_steps = max_steps
        self.temperature = temperature

    def plan(self, task: str, available_agents: list[str], context: str = "") -> Plan:
        agents = "|".join(available_agents) if available_agents else "GeneralAgent"
        user_prompt = f"可用 agent：{agents.replace('|', '、')}\n\n用户任务：{task}"
        if context:
            user_prompt += f"\n\n补充上下文：{context}"

        messages = [
            {
                "role": "system",
                "content": PLAN_SYSTEM_PROMPT.format(max_steps=self.max_steps, agents=agents),
            },
            {"role": "user", "content": user_prompt},
        ]
        resp = self.llm.chat(messages, temperature=self.temperature, format_json=True)
        try:
            data = extract_json(resp.content)
        except OutputParseError as exc:
            raise PlanParseError(str(exc)) from exc
        return Plan.from_dict(data, max_steps=self.max_steps)

    def replan(
        self,
        task: str,
        plan: Plan,
        available_agents: list[str],
        reason: str,
    ) -> Plan:
        """动态重规划：带着已执行的中间结果重新出计划（方案 4.1 关键设计）。"""
        context = f"原计划及执行状态：\n{plan.to_prompt()}\n\n需要调整的原因：{reason}"
        return self.plan(task, available_agents, context=context)
