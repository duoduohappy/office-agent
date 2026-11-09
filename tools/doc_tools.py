"""文档工具集（方案 4.3 Doc Agent，tools/doc_tools.py）。

支持 Word / Excel / PPT / PDF / TXT 五类格式的读取，
以及政务公文格式会议纪要和待办清单的生成。

格式依据 GB/T 9704-2012《党政机关公文格式》：
标题二号小标宋、正文三号仿宋、行距 28 磅、页边距上 3.7 / 下 3.5 / 左 2.8 / 右 2.6 cm。
这些细节是政企客户验收时最常被挑刺的地方。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.config import config
from core.tool_registry import ToolResult, tool

SANDBOX = config.project_root.resolve()

# 公文通知必须有发文机关署名（GB/T 9704 亦要求落款）。工具层给默认值，
# 避免 7B 漏传 signature 时产出「无落款的半成品通知」。
DEFAULT_NOTICE_SIGNATURE = "办公室"


def _resolve(path: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = SANDBOX / p
    resolved = p.resolve()
    if SANDBOX != resolved and SANDBOX not in resolved.parents:
        raise PermissionError(f"路径越界，只允许访问 {SANDBOX} 内的文件：{path}")
    return resolved


def _set_font(run, name_ascii: str = "Times New Roman", name_east: str = "仿宋_GB2312", size_pt: float = 16, bold: bool = False) -> None:
    """python-docx 默认不设置 eastAsia 字体会导致中文回退成宋体，必须显式写 XML。

    阶段 3：字体族名经 HAL.FontResolver 映射。Windows 上返回值与适配前
    完全一致（保持两轮评测的公文版式可复现）；麒麟/UOS 上按 fc-list 结果
    选国产字体，未安装时保留 OOXML 逻辑字体名并如实标注。
    """
    from adapt.hal import get_hal
    from docx.oxml.ns import qn
    from docx.shared import Pt

    fonts = get_hal().fonts
    name_ascii = fonts.resolve(name_ascii)[0]
    name_east = fonts.resolve(name_east)[0]

    run.font.name = name_ascii
    run.font.size = Pt(size_pt)
    run.bold = bold
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.append(rfonts)
    rfonts.set(qn("w:eastAsia"), name_east)


def _set_page(section) -> None:
    from docx.shared import Cm

    section.top_margin = Cm(3.7)
    section.bottom_margin = Cm(3.5)
    section.left_margin = Cm(2.8)
    section.right_margin = Cm(2.6)


def _set_line_spacing(paragraph, pt: float = 28) -> None:
    from docx.shared import Pt

    paragraph.paragraph_format.line_spacing = Pt(pt)
    paragraph.paragraph_format.space_after = Pt(0)


# ---------------------------------------------------------------- 读取
@tool(
    name="read_document",
    description="读取 Word/Excel/PPT/PDF/TXT 文档内容并转为纯文本。政务文档检索和 RAG 的入口",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文档路径，支持 .docx .xlsx .pptx .pdf .txt .md .csv .json"},
            "max_chars": {"type": "integer", "description": "最多返回字符数，默认 12000"},
        },
        "required": ["path"],
    },
)
def read_document(path: str, max_chars: int = 12000) -> ToolResult:
    resolved = _resolve(path)
    if not resolved.is_file():
        return ToolResult.failure("read_document", f"文件不存在：{path}")
    suffix = resolved.suffix.lower()
    try:
        text = _extract_text(resolved, suffix)
    except ImportError as exc:
        return ToolResult.failure("read_document", f"缺少解析依赖：{exc}")
    except Exception as exc:  # noqa: BLE001
        return ToolResult.failure("read_document", f"{type(exc).__name__}: {exc}")
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars] + f"\n...（已截断，原文 {len(text)} 字符）"
    return ToolResult(
        ok=True, content=text, tool_name="read_document",
        meta={"path": str(resolved), "format": suffix, "truncated": truncated},
    )


def _extract_text(path: Path, suffix: str) -> str:
    if suffix in {".txt", ".md", ".csv", ".json"}:
        return path.read_text(encoding="utf-8", errors="replace")

    if suffix == ".docx":
        from docx import Document

        doc = Document(str(path))
        parts = [p.text for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells]
                if any(cells):
                    parts.append(" | ".join(cells))
        return "\n".join(parts)

    if suffix == ".xlsx":
        from openpyxl import load_workbook

        wb = load_workbook(str(path), read_only=True, data_only=True)
        parts = []
        for ws in wb.worksheets:
            parts.append(f"[工作表] {ws.title}")
            for row in ws.iter_rows(values_only=True):
                vals = [str(v) for v in row if v is not None]
                if vals:
                    parts.append(" | ".join(vals))
        wb.close()
        return "\n".join(parts)

    if suffix == ".pptx":
        from pptx import Presentation

        prs = Presentation(str(path))
        parts = []
        for idx, slide in enumerate(prs.slides, 1):
            parts.append(f"[幻灯片 {idx}]")
            for shape in slide.shapes:
                if shape.has_text_frame and shape.text_frame.text.strip():
                    parts.append(shape.text_frame.text.strip())
        return "\n".join(parts)

    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        parts = []
        for idx, page in enumerate(reader.pages, 1):
            parts.append(f"[第 {idx} 页]\n{page.extract_text() or ''}")
        return "\n".join(parts)

    raise ValueError(f"不支持的格式 {suffix}，支持 Word/Excel/PPT/PDF/TXT")


# ---------------------------------------------------------------- 生成
@tool(
    name="write_minutes_docx",
    description="按政务公文格式（GB/T 9704-2012）生成会议纪要 Word 文档",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "输出路径，建议 data/outbox/xxx.docx"},
            "title": {"type": "string", "description": "会议标题，如「2026年第X次部门例会纪要」"},
            "meeting_date": {"type": "string", "description": "会议日期"},
            "location": {"type": "string", "description": "会议地点"},
            "host": {"type": "string", "description": "主持人"},
            "attendees": {"type": "string", "description": "参会人员，逗号分隔"},
            "summary": {"type": "string", "description": "会议主要内容与议定事项"},
            "todos": {"type": "string", "description": "待办事项 JSON 数组字符串，元素含 who/what/when"},
        },
        "required": ["path", "title", "summary"],
    },
    dangerous=True,
)
def write_minutes_docx(
    path: str,
    title: str,
    summary: str,
    meeting_date: str = "",
    location: str = "",
    host: str = "",
    attendees: str = "",
    todos: str = "[]",
) -> ToolResult:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    resolved = _resolve(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)

    try:
        todo_list = json.loads(todos) if isinstance(todos, str) else todos
        if not isinstance(todo_list, list):
            todo_list = []
    except json.JSONDecodeError:
        todo_list = []

    doc = Document()
    _set_page(doc.sections[0])

    # 标题：二号小标宋，居中
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_after = Pt(18)
    _set_font(p.add_run(title), name_east="方正小标宋简体", size_pt=22)
    _set_line_spacing(p, 32)

    # 会议基本信息
    info_lines = []
    if meeting_date:
        info_lines.append(f"时间：{meeting_date}")
    if location:
        info_lines.append(f"地点：{location}")
    if host:
        info_lines.append(f"主持人：{host}")
    if attendees:
        info_lines.append(f"参会人员：{attendees}")
    for line in info_lines:
        p = doc.add_paragraph()
        _set_font(p.add_run(line), size_pt=16)
        _set_line_spacing(p)

    # 分隔线
    p = doc.add_paragraph()
    _set_font(p.add_run("─" * 28), size_pt=12)
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_line_spacing(p, 20)

    # 一级标题 + 正文
    def add_section(heading: str, body: str) -> None:
        p = doc.add_paragraph()
        _set_font(p.add_run(heading), name_east="黑体", size_pt=16)
        _set_line_spacing(p)
        for para in [s.strip() for s in body.split("\n") if s.strip()]:
            p = doc.add_paragraph()
            p.paragraph_format.first_line_indent = Pt(32)
            _set_font(p.add_run(para), size_pt=16)
            _set_line_spacing(p)

    add_section("一、会议主要内容", summary)

    if todo_list:
        todo_lines = []
        for i, item in enumerate(todo_list, 1):
            if isinstance(item, dict):
                who = item.get("who", "未指定")
                what = item.get("what", "")
                when = item.get("when", "未定期限")
                todo_lines.append(f"{i}. {who}：{what}（完成时限：{when}）")
            else:
                todo_lines.append(f"{i}. {item}")
        add_section("二、待办事项", "\n".join(todo_lines))

    doc.save(str(resolved))
    return ToolResult(
        ok=True,
        content=f"已生成会议纪要：{resolved}（{len(todo_list)} 条待办）",
        tool_name="write_minutes_docx",
        meta={"path": str(resolved), "todo_count": len(todo_list)},
    )


@tool(
    name="write_todos_xlsx",
    description="把待办事项写成 Excel 清单（含责任人/事项/时限/状态）",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "输出路径，建议 data/outbox/xxx.xlsx"},
            "todos": {"type": "string", "description": "待办 JSON 数组字符串，元素含 who/what/when"},
        },
        "required": ["path", "todos"],
    },
    dangerous=True,
)
def write_todos_xlsx(path: str, todos: str) -> ToolResult:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    resolved = _resolve(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    try:
        items = json.loads(todos) if isinstance(todos, str) else todos
        if not isinstance(items, list):
            items = []
    except json.JSONDecodeError as exc:
        return ToolResult.failure("write_todos_xlsx", f"todos 不是合法 JSON：{exc}")

    wb = Workbook()
    ws = wb.active
    ws.title = "待办清单"
    headers = ["序号", "责任人", "事项", "完成时限", "状态"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="404040")
        cell.alignment = Alignment(horizontal="center")
    for idx, item in enumerate(items, 1):
        if isinstance(item, dict):
            ws.append([idx, item.get("who", ""), item.get("what", ""), item.get("when", ""), "待办"])
        else:
            ws.append([idx, "", str(item), "", "待办"])
    for col, width in zip("ABCDE", (8, 14, 52, 18, 12)):
        ws.column_dimensions[col].width = width
    wb.save(str(resolved))
    return ToolResult(
        ok=True, content=f"已生成待办清单：{resolved}（{len(items)} 条）",
        tool_name="write_todos_xlsx", meta={"path": str(resolved), "count": len(items)},
    )


@tool(
    name="write_notice_txt",
    description="起草并保存一份会议通知或邮件正文（纯文本）。通知须含主题、收件人、落款单位与日期",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "输出路径，建议 data/outbox/notices/xxx.txt"},
            "subject": {"type": "string", "description": "通知/邮件主题"},
            "body": {"type": "string", "description": "正文内容"},
            "recipients": {"type": "string", "description": "收件人，逗号分隔（仅记录，不实际发送）"},
            "signature": {"type": "string", "description": "落款单位，按任务指令指定填写，如「办公室」"},
            "department": {"type": "string", "description": "收件部门名称，按任务指令指定填写，如「技术科」"},
        },
        "required": ["path", "subject", "body"],
    },
    dangerous=True,
)
def write_notice_txt(
    path: str,
    subject: str,
    body: str,
    recipients: str = "",
    signature: str = "",
    department: str = "",
) -> ToolResult:
    """通知的格式契约由工具层保证：主题 / 收件对象 / 正文 / 落款。

    generic 路由下 7B 常漏掉落款或收件部门（首轮 M04、M08 的真实原因），
    pipeline 路由则由 EmailAgent 传入。两条路由在这里收敛到同一份完整文档。
    """
    resolved = _resolve(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)

    # 部门名落在收件人行（主题保持模型给定的表述，避免出现「关于X的关于…」）
    recipient_line = recipients or department or "（未填写）"
    if department and department not in recipient_line:
        recipient_line = f"{department}：{recipient_line}"

    lines = [f"主题：{subject}", f"收件人：{recipient_line}", "", body.rstrip()]
    effective_signature = signature or DEFAULT_NOTICE_SIGNATURE
    if effective_signature not in body:
        lines += ["", effective_signature, "（日期）"]
    content = "\n".join(lines) + "\n"
    resolved.write_text(content, encoding="utf-8")
    return ToolResult(
        ok=True, content=f"已保存通知草稿：{resolved}\n---\n{content}",
        tool_name="write_notice_txt", meta={"path": str(resolved)},
    )


DOC_TOOLS = [read_document, write_minutes_docx, write_todos_xlsx, write_notice_txt]
