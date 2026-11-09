"""Ollama 客户端。

只用标准库 urllib，不引入 httpx/requests——信创内网离线部署时依赖越少越好，
而 Ollama 本身就是本地 HTTP 服务，标准库完全够用。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    id: str = ""

    def to_message(self) -> dict[str, Any]:
        """Ollama 的 assistant tool_calls 结构。"""
        return {
            "function": {
                "name": self.name,
                "arguments": self.arguments,
            }
        }


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class OllamaError(RuntimeError):
    pass


class OllamaClient:
    def __init__(
        self,
        model: str,
        host: str = "http://127.0.0.1:11434",
        temperature: float = 0.2,
        num_ctx: int = 8192,
        timeout: float = 180.0,
    ) -> None:
        self.model = model
        self.host = host.rstrip("/")
        self.temperature = temperature
        self.num_ctx = num_ctx
        self.timeout = timeout
        # qwen2.5 的 tools 模板偶发返回 content="" 且 tool_calls=null，
        # 但 done_reason=stop、eval_count>0 —— 模型确实生成了 token，
        # 却被 Ollama 的工具解析器整体丢弃。单独统计，评测报告如实披露。
        self.empty_response_retries = 0

    # ---------------- 底层请求 ----------------
    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.host}{path}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise OllamaError(f"HTTP {exc.code} from {url}: {body}") from exc
        except urllib.error.URLError as exc:
            raise OllamaError(
                f"无法连接 Ollama（{url}）：{exc.reason}。请确认 `ollama serve` 已启动。"
            ) from exc

    def _get(self, path: str) -> dict[str, Any]:
        url = f"{self.host}{path}"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            raise OllamaError(f"无法连接 Ollama（{url}）：{exc}") from exc

    # ---------------- 能力探测 ----------------
    def is_available(self) -> bool:
        try:
            self._get("/api/tags")
            return True
        except OllamaError:
            return False

    def list_models(self) -> list[str]:
        try:
            data = self._get("/api/tags")
        except OllamaError:
            return []
        return [m.get("name", "") for m in data.get("models", [])]

    def has_model(self) -> bool:
        return any(name == self.model or name.startswith(self.model + ":") for name in self.list_models())

    # ---------------- 对话 ----------------
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        format_json: bool = False,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": self.temperature if temperature is None else temperature,
                "num_ctx": self.num_ctx,
            },
        }
        if tools:
            payload["tools"] = tools
        if format_json:
            payload["format"] = "json"

        # 空响应重试：这是后端解析抖动，不改变 payload / 温度 / 工具清单，
        # 也不改任何评分口径；重试次数单独计数，避免把失败藏起来。
        for attempt in range(3):
            data = self._post("/api/chat", payload)
            message = data.get("message", {}) or {}
            content = message.get("content", "") or ""
            tool_calls = self._parse_tool_calls(message.get("tool_calls") or [])
            if content.strip() or tool_calls:
                return LLMResponse(
                    content=content,
                    tool_calls=tool_calls,
                    model=data.get("model", self.model),
                    raw=data,
                )
            if attempt < 2:
                self.empty_response_retries += 1
                logger.warning(
                    "Ollama 返回空响应（content 与 tool_calls 均为空），第 %d 次原样重试",
                    attempt + 1,
                )
        return LLMResponse(content="", model=data.get("model", self.model), raw=data)

    @staticmethod
    def _parse_tool_calls(raw_calls: list[dict[str, Any]]) -> list[ToolCall]:
        """Ollama 各版本的 arguments 可能是 dict 也可能是 JSON 字符串，都要能吃。"""
        calls: list[ToolCall] = []
        for item in raw_calls:
            fn = item.get("function", {}) or {}
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args.strip() else {}
                except json.JSONDecodeError:
                    logger.warning("工具参数不是合法 JSON，原样透传：%r", args)
                    args = {}
            if not isinstance(args, dict):
                args = {}
            calls.append(
                ToolCall(
                    name=fn.get("name", ""),
                    arguments=args,
                    id=item.get("id", ""),
                )
            )
        return calls
