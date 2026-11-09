"""Doc Agent（方案 4.3）。

薄封装：把 MeetingAgent 产出的结构化数据落到政务公文格式 Word + 待办 Excel。
真正的文档解析逻辑在 tools/doc_tools.py，Agent 只负责编排，保持可测试。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.tool_registry import ToolRegistry
from tools import doc_tools


@dataclass
class DocBundle:
    minutes_path: str = ""
    todos_path: str = ""
    outputs: list[str] = None  # type: ignore[assignment]
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.outputs = self.outputs or []
        self.errors = self.errors or []

    @property
    def ok(self) -> bool:
        return bool(self.minutes_path) and not self.errors


class DocAgent:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def write_minutes(
        self,
        title: str,
        summary: str,
        todos: list[dict[str, str]],
        meeting_date: str = "",
        location: str = "",
        host: str = "",
        attendees: str = "",
        out_dir: str = "data/outbox",
    ) -> DocBundle:
        bundle = DocBundle()
        todos_json = json.dumps(todos, ensure_ascii=False)
        doc_path = f"{out_dir.rstrip('/')}/会议纪要.docx"
        xlsx_path = f"{out_dir.rstrip('/')}/待办清单.xlsx"

        r1 = self.registry.dispatch(
            "write_minutes_docx",
            {
                "path": doc_path,
                "title": title,
                "meeting_date": meeting_date,
                "location": location,
                "host": host,
                "attendees": attendees,
                "summary": summary,
                "todos": todos_json,
            },
        )
        if r1.ok:
            bundle.minutes_path = r1.meta.get("path", doc_path)
            bundle.outputs.append(bundle.minutes_path)
        else:
            bundle.errors.append(f"纪要生成失败：{r1.error}")

        r2 = self.registry.dispatch("write_todos_xlsx", {"path": xlsx_path, "todos": todos_json})
        if r2.ok:
            bundle.todos_path = r2.meta.get("path", xlsx_path)
            bundle.outputs.append(bundle.todos_path)
        else:
            bundle.errors.append(f"待办清单生成失败：{r2.error}")
        return bundle

    def read_any(self, path: str) -> tuple[str, str | None]:
        result = self.registry.dispatch("read_document", {"path": path})
        return (result.content, None) if result.ok else ("", result.error)
