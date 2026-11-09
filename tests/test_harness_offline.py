"""离线 harness 验证：不依赖 Ollama，用 FakeLLM 驱动完整循环。

验证点：
1. 工具调用 → 执行 → 结果回灌的闭环
2. 工具失败时 [TOOL_ERROR] 结构化回灌，模型自我修正
3. 模型给出纯文本时正确终止
4. 步数耗尽的强制收尾
5. output_parser 的 JSON 降级解析
6. planner 的计划解析与 to_prompt
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.harness import AgentLoop
from core.llm_client import LLMResponse, ToolCall
from core.output_parser import extract_json, extract_json_array
from core.tool_registry import ToolRegistry
from tools.file_tools import FILE_TOOLS

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    mark = "PASS" if cond else "FAIL"
    if cond:
        PASS += 1
    else:
        FAIL += 1
    print(f"[{mark}] {name}" + (f"  {detail}" if detail and not cond else ""))


class FakeLLM:
    """按脚本回放模型响应。"""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = list(responses)
        self.calls: int = 0

    def chat(self, messages, tools=None, temperature=None, format_json=False):
        self.calls += 1
        if not self.responses:
            return LLMResponse(content="（脚本耗尽）")
        return self.responses.pop(0)


def tool_call_resp(name: str, arguments: dict) -> LLMResponse:
    return LLMResponse(content="", tool_calls=[ToolCall(name=name, arguments=arguments)])


def main() -> int:
    # ---------- 1. 工具调用闭环 ----------
    registry = ToolRegistry().extend(FILE_TOOLS)
    llm = FakeLLM(
        [
            tool_call_resp("write_file", {"path": "data/outbox/hello.txt", "content": "你好，信创"}),
            tool_call_resp("read_file", {"path": "data/outbox/hello.txt"}),
            LLMResponse(content="文件已写入并读回验证：你好，信创"),
        ]
    )
    loop = AgentLoop(llm, registry, max_steps=5)
    result = loop.run("写一个文件再读回来")
    check("闭环：3 步后正常终止", result.stop_reason == "final" and result.tool_count == 2)
    check("闭环：两步都成功", all(s.ok for s in result.steps), str([s.error for s in result.steps]))
    check("闭环：read 读到 write 的内容", "你好，信创" in result.steps[1].content)
    check("闭环：最终回答来自最后一条", "读回验证" in result.answer)

    # ---------- 2. 错误回灌与自我修正 ----------
    llm = FakeLLM(
        [
            tool_call_resp("read_file", {"path": "data/inbox/不存在.txt"}),
            tool_call_resp("list_dir", {"path": "data/inbox"}),
            LLMResponse(content="目标文件不存在，inbox 目录当前为空"),
        ]
    )
    loop = AgentLoop(llm, registry, max_steps=5)
    result = loop.run("读一下那个文件")
    check("错误回灌：第一步失败被记录", result.steps[0].ok is False)
    check("错误回灌：错误信息进入 messages", any("[TOOL_ERROR]" in str(m) for m in result.messages))
    check("错误回灌：模型拿到错误后改用 list_dir", result.steps[1].tool_name == "list_dir" and result.steps[1].ok)

    # ---------- 3. 参数校验（缺必填参数 → 结构化错误） ----------
    llm = FakeLLM(
        [
            tool_call_resp("write_file", {"content": "没有路径"}),
            LLMResponse(content="需要提供 path 参数，我重新来"),
        ]
    )
    result = AgentLoop(llm, registry, max_steps=3).run("写文件")
    check("参数校验：缺必填参数返回 TOOL_ERROR", result.steps[0].ok is False and "path" in (result.steps[0].error or ""))

    # ---------- 4. 步数耗尽强制收尾 ----------
    endless = [tool_call_resp("list_dir", {"path": "."}) for _ in range(4)]
    endless.append(LLMResponse(content="阶段性结论"))
    llm = FakeLLM(endless)
    result = AgentLoop(llm, registry, max_steps=4).run("无限列目录")
    check("步数控制：4 步后停止并收尾", result.stop_reason == "max_steps" and result.tool_count == 4)
    check("步数控制：收尾回答非空", "阶段性结论" in result.answer)

    # ---------- 5. 沙箱越界拦截 ----------
    llm = FakeLLM(
        [
            tool_call_resp("read_file", {"path": "C:/Windows/system32/config/SAM"}),
            LLMResponse(content="无法读取系统目录"),
        ]
    )
    result = AgentLoop(llm, registry, max_steps=3).run("读系统文件")
    check("安全边界：沙箱外路径被拒绝", result.steps[0].ok is False and "越界" in (result.steps[0].error or ""))

    # ---------- 6. output_parser 降级解析 ----------
    check("parser：纯 JSON", extract_json('{"a": 1}')["a"] == 1)
    check("parser：markdown fence", extract_json('好的，计划如下：\n```json\n{"goal": "x", "steps": []}\n```\n以上')["goal"] == "x")
    check("parser：前后杂文", extract_json('说明文字 {"goal": "y"} 尾部废话')["goal"] == "y")
    check("parser：字符串内括号", extract_json('{"text": "包含 } 和 { 的内容", "n": 2}')["n"] == 2)
    check("parser：数组", len(extract_json_array('```json\n[1, 2, 3]\n```')) == 3)
    try:
        extract_json("完全没有 JSON")
        check("parser：垃圾输入抛错", False)
    except ValueError:
        check("parser：垃圾输入抛错", True)

    # ---------- 7. Planner 计划解析 ----------
    from agents.planner import Plan

    plan = Plan.from_dict(
        {
            "goal": "整理例会纪要并群发",
            "steps": [
                {"id": 1, "title": "录音转写", "agent": "MeetingAgent", "tool_hint": "transcribe", "expected": "文字稿"},
                {"title": "生成纪要", "agent": "DocAgent"},
            ],
        }
    )
    check("planner：计划解析", len(plan.steps) == 2 and plan.steps[0].agent == "MeetingAgent")
    check("planner：缺 id 时回填", plan.steps[1].id == 2)
    plan.steps[0].mark("done", "转写完成 1200 字")
    check("planner：to_prompt 含状态", "done" in plan.to_prompt() and "转写完成" in plan.to_prompt())
    check("planner：next_pending", plan.next_pending is plan.steps[1])

    # ---------- 8. 审计日志 ----------
    with tempfile.TemporaryDirectory() as td:
        from memory.structured_db import StructuredMemory

        mem = StructuredMemory(Path(td) / "m.sqlite3")
        mem.log_step("测试任务", "read_file", {"path": "x"}, True, 12)
        mem.log_step("测试任务", "read_file", {"path": "y"}, False, 5)
        mem.set_pref("常用称呼", "张工")
        stats = mem.tool_stats()
        check("memory：工具统计", stats[0]["calls"] == 2 and stats[0]["ok_calls"] == 1)
        check("memory：偏好读写", mem.get_pref("常用称呼") == "张工")
        check("memory：审计可查", len(mem.recent(task="测试任务")) == 2)
        mem.close()

    print(f"\n{'=' * 40}\n通过 {PASS} / 失败 {FAIL} / 共 {PASS + FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
