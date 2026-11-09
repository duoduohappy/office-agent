"""Reflection Agent（方案 4.7）。

在关键节点（文档生成后、邮件发送前）做质量检查，拦截明显错误。
采用「确定性规则优先 + LLM 兜底」：格式、字段、收件人、敏感信息这类
能写死规则的绝不交给 7B 模型判断——既省推理又不会漏。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

SENSITIVE_PATTERNS = [
    (r"身份证[号号码]*[:：]?\s*\d{15,18}", "疑似身份证号"),
    (r"(?<!\d)1[3-9]\d{9}(?!\d)", "疑似手机号"),
    (r"密[级码]|机密|绝密|秘密", "涉密关键词"),
    (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "外部邮箱地址"),
]


@dataclass
class Issue:
    level: str  # error | warn
    rule: str
    detail: str


@dataclass
class ReflectionReport:
    passed: bool
    issues: list[Issue] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    def add(self, level: str, rule: str, detail: str) -> None:
        self.issues.append(Issue(level, rule, detail))

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    def text(self) -> str:
        lines = [f"校验{'通过' if self.passed else '未通过'}（{len(self.issues)} 项）"]
        for i in self.issues:
            lines.append(f"  [{i.level.upper()}] {i.rule}：{i.detail}")
        for s in self.suggestions:
            lines.append(f"  [建议] {s}")
        return "\n".join(lines)


class ReflectionAgent:
    def __init__(self, llm=None, strict: bool = True) -> None:
        self.llm = llm
        self.strict = strict

    def check_minutes(
        self,
        *,
        summary: str,
        todos: list[dict[str, str]],
        title: str = "",
        expected_todo_min: int = 1,
        source_text: str = "",
    ) -> ReflectionReport:
        report = ReflectionReport(passed=True)

        if not summary or len(summary.strip()) < 30:
            report.add("error", "格式校验", "会议主要内容过短或为空，疑似摘要生成失败")
        if not title.strip():
            report.add("warn", "格式校验", "纪要缺少标题")
        if len(todos) < expected_todo_min:
            report.add("warn", "逻辑校验", f"待办事项仅 {len(todos)} 条，请确认是否遗漏")
        for idx, todo in enumerate(todos, 1):
            what = str(todo.get("what", "")).strip()
            if not what or len(what) < 4:
                report.add("error", "逻辑校验", f"第 {idx} 条待办描述为空或过短")
            if todo.get("who") in {"", "未指定"}:
                report.add("warn", "逻辑校验", f"第 {idx} 条待办未明确责任人：{what[:30]}")
        # 事实一致性：待办里的责任人应能在原文找到（避免模型编造人名）
        if source_text:
            for todo in todos:
                who = str(todo.get("who", "")).strip()
                if who and who != "未指定" and who not in source_text:
                    report.add("warn", "事实校验", f"责任人「{who}」未在原始转写稿中出现，可能由模型推断")

        self._check_sensitive(summary + "\n" + "\n".join(str(t) for t in todos), report, "纪要内容")
        report.passed = not report.errors if self.strict else True
        return report

    def check_email(
        self,
        *,
        subject: str,
        body: str,
        recipients: list[str],
        expected_recipients: int = 1,
    ) -> ReflectionReport:
        report = ReflectionReport(passed=True)
        if not subject.strip():
            report.add("error", "格式校验", "邮件主题为空")
        if not body.strip() or len(body.strip()) < 40:
            report.add("error", "格式校验", "邮件正文过短或为空")
        if len(recipients) < expected_recipients:
            report.add("error", "逻辑校验", f"收件人仅 {len(recipients)} 位，少于预期 {expected_recipients} 位")
        invalid = [r for r in recipients if "@" not in r]
        if invalid:
            report.add("error", "逻辑校验", f"以下收件人没有有效邮箱地址：{invalid}")
        for phrase in ("测试邮件", "TODO", "占位符xxx", "lorem ipsum"):
            if phrase.lower() in body.lower():
                report.add("warn", "格式校验", f"正文含疑似占位内容：{phrase}")
        self._check_sensitive(body, report, "邮件正文")
        report.suggestions.append("发送属于有副作用操作，需人工确认收件人后再执行")
        report.passed = not report.errors if self.strict else True
        return report

    @staticmethod
    def _check_sensitive(text: str, report: ReflectionReport, scope: str) -> None:
        for pattern, label in SENSITIVE_PATTERNS:
            hits = re.findall(pattern, text)
            if hits:
                report.add("warn", "安全校验", f"{scope}含{label}（{len(hits)} 处），请确认是否允许外发")
