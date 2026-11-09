"""A2A (Agent-to-Agent) 协议服务端与客户端。

实现范围（如实说明，避免把子集说成全量规范）：
- Agent Card：`GET /.well-known/agent.json`，按 A2A v0.3 字段组织。
- JSON-RPC 方法：`message/send`（v0.3）与 `tasks/send`（v0.2 别名）、
  `tasks/get`、`tasks/cancel`；Task 状态机 submitted → working →
  completed / failed / canceled。
- 消息 parts 输出用 v0.3 的 `kind`，解析同时兼容 v0.2 的 `type`。
- **未实现**：SSE 流式订阅 `message/stream` / `tasks/sendSubscribe`、
  push notification、鉴权与签名校验。Card 里 capabilities 如实置 false，
  调用这些方法返回 -32601，不伪造成功。
- Task 存储为进程内内存表（线程安全），**不持久化**；需要跨进程留存时
  由调用方把 Task 落库（edge 层的同步队列就是这么做的）。

用法：
  python -m protocols.a2a --self-test
  python -m protocols.a2a --http 127.0.0.1:8770
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

A2A_PROTOCOL_VERSION = "0.3.0"
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

TASK_STATES = {
    "submitted", "working", "input-required", "completed",
    "canceled", "failed", "rejected", "auth-required", "unknown",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def text_part(text: str) -> dict[str, str]:
    return {"kind": "text", "text": text}


def _part_text(part: Any) -> str:
    if not isinstance(part, dict):
        return ""
    return str(part.get("text") or part.get("data") or "")


def _message_text(message: Any) -> str:
    if isinstance(message, str):
        return message
    if not isinstance(message, dict):
        return ""
    parts = message.get("parts") or []
    return "\n".join(filter(None, (_part_text(p) for p in parts)))


class A2AError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


@dataclass
class SkillResult:
    """Skill 执行结果：主文本 + 可选结构化附件 + 溯源元数据。"""

    text: str
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    state: str = "completed"


@dataclass
class Skill:
    id: str
    name: str
    description: str
    tags: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    handler: Callable[[str, dict[str, Any]], SkillResult] | None = None

    def to_card(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "tags": self.tags,
            "examples": self.examples,
            "inputModes": ["text/plain"],
            "outputModes": ["text/plain", "application/json"],
        }


class TaskStore:
    """线程安全的进程内 Task 表。"""

    def __init__(self) -> None:
        self._tasks: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def create(self, task_id: str, message: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            task = {
                "kind": "task",
                "id": task_id,
                "contextId": _uid("ctx"),
                "status": {"state": "submitted", "timestamp": _now()},
                "messages": [message],
                "artifacts": [],
                "history": [],
                "metadata": metadata,
                "createdAt": _now(),
            }
            self._tasks[task_id] = task
            return self._copy(task)

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return self._copy(task) if task else None

    def update(self, task_id: str, mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any] | None:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None
            mutator(task)
            return self._copy(task)

    def ids(self) -> list[str]:
        with self._lock:
            return list(self._tasks)

    @staticmethod
    def _copy(task: dict[str, Any]) -> dict[str, Any]:
        return json.loads(json.dumps(task, ensure_ascii=False))


class TaskExecutor:
    """把 A2A Task 分派给已注册 Skill；没有 Ollama 也能真实跑通协议。"""

    def __init__(self, skills: list[Skill] | None = None) -> None:
        self.skills: dict[str, Skill] = {s.id: s for s in (skills or [])}

    def register(self, skill: Skill) -> Skill:
        self.skills[skill.id] = skill
        return skill

    def execute(self, text: str, skill_id: str | None, metadata: dict[str, Any]) -> SkillResult:
        skill = self.skills.get(skill_id) if skill_id else None
        if skill_id and not skill:
            raise A2AError(INVALID_PARAMS, f"未知 skill：{skill_id}", {"available": list(self.skills)})
        if not skill:
            skill = next(iter(self.skills.values()), None)
        if not skill or skill.handler is None:
            return SkillResult(text=f"已收到任务，但没有可执行的 skill：{text}", metadata={"noop": True})
        return skill.handler(text, metadata)


def default_executor() -> TaskExecutor:
    """默认能力：本地知识检索（可溯源）+ 回声，均不依赖模型。"""
    from core.tool_registry import ToolRegistry
    from tools import knowledge_tools

    registry = ToolRegistry().extend(knowledge_tools.KNOWLEDGE_TOOLS)

    def retrieve(text: str, metadata: dict[str, Any]) -> SkillResult:
        result = registry.dispatch("search_knowledge_base", {"query": text, "top_k": 5})
        if not result.ok:
            return SkillResult(text=f"检索失败：{result.error}", state="failed", metadata={"error": result.error})
        return SkillResult(
            text=result.content,
            artifacts=[
                {
                    "artifactId": _uid("art"),
                    "name": "knowledge_evidence",
                    "parts": [text_part(result.content)],
                    "metadata": {"tool": "search_knowledge_base", **result.meta},
                }
            ],
            metadata={"tool": "search_knowledge_base", "hits": result.meta.get("hits", 0)},
        )

    return TaskExecutor(
        [
            Skill(
                id="knowledge_search",
                name="本地知识检索",
                description="在端侧知识库中检索证据片段，返回带来源文件路径的材料",
                tags=["rag", "citation", "offline"],
                examples=["检索与国产适配相关的段落并标注来源"],
                handler=retrieve,
            ),
            Skill(
                id="echo",
                name="连通性回声",
                description="原样回显任务文本，用于协议连通性与链路验证",
                tags=["diagnostic"],
                examples=["ping"],
                handler=lambda text, meta: SkillResult(text=f"ECHO: {text}", metadata={"diagnostic": True}),
            ),
        ]
    )


class A2AServer:
    def __init__(
        self,
        *,
        name: str = "office-agent-edge",
        description: str = "端侧离线办公智能体（A2A 协议子集）",
        url: str = "http://127.0.0.1:8770",
        version: str = "0.4.0",
        executor: TaskExecutor | None = None,
        on_task: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.url = url.rstrip("/")
        self.version = version
        self.executor = executor or default_executor()
        self.store = TaskStore()
        self.on_task = on_task

    # ------------------------------------------------------------------
    def agent_card(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "url": self.url,
            "provider": {"organization": "office-agent", "url": self.url},
            "version": self.version,
            "protocolVersion": A2A_PROTOCOL_VERSION,
            "documentationUrl": "https://github.com/duoduohappy/office-agent",
            "capabilities": {
                "streaming": False,
                "pushNotifications": False,
                "stateTransitionHistory": True,
            },
            "securitySchemes": {},
            "security": [],
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain", "application/json"],
            "skills": [s.to_card() for s in self.executor.skills.values()],
            "supportsAuthenticatedExtendedCard": False,
            "_implementationNote": (
                "实现 message/send、tasks/send、tasks/get、tasks/cancel；"
                "未实现 SSE 流式订阅、push notification、鉴权；Task 仅内存存储不持久化。"
            ),
        }

    # ------------------------------------------------------------------
    def handle_message(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            req_id = message.get("id") if isinstance(message, dict) else None
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": INVALID_PARAMS, "message": "仅支持 JSON-RPC 2.0"}}
        req_id = message.get("id")
        try:
            result = self.dispatch(message.get("method", ""), message.get("params") or {})
        except A2AError as exc:
            err: dict[str, Any] = {"code": exc.code, "message": str(exc)}
            if exc.data is not None:
                err["data"] = exc.data
            return {"jsonrpc": "2.0", "id": req_id, "error": err}
        except Exception as exc:  # noqa: BLE001
            logger.exception("A2A dispatch failed")
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": INTERNAL_ERROR, "message": f"{type(exc).__name__}: {exc}"}}
        if "id" not in message:
            return None
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method in {"message/send", "tasks/send"}:
            return self._send(params)
        if method == "tasks/get":
            return self._get(params)
        if method == "tasks/cancel":
            return self._cancel(params)
        if method in {"message/stream", "tasks/sendSubscribe", "tasks/pushNotificationConfig/set"}:
            raise A2AError(METHOD_NOT_FOUND, f"{method} 未实现：本服务端未开启 streaming / push notification")
        if method == "agent/getAuthenticatedExtendedCard":
            raise A2AError(METHOD_NOT_FOUND, "未实现鉴权扩展 Agent Card")
        raise A2AError(METHOD_NOT_FOUND, f"未实现的方法：{method}")

    def _send(self, params: dict[str, Any]) -> dict[str, Any]:
        message = params.get("message")
        text = _message_text(message)
        if not text.strip():
            raise A2AError(INVALID_PARAMS, "message.parts 中缺少非空文本")
        if not isinstance(message, dict):
            message = {"kind": "message", "role": "user", "messageId": _uid("msg"), "parts": [text_part(text)]}
        message.setdefault("kind", "message")
        message.setdefault("role", "user")
        message.setdefault("messageId", _uid("msg"))
        metadata = dict(params.get("metadata") or {})
        skill_id = metadata.get("skillId") or params.get("skillId")
        task_id = params.get("id") or message.get("taskId") or _uid("task")
        configuration = params.get("configuration") or {}
        if configuration.get("blocking") is False:
            raise A2AError(INVALID_PARAMS, "本服务端只支持同步返回（blocking=true），不提供异步/流式执行")

        task = self.store.create(task_id, message, metadata)
        self._transition(task_id, "working")
        try:
            result = self.executor.execute(text, skill_id, metadata)
            state = result.state if result.state in TASK_STATES else "completed"
        except A2AError:
            self._transition(task_id, "failed")
            raise
        except Exception as exc:  # noqa: BLE001
            self._transition(task_id, "failed")
            raise A2AError(INTERNAL_ERROR, f"skill 执行异常：{type(exc).__name__}: {exc}") from exc

        def mutate(t: dict[str, Any]) -> None:
            t["status"] = {"state": state, "timestamp": _now()}
            answer = {
                "kind": "message", "role": "agent", "messageId": _uid("msg"),
                "parts": [text_part(result.text)], "taskId": task_id,
            }
            t["messages"].append(answer)
            t["artifacts"] = list(result.artifacts)
            t["metadata"].update(result.metadata)
            t["history"].append({"state": "submitted", "timestamp": task["createdAt"]})
            t["history"].append({"state": "working", "timestamp": _now()})

        final = self.store.update(task_id, mutate) or task
        if self.on_task:
            self.on_task(final)
        return final

    def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        task_id = params.get("id")
        if not task_id:
            raise A2AError(INVALID_PARAMS, "tasks/get 需要 id")
        task = self.store.get(str(task_id))
        if not task:
            raise A2AError(INVALID_PARAMS, f"Task 不存在：{task_id}")
        length = params.get("historyLength")
        if isinstance(length, int) and length >= 0:
            task["history"] = task["history"][:length]
        return task

    def _cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        task_id = str(params.get("id") or "")
        if not self.store.get(task_id):
            raise A2AError(INVALID_PARAMS, f"Task 不存在：{task_id}")
        return self.store.update(
            task_id,
            lambda t: t.update({"status": {"state": "canceled", "timestamp": _now()}}),
        )

    def _transition(self, task_id: str, state: str) -> None:
        self.store.update(task_id, lambda t: t.update({"status": {"state": state, "timestamp": _now()}}))

    # ------------------------------------------------------------------
    def http_handler(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args) -> None:
                logger.debug("a2a-http " + fmt, *args)

            def _send(self, status: int, payload: Any) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                path = self.path.split("?")[0].rstrip("/") or "/"
                if path == "/.well-known/agent.json":
                    self._send(200, server.agent_card())
                elif path in {"/health", "/"}:
                    self._send(200, {"status": "ok", "name": server.name, "protocolVersion": A2A_PROTOCOL_VERSION})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                path = self.path.split("?")[0].rstrip("/") or "/"
                if path not in {"/a2a", "/"}:
                    self._send(404, {"error": "not found"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    raw = self.rfile.read(length).decode("utf-8")
                    message = json.loads(raw)
                except (ValueError, UnicodeDecodeError) as exc:
                    self._send(400, {"jsonrpc": "2.0", "id": None, "error": {"code": INVALID_PARAMS, "message": str(exc)}})
                    return
                frame = server.handle_message(message)
                self._send(200, frame if frame is not None else {})

        return Handler

    def serve_http(self, host: str = "127.0.0.1", port: int = 8770) -> ThreadingHTTPServer:
        self.url = f"http://{host}:{port}"
        httpd = ThreadingHTTPServer((host, port), self.http_handler())
        httpd.daemon_threads = True
        logger.info("A2A 监听 %s（card: /.well-known/agent.json）", self.url)
        return httpd


class A2AClient:
    """纯标准库 A2A 客户端；给 edge 层做端↔边同步用。"""

    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get(self, path: str) -> dict[str, Any]:
        with urlopen(f"{self.base_url}{path}", timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _rpc(self, method: str, params: dict[str, Any]) -> Any:
        payload = {"jsonrpc": "2.0", "id": _uid("req"), "method": method, "params": params}
        req = Request(
            f"{self.base_url}/a2a",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urlopen(req, timeout=self.timeout) as resp:
            frame = json.loads(resp.read().decode("utf-8"))
        if "error" in frame:
            err = frame["error"]
            raise A2AError(err.get("code", INTERNAL_ERROR), err.get("message", "unknown"), err.get("data"))
        return frame.get("result")

    def agent_card(self) -> dict[str, Any]:
        return self._get("/.well-known/agent.json")

    def send(
        self,
        text: str,
        *,
        skill_id: str | None = None,
        task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        meta = dict(metadata or {})
        if skill_id:
            meta["skillId"] = skill_id
        params: dict[str, Any] = {
            "message": {"kind": "message", "role": "user", "messageId": _uid("msg"), "parts": [text_part(text)]},
            "metadata": meta,
        }
        if task_id:
            params["id"] = task_id
        return self._rpc("message/send", params)

    def get(self, task_id: str, history_length: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"id": task_id}
        if history_length is not None:
            params["historyLength"] = history_length
        return self._rpc("tasks/get", params)

    def cancel(self, task_id: str) -> dict[str, Any]:
        return self._rpc("tasks/cancel", {"id": task_id})

    def is_available(self) -> bool:
        try:
            return self._get("/health").get("status") == "ok"
        except (URLError, OSError, ValueError, json.JSONDecodeError):
            return False


def start_background(host: str = "127.0.0.1", port: int = 0, **kwargs: Any) -> tuple[A2AServer, ThreadingHTTPServer, threading.Thread]:
    """给测试/演示用：后台起一个真实 HTTP 服务，返回 (server, httpd, thread)。"""
    server = A2AServer(**kwargs)
    httpd = server.serve_http(host, port)
    actual_port = httpd.server_address[1]
    server.url = f"http://{host}:{actual_port}"
    thread = threading.Thread(target=httpd.serve_forever, name="a2a-server", daemon=True)
    thread.start()
    return server, httpd, thread


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="office-agent A2A 服务端")
    parser.add_argument("--http", metavar="HOST:PORT", help="监听地址，如 127.0.0.1:8770")
    parser.add_argument("--self-test", action="store_true", help="进程内自测")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(levelname)s %(message)s")

    if args.self_test:
        server = A2AServer()
        card = server.agent_card()
        send = server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "message/send",
             "params": {"message": {"parts": [text_part("国产适配")]}, "metadata": {"skillId": "knowledge_search"}}}
        )
        task_id = send["result"]["id"]
        got = server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tasks/get", "params": {"id": task_id}})
        stream = server.handle_message({"jsonrpc": "2.0", "id": 3, "method": "message/stream", "params": {}})
        print(json.dumps(
            {
                "skills": [s["id"] for s in card["skills"]],
                "streaming": card["capabilities"]["streaming"],
                "send_state": send["result"]["status"]["state"],
                "artifacts": len(send["result"]["artifacts"]),
                "get_state": got["result"]["status"]["state"],
                "stream_unsupported_code": stream["error"]["code"],
            },
            ensure_ascii=False, indent=2,
        ))
        return 0

    host, _, port = (args.http or "127.0.0.1:8770").rpartition(":")
    if not host:
        host, port = "127.0.0.1", "8770"
    httpd = A2AServer().serve_http(host, int(port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
