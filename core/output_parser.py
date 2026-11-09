"""结构化输出解析。

真实场景下 7B 本地模型输出 JSON 经常不干净：markdown fence、前置解释、
尾随废话、截断。这里按优先级逐层降级解析，是"把工具调用准确率从 60%
拉到 85%"的第一道防线。
"""

from __future__ import annotations

import json
import re
from typing import Any


class OutputParseError(ValueError):
    pass


# 兼容旧名
PlanParseError = OutputParseError


def extract_json(text: str) -> dict[str, Any]:
    """从模型输出里抠出一个 JSON 对象，逐层降级：
    1. 直接 json.loads
    2. 剥掉 ```json fence
    3. 扫描第一个括号平衡的 {...}（正确处理字符串内的转义与嵌套）
    """
    if not text or not text.strip():
        raise OutputParseError("模型返回为空")

    candidates: list[str] = []
    raw = text.strip()
    candidates.append(raw)

    fenced = re.findall(r"```(?:json)?\s*(.*?)```", raw, flags=re.S | re.I)
    candidates.extend(block.strip() for block in fenced)

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    balanced = _first_balanced_object(raw)
    if balanced:
        try:
            parsed = json.loads(balanced)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    raise OutputParseError(f"无法从模型输出中解析出 JSON：{raw[:300]}")


def _first_balanced_object(text: str) -> str | None:
    """扫描出第一个括号平衡的 JSON 对象，跳过字符串字面量里的括号。"""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]
    return None


def extract_json_array(text: str) -> list[Any]:
    """从模型输出里抠出 JSON 数组（待办清单等场景）。"""
    if not text or not text.strip():
        raise OutputParseError("模型返回为空")
    raw = text.strip()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
    except json.JSONDecodeError:
        pass
    fenced = re.findall(r"```(?:json)?\s*(.*?)```", raw, flags=re.S | re.I)
    for block in fenced:
        try:
            parsed = json.loads(block.strip())
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            continue
    m = re.search(r"\[.*\]", raw, flags=re.S)
    if m:
        try:
            parsed = json.loads(m.group(0))
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass
    raise OutputParseError(f"无法从模型输出中解析出 JSON 数组：{raw[:300]}")
