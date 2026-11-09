"""多智能体编排器（方案 2.2 业务闭环 / 4.1 动态规划）。

典型链路：
  用户指令 → Planner 出 JSON 计划 → MeetingAgent（转写/摘要/待办）
  → DocAgent（纪要 docx + 待办 xlsx）→ Reflection 校验
  → EmailAgent（通知草稿 + 收件人查询）→ Reflection 校验
  → DesktopAgent（能力探测，不自动发送）→ Memory 审计

每个环节都产生可检查的中间产物；Reflection 不通过就拦截并给出纠错方向，
不会带着错误往下跑。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from agents.desktop_agent import DesktopAgent
from agents.doc_agent import DocAgent, DocBundle
from agents.email_agent import EmailAgent, EmailDraft
from agents.meeting_agent import MeetingAgent, MeetingResult
from agents.planner import Plan, PlanActPlanner, PlanParseError
from agents.reflection import ReflectionAgent, ReflectionReport
from core.harness import AgentLoop, RunResult
from core.tool_registry import ToolRegistry
from memory.structured_db import StructuredMemory

logger = logging.getLogger(__name__)

AGENT_NAMES = ["MeetingAgent", "DocAgent", "EmailAgent", "DesktopAgent", "GeneralAgent"]


@dataclass
class PipelineResult:
    task: str = ""
    plan: Plan | None = None
    meeting: MeetingResult | None = None
    doc_bundle: DocBundle | None = None
    email_draft: EmailDraft | None = None
    reports: list[ReflectionReport] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    blocked_by_reflection: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.outputs)

    def summary(self) -> str:
        parts = [f"任务：{self.task}"]
        if self.plan:
            done = sum(1 for s in self.plan.steps if s.status == "done")
            parts.append(f"计划：{done}/{len(self.plan.steps)} 步完成")
        if self.meeting:
            parts.append(
                f"会议处理：{self.meeting.chunk_count} 片段，"
                f"{'Map-Reduce' if self.meeting.used_map_reduce else '单段直出'}，"
                f"{len(self.meeting.todos)} 条待办"
            )
        if self.outputs:
            parts.append("产出：" + "、".join(self.outputs))
        for r in self.reports:
            parts.append(r.text().splitlines()[0])
        if self.errors:
            parts.append("错误：" + "；".join(self.errors))
        return "\n".join(parts)


class Orchestrator:
    def __init__(self, llm, registry: ToolRegistry, memory: StructuredMemory, max_plan_steps: int = 8) -> None:
        self.llm = llm
        self.registry = registry
        self.memory = memory
        self.planner = PlanActPlanner(llm, max_steps=max_plan_steps)
        self.meeting_agent = MeetingAgent(llm, registry)
        self.doc_agent = DocAgent(registry)
        self.email_agent = EmailAgent(llm, registry, memory)
        self.desktop_agent = DesktopAgent(audit=memory)
        self.reflection = ReflectionAgent(llm=None, strict=True)

    # ------------------------------------------------------------------
    def run_meeting_pipeline(
        self,
        transcript_source: str,
        *,
        title: str = "部门例会纪要",
        meeting_date: str = "",
        location: str = "",
        host: str = "",
        attendees: str = "",
        recipients_group: str = "全体成员",
        use_plan: bool = True,
    ) -> PipelineResult:
        task = f"把「{transcript_source if len(transcript_source) < 60 else '会议录音转写稿'}」整理成纪要，提取待办，起草会议通知"
        result = PipelineResult(task=task)

        # --- 步骤 1：Planner 出结构化计划 ---
        if use_plan:
            try:
                result.plan = self.planner.plan(task, AGENT_NAMES)
                self._log("plan", "PlanAct", True, {"goal": result.plan.goal, "steps": len(result.plan.steps)})
            except (PlanParseError, Exception) as exc:  # noqa: BLE001
                # 7B 偶发 JSON 不合规时降级：不中断业务链路，如实记录
                result.errors.append(f"规划降级：{type(exc).__name__}: {exc}")
                self._log("plan", "PlanAct", False, {"error": str(exc)})
                result.plan = Plan(goal=task, steps=[])

        # --- 步骤 2：MeetingAgent ---
        transcript, load_err = self.meeting_agent.load_transcript(transcript_source)
        if load_err:
            result.errors.append(f"转写稿加载失败：{load_err}")
            return result
        meeting = self.meeting_agent.process(transcript)
        result.meeting = meeting
        self._mark_plan(result.plan, "MeetingAgent", "done" if meeting.ok else "failed", f"{len(meeting.todos)} 条待办")
        self._log("meeting", "MeetingAgent", meeting.ok, {"chunks": meeting.chunk_count})
        if meeting.errors and not meeting.summary:
            result.errors.extend(meeting.errors)
            return result

        # --- 步骤 3：DocAgent 生成纪要 + 待办清单 ---
        bundle = self.doc_agent.write_minutes(
            title=title, summary=meeting.summary, todos=meeting.todos,
            meeting_date=meeting_date, location=location, host=host, attendees=attendees,
        )
        result.doc_bundle = bundle
        result.outputs.extend(bundle.outputs)
        self._mark_plan(result.plan, "DocAgent", "done" if bundle.ok else "failed", "、".join(bundle.outputs))
        self._log("minutes", "DocAgent", bundle.ok, {"outputs": bundle.outputs})
        result.errors.extend(bundle.errors)
        if not bundle.ok:
            return result

        # --- 步骤 4：Reflection 校验纪要 ---
        report = self.reflection.check_minutes(
            summary=meeting.summary, todos=meeting.todos, title=title,
            expected_todo_min=1, source_text=transcript,
        )
        result.reports.append(report)
        self._log("reflection:minutes", "ReflectionAgent", report.passed, {"issues": len(report.issues)})
        if not report.passed:
            result.blocked_by_reflection = True
            result.errors.append("纪要校验未通过，已拦截后续邮件流程：" + "；".join(i.detail for i in report.errors))
            # 动态重规划：带着失败原因重出计划（方案 4.1 关键设计）
            if use_plan and result.plan and result.plan.steps:
                try:
                    result.plan = self.planner.replan(task, result.plan, AGENT_NAMES, "纪要校验未通过，需修正摘要/待办")
                except Exception:  # noqa: BLE001
                    pass
            return result

        # --- 步骤 5：EmailAgent 起草通知 + 查收件人 ---
        draft = self.email_agent.compose(meeting.summary, meeting.todos, group=recipients_group)
        draft = self.email_agent.save(draft)
        result.email_draft = draft
        if draft.path:
            result.outputs.append(draft.path)
        self._mark_plan(result.plan, "EmailAgent", "done" if draft.ok else "failed", draft.path)
        self._log("email", "EmailAgent", draft.ok, {"recipients": len(draft.recipients)})
        result.errors.extend(draft.errors)

        # --- 步骤 6：Reflection 校验邮件（发送前最后一道闸） ---
        email_report = self.reflection.check_email(
            subject=draft.subject, body=draft.body, recipients=draft.recipients,
            expected_recipients=1,
        )
        result.reports.append(email_report)
        self._log("reflection:email", "ReflectionAgent", email_report.passed, {"issues": len(email_report.issues)})
        if not email_report.passed:
            result.blocked_by_reflection = True
            result.errors.append("邮件校验未通过，禁止进入发送环节：" + "；".join(i.detail for i in email_report.errors))

        # --- 步骤 7：DesktopAgent 只做能力探测，不实际发送 ---
        desktop = self.desktop_agent.execute("foxmail.exe", "send_email", path="")
        self._mark_plan(result.plan, "DesktopAgent", "done" if desktop.ok else "blocked", desktop.detail or "；".join(desktop.errors))
        self._log("desktop", "DesktopAgent", desktop.ok, {"backend": self.desktop_agent.ui_backend()})
        return result

    # ------------------------------------------------------------------
    def run_generic(self, task: str) -> RunResult:
        """不属于会议闭环的通用任务：PlanAct 计划 + 单 AgentLoop 执行。"""
        try:
            plan = self.planner.plan(task, AGENT_NAMES)
        except Exception as exc:  # noqa: BLE001
            logger.warning("规划失败，退化为无计划执行：%s", exc)
            plan = None
        prompt_task = f"{task}\n\n参考计划（可按需调整）：\n{plan.to_prompt()}" if plan else task
        loop = AgentLoop(self.llm, self.registry)
        return loop.run(prompt_task)

    # ------------------------------------------------------------------
    @staticmethod
    def _mark_plan(plan: Plan | None, agent: str, status: str, note: str) -> None:
        if not plan:
            return
        for step in plan.steps:
            if step.agent == agent and step.status == "pending":
                step.mark(status, note)
                return

    def _log(self, stage: str, agent: str, ok: bool, detail: dict[str, Any]) -> None:
        self.memory.log_step(stage, stage, detail, ok, 0, agent=agent)
