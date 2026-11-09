"""MCP (Model Context Protocol) 服务端 —— JSON-RPC 2.0。

设计取舍（如实说明）：
- 传输层实现 **stdio（换行分隔 JSON）** 与 **简化 HTTP POST** 两种；
  HTTP 模式是标准 JSON-RPC over HTTP，**不是**完整的 MCP Streamable HTTP
  （未实现 SSE 事件流 / session 协商），客户端按普通 JSON-RPC 调用即可。
- 工具不重复实现：直接把项目现有 ToolRegistry 的 ToolSpec 映射成 MCP
  tools/list 的 inputSchema，tools/call 走同一个 dispatch，保证 MCP 调用
  与 CLI / 评测入口行为一致。
- 安全边界：dangerous=True 的工具（写文件、生成通知等）会列出但默认拒绝执行，
  需显式 --allow-dangerous；不在协议层偷偷放开副作用。
- resources 暴露 data/inbox 下可解析文档，URI 为 file:///<项目内相对路径>，
  供客户端核验 RAG 引文原文（引用溯源链路的一环）。

用法：
  python -m protocols.mcp_server                      # stdio（供 MCP 客户端拉起）
  python -m protocols.mcp_server --self-test          # 进程内自测，不需要外部客户端
  python -m protocols.mcp_server --http 127.0.0.1:8765
  python -m protocols.mcp_server --allow-dangerous    # 放开有副作用工具
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote

from core.config import config
from core.tool_registry import ToolRegistry

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "office-agent"
SERVER_VERSION = "0.4.0"

# JSON-RPC 2.0 标准错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

_MIME = {
    ".txt": "text/plain", ".md": "text/markdown", ".csv": "text/csv",
    ".json": "application/json", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".pdf": "application/pdf",
}


def default_registry() -> ToolRegistry:
    """与 main.build_registry 同构；单独定义避免 protocols -> main 的循环导入。"""
    from tools import audio_tools, doc_tools, file_tools, knowledge_tools

    registry = ToolRegistry()
    registry.extend(file_tools.FILE_TOOLS)
    registry.extend(doc_tools.DOC_TOOLS)
    registry.extend(audio_tools.AUDIO_TOOLS)
    registry.extend(knowledge_tools.KNOWLEDGE_TOOLS)
    try:  # 向量检索工具是可选能力，未装 chromadb 时也能起 MCP
        from tools import rag_tools

        registry.extend(rag_tools.RAG_TOOLS)
    except ImportError:  # pragma: no cover - 取决于可选依赖
        pass
    return registry


def _response(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


class MCPServer:
    """无状态 JSON-RPC 处理器；stdio / HTTP / 进程内自测共用同一套逻辑。"""

    def __init__(
        self,
        registry: ToolRegistry | None = None,
        *,
        allow_dangerous: bool = False,
        inbox_dir: Path | None = None,
        auditor: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.registry = registry or default_registry()
        self.allow_dangerous = allow_dangerous
        self.inbox_dir = Path(inbox_dir or config.inbox_dir)
        self.auditor = auditor
        self.initialized = False

    # ------------------------------------------------------------------
    # 协议入口
    # ------------------------------------------------------------------
    def handle_message(self, message: Any) -> dict[str, Any] | None:
        """处理一条 JSON-RPC 消息；通知（无 id）返回 None。"""
        if not isinstance(message, dict):
            return _error(None, INVALID_REQUEST, "请求必须是 JSON 对象")
        if message.get("jsonrpc") != "2.0":
            return _error(message.get("id"), INVALID_REQUEST, "仅支持 jsonrpc=2.0")
        method = message.get("method")
        if not isinstance(method, str):
            return _error(message.get("id"), INVALID_REQUEST, "method 必须是字符串")
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _error(message.get("id"), INVALID_PARAMS, "params 必须是对象")
        req_id = message.get("id")
        is_notification = "id" not in message
        try:
            result = self.dispatch(method, params, is_notification=is_notification)
        except MCPMethodNotFound as exc:
            return None if is_notification else _error(req_id, METHOD_NOT_FOUND, str(exc))
        except (MCPInvalidParams, ValueError, TypeError) as exc:
            return None if is_notification else _error(req_id, INVALID_PARAMS, str(exc))
        except Exception as exc:  # noqa: BLE001 - 协议层不让单个工具异常打断会话
            logger.exception("MCP dispatch failed: %s", method)
            return None if is_notification else _error(req_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
        return None if is_notification else _response(req_id, result)

    def dispatch(self, method: str, params: dict[str, Any], *, is_notification: bool = False) -> Any:
        self._audit("request", {"method": method, "notification": is_notification})
        if method == "initialize":
            self.initialized = True
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                    "prompts": {"listChanged": False},
                    "logging": {},
                },
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "端侧离线办公智能体工具集。dangerous 工具默认被策略拒绝，"
                    "需服务端以 --allow-dangerous 启动；resources 可读取知识库原文用于引文核验。"
                ),
            }
        if method in {"notifications/initialized", "notifications/cancelled"}:
            return None
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": self.tools_list()}
        if method == "tools/call":
            return self.tools_call(params)
        if method == "resources/list":
            return {"resources": self.resources_list()}
        if method == "resources/read":
            return self.resources_read(params)
        if method == "resources/templates/list":
            return {"resourceTemplates": []}
        if method == "prompts/list":
            return {"prompts": []}
        if method == "logging/setLevel":
            return {}
        raise MCPMethodNotFound(f"未实现的方法：{method}")

    # ------------------------------------------------------------------
    # tools
    # ------------------------------------------------------------------
    def tools_list(self) -> list[dict[str, Any]]:
        out = []
        for name in self.registry.names():
            spec = self.registry.get(name)
            fn = spec.to_schema()["function"]
            schema = dict(fn.get("parameters") or {"type": "object", "properties": {}})
            schema.setdefault("type", "object")
            schema.setdefault("properties", {})
            out.append(
                {
                    "name": fn["name"],
                    "description": fn.get("description", ""),
                    "inputSchema": schema,
                    "annotations": {
                        "readOnlyHint": not spec.dangerous,
                        "destructiveHint": spec.dangerous,
                        "openWorldHint": False,
                    },
                }
            )
        return out

    def tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise MCPInvalidParams("tools/call 需要非空字符串 name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise MCPInvalidParams("arguments 必须是对象")
        spec = self.registry.get(name)
        if spec.dangerous and not self.allow_dangerous:
            self._audit("denied", {"tool": name})
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"[MCP_POLICY] 工具 {name} 有副作用（dangerous=True），当前服务端拒绝执行。"
                            "需以 --allow-dangerous 启动，或由调用方先取得人工确认。"
                        ),
                    }
                ],
                "isError": True,
            }
        result = self.registry.dispatch(name, arguments)
        self._audit("tool_call", {"tool": name, "ok": result.ok})
        payload: dict[str, Any] = {
            "content": [{"type": "text", "text": result.content or result.error or ""}],
            "isError": not result.ok,
        }
        if result.meta:
            payload["_meta"] = {"elapsed_ms": result.elapsed_ms, **result.meta}
        return payload

    # ------------------------------------------------------------------
    # resources（知识库原文，用于引文核验）
    # ------------------------------------------------------------------
    def resources_list(self) -> list[dict[str, Any]]:
        out = []
        for path in self._iter_documents():
            rel = self._relative(path)
            out.append(
                {
                    "uri": f"file:///{rel}",
                    "name": path.name,
                    "description": f"本地知识库文档 {rel}",
                    "mimeType": _MIME.get(path.suffix.lower(), "application/octet-stream"),
                }
            )
        return out

    def resources_read(self, params: dict[str, Any]) -> dict[str, Any]:
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri.startswith("file:///"):
            raise MCPInvalidParams("uri 必须是 file:///<项目内相对路径>")
        rel = unquote(uri[len("file:///") :])
        path = self._resolve_document(rel)
        if path is None:
            raise MCPInvalidParams(f"资源不存在或不在知识库目录内：{rel}")
        if path.suffix.lower() in {".txt", ".md", ".csv", ".json"}:
            text = path.read_text(encoding="utf-8", errors="replace")
        else:
            from tools.doc_tools import _extract_text

            text = _extract_text(path, path.suffix.lower())
        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": _MIME.get(path.suffix.lower(), "text/plain"),
                    "text": text,
                }
            ]
        }

    def _iter_documents(self):
        if not self.inbox_dir.is_dir():
            return []
        return sorted(
            p for p in self.inbox_dir.rglob("*")
            if p.is_file() and p.suffix.lower() in _MIME and not any(x.startswith(".") for x in p.parts)
        )

    def _relative(self, path: Path) -> str:
        try:
            return path.relative_to(config.project_root).as_posix()
        except ValueError:
            return path.as_posix()

    def _resolve_document(self, rel: str) -> Path | None:
        """防目录穿越：解析后必须仍落在 inbox_dir 内。"""
        candidate = (config.project_root / rel).resolve()
        root = self.inbox_dir.resolve()
        if not candidate.is_file() or root not in candidate.parents:
            return None
        return candidate

    def _audit(self, event: str, payload: dict[str, Any]) -> None:
        if self.auditor:
            self.auditor(event, payload)

    # ------------------------------------------------------------------
    # 传输
    # ------------------------------------------------------------------
    def serve_stdio(self, stdin=None, stdout=None) -> None:
        """stdio 帧 = 每行一个 JSON 对象；stdout 只写协议帧，日志一律走 stderr。"""
        inp = stdin or sys.stdin
        out = stdout or sys.stdout
        for line in inp:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                frame = _error(None, PARSE_ERROR, f"JSON 解析失败：{exc}")
            else:
                frame = self.handle_message(message)
            if frame is not None:
                out.write(json.dumps(frame, ensure_ascii=False) + "\n")
                out.flush()

    def http_handler(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args) -> None:  # 日志走 logging，不污染 stderr 协议帧
                logger.debug("mcp-http " + fmt, *args)

            def _send(self, status: int, payload: Any) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                if self.path.rstrip("/") in {"/health", ""}:
                    self._send(200, {"status": "ok", "server": SERVER_NAME, "protocolVersion": PROTOCOL_VERSION})
                else:
                    self._send(404, _error(None, METHOD_NOT_FOUND, "仅提供 POST /mcp 与 GET /health"))

            def do_POST(self) -> None:  # noqa: N802
                if self.path.rstrip("/") not in {"/mcp", ""}:
                    self._send(404, _error(None, METHOD_NOT_FOUND, "路径不存在"))
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    raw = self.rfile.read(length).decode("utf-8")
                    message = json.loads(raw)
                except (ValueError, UnicodeDecodeError) as exc:
                    self._send(400, _error(None, PARSE_ERROR, f"请求体解析失败：{exc}"))
                    return
                if isinstance(message, list):  # JSON-RPC batch
                    frames = [f for m in message if (f := server.handle_message(m)) is not None]
                    self._send(200, frames or {})
                else:
                    frame = server.handle_message(message)
                    self._send(200, frame if frame is not None else {})

        return Handler

    def serve_http(self, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
        httpd = ThreadingHTTPServer((host, port), self.http_handler())
        httpd.daemon_threads = True
        logger.info("MCP HTTP 监听 http://%s:%d/mcp", host, port)
        return httpd

    def self_test(self) -> dict[str, Any]:
        """进程内协议自测；不依赖外部客户端，结果用于离线断言。"""
        def call(method: str, params: dict[str, Any] | None = None, req_id: int = 1) -> dict[str, Any]:
            return self.handle_message({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}) or {}

        init = call("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}})
        tools = call("tools/list")
        names = [t["name"] for t in tools.get("result", {}).get("tools", [])]
        read = call("resources/list")
        results = {
            "initialize": init.get("result", {}).get("protocolVersion"),
            "tool_count": len(names),
            "tools": names,
            "resource_count": len(read.get("result", {}).get("resources", [])),
            "has_input_schema": all(
                t.get("inputSchema", {}).get("type") == "object" for t in tools.get("result", {}).get("tools", [])
            ),
            "ping": call("ping").get("result"),
            "unknown_method_error": call("nope").get("error", {}).get("code"),
        }
        if names:
            # 只读工具应能真实执行；dangerous 工具应被策略拒绝
            readonly = next(
                (n for n in names if not self.registry.get(n).dangerous), names[0]
            )
            probe = call("tools/call", {"name": readonly, "arguments": _probe_args(self.registry.get(readonly))})
            results["readonly_call"] = probe.get("result", {}).get("isError")
            danger = next((n for n in names if self.registry.get(n).dangerous), None)
            if danger:
                blocked = call("tools/call", {"name": danger, "arguments": {}})
                results["dangerous_blocked"] = (
                    blocked.get("result", {}).get("isError") is True and not self.allow_dangerous
                )
        return results


def _probe_args(spec) -> dict[str, Any]:
    """给只读工具造最小合法参数，取不到就返回空（失败也是可观察结果）。"""
    props = spec.parameters.get("properties", {})
    args: dict[str, Any] = {}
    for key, meta in props.items():
        typ = meta.get("type", "string")
        args[key] = 1 if typ == "integer" else "data/inbox/sample_meeting.txt" if "path" in key else "测试"
    required = spec.parameters.get("required", [])
    return {k: v for k, v in args.items() if k in required}


class MCPMethodNotFound(Exception):
    pass


class MCPInvalidParams(Exception):
    pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="office-agent MCP 服务端")
    parser.add_argument("--http", metavar="HOST:PORT", help="以简化 HTTP JSON-RPC 模式监听，如 127.0.0.1:8765")
    parser.add_argument("--self-test", action="store_true", help="进程内自测并打印 JSON")
    parser.add_argument("--allow-dangerous", action="store_true", help="允许调用有副作用的工具")
    parser.add_argument("--inbox", help="知识库目录，默认 data/inbox")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(levelname)s %(message)s")

    server = MCPServer(
        allow_dangerous=args.allow_dangerous,
        inbox_dir=Path(args.inbox) if args.inbox else None,
    )
    if args.self_test:
        print(json.dumps(server.self_test(), ensure_ascii=False, indent=2))
        return 0
    if args.http:
        host, _, port = args.http.rpartition(":")
        if not host:
            host, port = "127.0.0.1", "8765"
        httpd = server.serve_http(host, int(port))
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            httpd.server_close()
        return 0
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    except AttributeError:  # pragma: no cover - 非标准流
        pass
    server.serve_stdio()
    return 0


if __name__ == "__main__":
    sys.exit(main())
