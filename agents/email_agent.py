"""Email Agent（方案 4.4）。

职责：邮件草稿撰写、收件人查询、发送前准备。
安全边界：只生成草稿文件（data/outbox/notices/），不实际发送——
真实发送属于有副作用操作，按方案 4.7 由 Reflection 校验后交人工确认或 DesktopAgent。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.tool_registry import ToolRegistry
from memory.structured_db import StructuredMemory

NOTICE_PROMPT = """根据下面的会议要点，起草一份正式的会议通知邮件正文。

要求：
- 公文语气，简洁庄重，不用营销话术、不用感叹号。
- 包含：会议/事项背景、核心内容、对收件人的要求、时间节点。
- 列出会议纪要和待办清单作为附件提示。
- 落款用「办公室」，日期留「（日期）」占位。
- 直接输出正文，不要输出主题行，不要 markdown。

会议要点：
{summary}

待办事项（供正文引用，不必全列）：
{todos}"""


@dataclass
class EmailDraft:
    subject: str = ""
    body: str = ""
    recipients: list[str] = field(default_factory=list)
    path: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.body) and not self.errors


class EmailAgent:
    def __init__(self, llm, registry: ToolRegistry, memory: StructuredMemory) -> None:
        self.llm = llm
        self.registry = registry
        self.memory = memory

    def resolve_recipients(self, group: str) -> list[str]:
        rows = self.memory.resolve_group(group)
        return [f"{r['name']} <{r['email']}>" for r in rows]

    def compose(self, summary: str, todos: list[dict[str, str]], group: str = "全体成员") -> EmailDraft:
        draft = EmailDraft(subject="关于部门例会纪要及待办事项落实的通知")
        draft.recipients = self.resolve_recipients(group)
        todo_text = "\n".join(
            f"- {t.get('who', '未指定')}：{t.get('what', '')}（{t.get('when', '未定期限')}）"
            for t in todos[:10]
        ) or "（无）"
        try:
            resp = self.llm.chat(
                [{"role": "user", "content": NOTICE_PROMPT.format(summary=summary, todos=todo_text)}],
                temperature=0.3,
            )
            draft.body = resp.content.strip()
        except Exception as exc:  # noqa: BLE001
            draft.errors.append(f"草稿生成失败：{type(exc).__name__}: {exc}")
            return draft
        return draft

    def save(self, draft: EmailDraft, out_path: str = "data/outbox/notices/会议通知.txt") -> EmailDraft:
        result = self.registry.dispatch(
            "write_notice_txt",
            {
                "path": out_path,
                "subject": draft.subject,
                "body": draft.body,
                "recipients": "、".join(draft.recipients),
            },
        )
        if result.ok:
            draft.path = result.meta.get("path", out_path)
        else:
            draft.errors.append(f"草稿保存失败：{result.error}")
        return draft
