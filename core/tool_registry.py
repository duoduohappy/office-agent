from __future__ import annotations

import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable


@dataclass
class ToolResult:
    """工具执行的统一返回结构。"""

    ok: bool
    content: str
    tool_name: str = ""
    elapsed_ms: int = 0
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_message_content(self) -> str:
        """转成回灌给模型的一段纯文本。"""
        if self.ok:
            return self.content
        return f"[TOOL_ERROR] {self.tool_name} 执行失败：{self.error}"

    @classmethod
    def failure(cls, tool_name: str, error: str, elapsed_ms: int = 0) -> "ToolResult":
        return cls(ok=False, content="", tool_name=tool_name, error=error, elapsed_ms=elapsed_ms)


@dataclass
class ToolSpec:
    """一个可被模型调用的工具。"""

    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[..., Any]
    dangerous: bool = False  # True 表示有外部副作用，需要人工确认或白名单校验

    def to_schema(self) -> dict[str, Any]:
        """OpenAI / Ollama 兼容的 function calling schema。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def invoke(self, arguments: dict[str, Any]) -> ToolResult:
        """执行工具，把异常收敛成 ToolResult。"""
        start = time.perf_counter()
        try:
            self._validate_arguments(arguments)
            result = self.func(**arguments)
            elapsed = int((time.perf_counter() - start) * 1000)
            if isinstance(result, ToolResult):
                result.tool_name = result.tool_name or self.name
                result.elapsed_ms = elapsed
                return result
            return ToolResult(
                ok=True,
                content=str(result),
                tool_name=self.name,
                elapsed_ms=elapsed,
            )
        except Exception as exc:  # noqa: BLE001 - 故意兜住全部，交给模型自我修正
            elapsed = int((time.perf_counter() - start) * 1000)
            return ToolResult.failure(self.name, f"{type(exc).__name__}: {exc}", elapsed)

    def _validate_arguments(self, arguments: dict[str, Any]) -> None:
        """轻量参数校验：只检查必填项和未知参数，不做完整 JSON Schema 校验。"""
        if not isinstance(arguments, dict):
            raise TypeError(f"参数必须是 dict，收到 {type(arguments).__name__}")
        schema = self.parameters or {}
        required = schema.get("required", []) or []
        props = schema.get("properties", {}) or {}
        missing = [key for key in required if key not in arguments]
        if missing:
            raise ValueError(f"缺少必填参数 {missing}，该工具需要 {list(props)}")
        if props:
            unknown = [key for key in arguments if key not in props]
            if unknown:
                raise ValueError(f"存在未定义参数 {unknown}，允许的参数为 {list(props)}")


def tool(
    name: str | None = None,
    description: str = "",
    parameters: dict[str, Any] | None = None,
    dangerous: bool = False,
) -> Callable[[Callable[..., Any]], ToolSpec]:
    """把一个函数声明为工具。"""

    def decorator(func: Callable[..., Any]) -> ToolSpec:
        spec = ToolSpec(
            name=name or func.__name__,
            description=description or inspect.getdoc(func) or "",
            parameters=parameters or {"type": "object", "properties": {}},
            func=func,
            dangerous=dangerous,
        )
        return spec

    return decorator

logger = logging.getLogger(__name__)


class ToolNotFound(KeyError):
    pass


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    # ---------------- 注册 ----------------
    def add(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._tools:
            raise ValueError(f"工具名重复注册：{spec.name}")
        self._tools[spec.name] = spec
        return spec

    def register(self, spec: ToolSpec) -> ToolSpec:
        """兼容 add 的别名，便于链式写法。"""
        return self.add(spec)

    def define(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        dangerous: bool = False,
    ):
        """装饰器方式注册。"""

        def decorator(func):
            spec = tool_decorator(
                name=name, description=description, parameters=parameters, dangerous=dangerous
            )(func)
            self.add(spec)
            return func

        return decorator

    def extend(self, specs: Iterable[ToolSpec]) -> "ToolRegistry":
        for spec in specs:
            self.add(spec)
        return self

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    # ---------------- 查询 ----------------
    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotFound(
                f"工具 {name} 不存在，可用工具：{list(self._tools)}"
            ) from exc

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        """给模型看的工具清单。"""
        return [spec.to_schema() for spec in self._tools.values()]

    def describe(self) -> str:
        """给 System Prompt 用的人类可读工具清单（模型不认 schema 时的兜底）。"""
        lines = []
        for spec in self._tools.values():
            params = spec.parameters.get("properties", {})
            arg_text = ", ".join(params) if params else "无参数"
            lines.append(f"- {spec.name}({arg_text}): {spec.description}")
        return "\n".join(lines)

    # ---------------- 执行 ----------------
    def dispatch(self, name: str, arguments: dict[str, Any] | None = None) -> ToolResult:
        """执行一次工具调用。永不抛异常，失败信息会回灌给模型。"""
        try:
            spec = self.get(name)
        except ToolNotFound as exc:
            return ToolResult.failure(name, str(exc))
        result = spec.invoke(arguments or {})
        level = logging.INFO if result.ok else logging.WARNING
        logger.log(level, "tool=%s ok=%s %dms", name, result.ok, result.elapsed_ms)
        return result
