"""可复现评测素材生成。

所有素材落在 data/inbox/eval_fixtures/（项目根内，不触发文件工具沙箱）。
每次评测前调用 ensure_fixtures() 保证存在；内容固定，便于跨轮次对比。

为什么不用真实业务文件：评测集必须自包含、可重复，且不能夹带真实单位信息。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from core.config import PROJECT_ROOT

FIXTURE_DIR = PROJECT_ROOT / "data" / "inbox" / "eval_fixtures"

POLICY_PARAS = [
    "关于推进端侧智能办公应用建设的通知",
    "各单位：为落实数据不出内网的保密要求，现就端侧智能办公应用建设有关事项通知如下。",
    "一、总体要求。所有涉及内部资料的处理必须在本机完成，禁止调用境外云端大模型接口。",
    "二、技术路线。采用 7B 量化模型 Qwen2.5-7B Q4_K_M 在本地推理，配合多智能体协作完成文档处理、会议纪要与邮件起草。",
    "三、安全边界。邮件发送、文件删除等有副作用的操作须经人工确认，系统仅生成草稿。",
    "四、进度安排。11 月 30 日前完成功能冻结，12 月 15 日前完成国产 CPU 与麒麟操作系统适配验证。",
    "办公室  2026 年 11 月 3 日",
]

SALES_ROWS = [
    ("部门", "10月销售额(万元)", "9月销售额(万元)", "同比(%)"),
    ("综合办", 128.5, 116.2, 10.6),
    ("技术科", 342.0, 301.8, 13.3),
    ("财务科", 96.4, 99.1, -2.7),
    ("后勤处", 74.2, 68.5, 8.3),
    ("合计", 641.1, 585.6, 9.5),
]

MEETING_SHORT = """2026年11月6日 部门例会记录
主持人：张伟  地点：三楼会议室
参会：张伟、李娜、王强、赵敏

张伟：先说端侧模型的事。Qwen2.5-7B 的 Q4_K_M 量化版本已在样机上跑通，首 Token 约 1.2 秒，内存峰值 5.4GB。
李娜：文档格式这块，纪要模板要严格按 GB/T 9704-2012 来做，标题二号小标宋，正文三号仿宋。
王强：我这边负责工具调用层，建议统一走 JSON Schema 做参数校验，避免 7B 模型乱填参数。
张伟：定了，11月30日前功能冻结。李娜负责纪要模板复核，王强负责工具注册表单元测试，都在11月20日前完成。
赵敏：邮件通知还是只出草稿，不自动发送，等人工确认。
"""

PDF_LINES = [
    "INTERNAL TECHNICAL NOTICE 2026-11",
    "Endpoint AI Office Agent - Local Inference Only",
    "Data must not leave the intranet at any time.",
    "Model: Qwen2.5-7B Q4_K_M, running on local PC CPU.",
    "Owner: Office of Information Technology",
]


def _build_docx(path: Path) -> None:
    from docx import Document

    doc = Document()
    for line in POLICY_PARAS:
        doc.add_paragraph(line)
    doc.save(str(path))


def _build_xlsx(path: Path) -> None:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "销售数据"
    for row in SALES_ROWS:
        ws.append(list(row))
    wb.save(str(path))


def _build_pdf(path: Path) -> None:
    """手写最小 PDF 1.4（纯 ASCII 内容），避免引入 reportlab 等额外依赖。

    pypdf 只能读取、不能方便地从文本建页，所以这里直接拼 PDF 对象。
    """
    def esc(text: str) -> str:
        return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    content = "BT /F1 12 Tf 72 720 Td 16 TL\n"
    for line in PDF_LINES:
        content += f"({esc(line)}) Tj T*\n"
    content += "ET\n"
    stream = content.encode("ascii")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"endstream",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode()
    path.write_bytes(bytes(out))


def ensure_fixtures() -> dict[str, Path]:
    """生成（不覆盖已有）评测素材，返回 name -> 绝对路径。"""
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    files: dict[str, Path] = {
        "policy_doc": FIXTURE_DIR / "policy_doc.docx",
        "sales_xlsx": FIXTURE_DIR / "sales_2026_10.xlsx",
        "notice_pdf": FIXTURE_DIR / "internal_notice.pdf",
        "plain_txt": FIXTURE_DIR / "notice_source.txt",
        "meeting_short": FIXTURE_DIR / "meeting_short.txt",
        "meeting_long": FIXTURE_DIR / "meeting_long.txt",
    }
    if not files["policy_doc"].exists():
        _build_docx(files["policy_doc"])
    if not files["sales_xlsx"].exists():
        _build_xlsx(files["sales_xlsx"])
    if not files["notice_pdf"].exists():
        _build_pdf(files["notice_pdf"])
    if not files["plain_txt"].exists():
        files["plain_txt"].write_text(
            "内部通知草稿素材\n1. 端侧推理，数据不出内网。\n2. 邮件只生成草稿，发送须人工确认。\n"
            "3. 11月30日前功能冻结。\n",
            encoding="utf-8",
        )
    if not files["meeting_short"].exists():
        files["meeting_short"].write_text(MEETING_SHORT, encoding="utf-8")
    if not files["meeting_long"].exists():
        src = PROJECT_ROOT / "data" / "inbox" / "sample_meeting.txt"
        if src.exists():
            shutil.copyfile(src, files["meeting_long"])
        else:
            # sample 缺失时退化成长稿：把短稿重复拼接，保证 Map-Reduce 分支仍可触发
            files["meeting_long"].write_text(MEETING_SHORT * 4, encoding="utf-8")
    return files
