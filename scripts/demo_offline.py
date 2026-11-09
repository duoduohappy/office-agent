"""离线演示：不装 Ollama 也能跑完整业务闭环（面试演示用）。

用脚本化 LLM 模拟 Qwen2.5-7B 的各次响应，走通
Planner → MeetingAgent → DocAgent → Reflection → EmailAgent → Reflection → DesktopAgent，
并在 data/outbox/ 下产出真实的 Word / Excel / 通知草稿文件。

    .venv/Scripts/python.exe scripts/demo_offline.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.orchestrator import Orchestrator
from core.config import config
from core.tool_registry import ToolRegistry
from memory.structured_db import StructuredMemory
from tests.test_pipeline_offline import ScriptedLLM
from tools import audio_tools, doc_tools, file_tools


def main() -> int:
    config.ensure_dirs()
    registry = ToolRegistry().extend(file_tools.FILE_TOOLS).extend(doc_tools.DOC_TOOLS).extend(audio_tools.AUDIO_TOOLS)
    memory = StructuredMemory(config.memory_db)
    for name, email, dept in [
        ("张伟", "zhangwei@example.gov.cn", "综合办"),
        ("李娜", "lina@example.gov.cn", "综合办"),
        ("王强", "wangqiang@example.gov.cn", "技术科"),
    ]:
        memory.add_contact(name, email, dept)

    llm = ScriptedLLM()
    orchestrator = Orchestrator(llm, registry, memory)
    result = orchestrator.run_meeting_pipeline(
        "data/inbox/sample_meeting.txt",
        title="2026年第11次部门例会纪要",
        meeting_date="2026年11月6日",
        location="三楼会议室",
        host="张伟",
        attendees="张伟、李娜、王强、赵敏、陈建国",
    )

    print("=" * 66)
    print("端侧信创办公智能体 · 离线业务闭环演示")
    print("=" * 66)
    print(result.summary())
    print("-" * 66)
    if result.plan:
        print(f"\nPlanner 计划（{len(result.plan.steps)} 步）：")
        for s in result.plan.steps:
            print(f"  [{s.status:8s}] {s.agent:14s} {s.title}")
    if result.meeting:
        print(f"\n会议摘要：{result.meeting.summary[:160]}...")
        print("待办事项：")
        for t in result.meeting.todos:
            print(f"  - {t['who']}：{t['what']}（{t['when']}）")
    for r in result.reports:
        print(f"\n{r.text()}")
    if result.outputs:
        print("\n产出文件：")
        for p in result.outputs:
            size = Path(p).stat().st_size
            print(f"  {p}  ({size} bytes)")
    print(f"\n模型调用序列：{' → '.join(llm.call_log)}")
    print(f"闭环状态：{'成功' if result.ok else '存在未解决问题'}")
    print("=" * 66)
    memory.close()
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
