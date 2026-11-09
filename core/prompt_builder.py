"""Prompt 构造器（方案 core/prompt_builder.py）。

System Prompt = 角色定义 + 工具清单 + 记忆摘要，集中管理便于做
A/B 对比实验（评测里的 prompt 工程数据支撑）。
"""

from __future__ import annotations

from core.tool_registry import ToolRegistry

BASE_TEMPLATE = """{role}

你可以使用以下工具：
{tools}

{rules}"""

DEFAULT_RULES = """工作原则：
1. 先想清楚再动手，一次只调用当前必需的步骤。
2. 所有事实必须来自工具返回，不要编造内容。
3. 工具返回 [TOOL_ERROR] 时，读懂错误、修正参数后重试；连续失败两次就换方案或如实说明。
4. 有副作用的操作（发邮件、删文件、改既有文档）先向用户复述确认。
5. 完成后用简洁中文给结论，并列出产出文件路径。"""

AGENT_ROLES: dict[str, str] = {
    "MeetingAgent": "你是会议处理专员，负责录音转写、会议摘要、待办事项提取，产出符合政务公文规范的会议纪要。",
    "DocAgent": "你是文档处理专员，负责 Word/Excel/PPT/PDF 的读取、编辑、格式转换和内容提取。",
    "EmailAgent": "你是邮件处理专员，负责邮件草稿撰写、收件人查询和邮件发送前的准备。",
    "DesktopAgent": "你是桌面操作专员，负责通过 GUI 自动化操作邮件客户端、文件管理器、浏览器等白名单应用。",
    "GeneralAgent": "你是通用办公助手，处理不归属于其他专员的办公任务。",
}


def build_system_prompt(
    agent_name: str = "GeneralAgent",
    registry: ToolRegistry | None = None,
    memory_summary: str = "",
    extra_rules: str = "",
) -> str:
    role = AGENT_ROLES.get(agent_name, AGENT_ROLES["GeneralAgent"])
    tools = registry.describe() if registry else "（本任务不需要工具）"
    rules = DEFAULT_RULES + ("\n" + extra_rules if extra_rules else "")
    if memory_summary:
        rules += f"\n\n历史记忆摘要（供参考，可能过时）：\n{memory_summary}"
    return BASE_TEMPLATE.format(role=role, tools=tools, rules=rules)
