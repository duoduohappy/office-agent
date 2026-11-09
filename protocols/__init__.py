"""协议层。

- `protocols.mcp_server`：MCP (Model Context Protocol) 服务端，把本地
  ToolRegistry 暴露为 MCP tools / resources，支持 stdio 与简化 HTTP JSON-RPC。
- `protocols.a2a`：A2A (Agent-to-Agent) Agent Card + Task 状态机 + 客户端。

按需导入子模块（例如 `from protocols import a2a`），避免 import protocols
就拉起工具注册表等重依赖。实现范围与未实现项见各模块 docstring 与 README。
"""

__all__ = ["mcp_server", "a2a"]
