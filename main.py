"""入口：CLI 交互（方案 main.py）。

用法：
  python main.py                      # 交互式 REPL
  python main.py "总结 data/inbox 下的文件"   # 单任务
  python main.py --check              # 环境自检（Ollama/模型/目录）
"""

from __future__ import annotations

import argparse
import sys

from agents.orchestrator import Orchestrator
from core.config import config
from core.harness import AgentLoop, RunResult
from core.llm_client import OllamaClient
from core.prompt_builder import build_system_prompt
from core.tool_registry import ToolRegistry
from memory.structured_db import StructuredMemory
from tools import audio_tools, doc_tools, file_tools


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.extend(file_tools.FILE_TOOLS)
    registry.extend(doc_tools.DOC_TOOLS)
    registry.extend(audio_tools.AUDIO_TOOLS)
    return registry


def build_agent(model: str | None = None, agent_name: str = "GeneralAgent") -> AgentLoop:
    cfg = config
    if model:
        cfg_model = model
    else:
        cfg_model = cfg.model
    llm = OllamaClient(model=cfg_model, host=cfg.ollama_host, temperature=cfg.temperature, num_ctx=cfg.num_ctx, timeout=cfg.request_timeout)
    registry = build_registry()
    memory = StructuredMemory(cfg.memory_db)
    system_prompt = build_system_prompt(agent_name, registry)

    def on_step(record) -> None:
        mark = "OK " if record.ok else "ERR"
        print(f"  [{mark}] {record.tool_name}({record.arguments}) {record.elapsed_ms}ms")
        memory.log_step(
            task=current_task[0], tool_name=record.tool_name, arguments=record.arguments,
            ok=record.ok, elapsed_ms=record.elapsed_ms, agent=agent_name,
            detail=record.error or record.content[:500],
        )

    current_task = [""]

    class Loop(AgentLoop):
        def run(self, task, tool_schemas=None, history=None):
            current_task[0] = task[:200]
            return super().run(task, tool_schemas, history)

    return Loop(llm, registry, max_steps=cfg.max_steps, system_prompt=system_prompt, on_step=on_step), memory


def _user_env(name: str) -> str:
    """读用户级环境变量。

    set_disk_env.ps1 写入的是注册表 HKCU\\Environment，当前已打开的终端
    不一定能立刻看到；自检时把注册表值也读出来，避免误报"未配置"。
    """
    import os

    current = os.getenv(name, "")
    if current:
        return current
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                return str(winreg.QueryValueEx(key, name)[0])
        except OSError:
            return ""
    return ""


def check_env() -> int:
    """环境自检：Ollama 服务、模型、目录、磁盘缓存重定向。"""
    ok = True
    client = OllamaClient(model=config.model, host=config.ollama_host)
    if client.is_available():
        print(f"[OK] Ollama 服务：{config.ollama_host}")
        models = client.list_models()
        if models:
            print(f"[OK] 已装模型：{', '.join(models)}")
            if not client.has_model():
                print(f"[!!] 目标模型 {config.model} 未拉取（ollama pull {config.model}）")
                ok = False
        else:
            print("[!!] Ollama 在线但没有任何模型，先 ollama pull")
            ok = False
    else:
        print(f"[!!] Ollama 未启动/未安装（{config.ollama_host}）。安装后运行：ollama serve")
        ok = False

    config.ensure_dirs()
    print(f"[OK] 数据目录：{config.data_dir}")
    import os
    ollama_models = _user_env("OLLAMA_MODELS")
    if ollama_models:
        source = "当前会话" if os.getenv("OLLAMA_MODELS") else "用户级配置，新终端生效"
        print(f"[OK] 模型权重已重定向到：{ollama_models}（{source}）")
    else:
        print("[!!] OLLAMA_MODELS 未设置，模型会下载到 C 盘！先运行 scripts/set_disk_env.ps1")
        ok = False
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="端侧信创办公智能体")
    parser.add_argument("task", nargs="?", help="要执行的任务；不填则进入交互模式")
    parser.add_argument("--model", help="覆盖默认模型名")
    parser.add_argument("--check", action="store_true", help="环境自检")
    parser.add_argument("--pipeline", metavar="TRANSCRIPT", help="跑完整会议闭环（传转写稿路径）")
    parser.add_argument("--title", default="部门例会纪要", help="会议纪要标题")
    parser.add_argument("--attendees", default="", help="参会人员，逗号分隔")
    parser.add_argument("--group", default="全体成员", help="通知收件人组 / 部门名")
    parser.add_argument("--out-dir", default="data/outbox", help="产出目录（默认 data/outbox）")
    args = parser.parse_args()

    if args.check:
        return check_env()

    def seed_contacts(mem: StructuredMemory) -> None:
        """演示用本地通讯录；真实环境从企业通讯录 API 导入。"""
        if mem.all_contacts():
            return
        for name, email, dept in [
            ("张伟", "zhangwei@example.com", "综合办"),
            ("李娜", "lina@example.com", "综合办"),
            ("王强", "wangqiang@example.com", "技术科"),
        ]:
            mem.add_contact(name, email, dept)

    # --pipeline：跑方案 2.2 的完整会议闭环
    if args.pipeline:
        config.ensure_dirs()
        registry = build_registry()
        memory = StructuredMemory(config.memory_db)
        seed_contacts(memory)
        llm = OllamaClient(
            model=args.model or config.model, host=config.ollama_host,
            temperature=config.temperature, num_ctx=config.num_ctx, timeout=config.request_timeout,
        )
        orch = Orchestrator(llm, registry, memory)
        result = orch.run_meeting_pipeline(
            args.pipeline, title=args.title, attendees=args.attendees,
            recipients_group=args.group, out_dir=args.out_dir,
        )
        print(f"\n{'=' * 60}\n{result.summary()}\n{'=' * 60}")
        print(f"[闭环状态] {'成功' if result.ok else '存在未解决问题'}")
        memory.close()
        return 0 if result.ok else 1

    agent, memory = build_agent(args.model)
    config.ensure_dirs()
    seed_contacts(memory)

    def show(result: RunResult) -> None:
        print(f"\n{'=' * 60}\n{result.answer}\n{'=' * 60}")
        print(f"[运行统计] {result.summary()}")

    if args.task:
        show(agent.run(args.task))
    else:
        print(f"端侧办公智能体（模型 {args.model or config.model}），输入任务，exit 退出")
        while True:
            try:
                task = input("\n任务> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not task or task.lower() in {"exit", "quit"}:
                break
            show(agent.run(task))
    memory.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
