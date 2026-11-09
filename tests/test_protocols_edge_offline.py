"""协议层 / 向量库 / 端边云协同离线自测。

全部断言都不依赖 Ollama；HTTP 部分是**真实本机 socket 往返**（不是 mock），
因此可以验证 stdio/HTTP 帧、A2A 状态机、端↔边同步确实跑通。

同时包含防自欺护栏：后端降级必须留痕、不得把 hashing embedding 说成语义模型、
新增工具不得悄悄污染 20 条评测集的 registry 与 requires。

运行：.venv/Scripts/python.exe tests/test_protocols_edge_offline.py
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import config
from core.tool_registry import ToolRegistry
from edge import coordination as edge
from memory import vector_store as vs
from protocols import a2a
from protocols import mcp_server as mcp
from tools import knowledge_tools, rag_tools

PASS = 0
FAIL = 0
TMP = config.workspace_dir / "_selftest" / "protocols"
TMP.mkdir(parents=True, exist_ok=True)


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL: {name} {detail}")


def rpc(server, method: str, params: dict | None = None, req_id: int = 1):
    return server.handle_message({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})


# ----------------------------------------------------------------------
def test_mcp() -> None:
    registry = mcp.default_registry()
    server = mcp.MCPServer(registry, allow_dangerous=False)

    init = rpc(server, "initialize", {"protocolVersion": mcp.PROTOCOL_VERSION})["result"]
    check("MCP initialize 返回协议版本", init["protocolVersion"] == mcp.PROTOCOL_VERSION)
    check("MCP capabilities 含 tools/resources", "tools" in init["capabilities"] and "resources" in init["capabilities"])

    tools = rpc(server, "tools/list")["result"]["tools"]
    check("MCP tools/list 数量与注册表一致", len(tools) == len(registry.names()), str(len(tools)))
    check("MCP 每个工具都是合法 inputSchema", all(t["inputSchema"]["type"] == "object" for t in tools))
    names = [t["name"] for t in tools]
    check("MCP 暴露向量检索工具", "vector_search" in names and "verify_quote" in names)
    check("MCP 工具标注 dangerous 注解", all("annotations" in t and "readOnlyHint" in t["annotations"] for t in tools))

    ok = rpc(server, "tools/call", {"name": "list_dir", "arguments": {"path": "data/inbox"}})["result"]
    check("MCP 只读工具真实执行", ok["isError"] is False and ok["content"], str(ok)[:120])

    danger = next(n for n in names if registry.get(n).dangerous)
    denied = rpc(server, "tools/call", {"name": danger, "arguments": {}})["result"]
    check("MCP dangerous 工具默认拒绝", denied["isError"] is True and "MCP_POLICY" in denied["content"][0]["text"])

    opened = mcp.MCPServer(registry, allow_dangerous=True)
    allowed = rpc(opened, "tools/call", {"name": "write_file", "arguments": {
        "path": "data/workspace/_selftest/protocols/mcp_write.txt", "content": "mcp-ok"}})["result"]
    check("MCP --allow-dangerous 后可写", allowed["isError"] is False, str(allowed)[:120])

    resources = rpc(server, "resources/list")["result"]["resources"]
    check("MCP resources 暴露知识库文档", len(resources) > 0)
    uri = resources[0]["uri"]
    read = rpc(server, "resources/read", {"uri": uri})["result"]
    check("MCP resources/read 返回原文", len(read["contents"][0]["text"]) > 0)
    blocked = rpc(server, "resources/read", {"uri": "file:///../../Windows/win.ini"})
    check("MCP resources 拒绝目录穿越", blocked["error"]["code"] == mcp.INVALID_PARAMS)

    check("MCP 未知方法", rpc(server, "nope")["error"]["code"] == mcp.METHOD_NOT_FOUND)
    bad = server.handle_message({"jsonrpc": "1.0", "id": 7, "method": "ping"})
    check("MCP 拒绝非 2.0 帧", bad["error"]["code"] == mcp.INVALID_REQUEST)
    check("MCP 通知不回帧", server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None)
    check("MCP ping", rpc(server, "ping")["result"] == {})

    # stdio 帧：stdout 只写协议帧
    inp = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n" + "bad-json\n")
    out = io.StringIO()
    server.serve_stdio(stdin=inp, stdout=out)
    frames = [json.loads(x) for x in out.getvalue().strip().splitlines()]
    check("MCP stdio 逐行帧", len(frames) == 2 and frames[0]["result"] == {}, str(frames)[:150])
    check("MCP stdio 解析错误码", frames[1]["error"]["code"] == mcp.PARSE_ERROR)

    # 真实 HTTP 往返
    httpd = server.serve_http("127.0.0.1", 0)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        import urllib.request

        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/mcp",
            data=json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/list"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read())
        check("MCP HTTP 真实往返", len(body["result"]["tools"]) == len(tools))
    finally:
        httpd.shutdown()
        httpd.server_close()


# ----------------------------------------------------------------------
def test_a2a() -> None:
    server = a2a.A2AServer()
    card = server.agent_card()
    check("A2A Agent Card 字段完整", all(k in card for k in ("name", "url", "version", "skills", "capabilities")))
    check("A2A 如实声明不支持 streaming", card["capabilities"]["streaming"] is False)
    check("A2A 如实声明不支持 push", card["capabilities"]["pushNotifications"] is False)
    check("A2A 注册 skills", {s["id"] for s in card["skills"]} == {"knowledge_search", "echo"})

    send = rpc(server, "message/send", {
        "message": {"parts": [a2a.text_part("国产适配")]}, "metadata": {"skillId": "echo"}})
    task = send["result"]
    check("A2A message/send 完成", task["status"]["state"] == "completed", str(task["status"]))
    check("A2A Task 有 agent 回复", len(task["messages"]) == 2 and "ECHO" in task["messages"][1]["parts"][0]["text"])
    check("A2A Task 有 contextId", "contextId" in task)

    got = rpc(server, "tasks/get", {"id": task["id"]})["result"]
    check("A2A tasks/get 返回同一 Task", got["id"] == task["id"])
    check("A2A 状态历史可追溯", len(got["history"]) == 2)

    alias = rpc(server, "tasks/send", {"message": {"type": "message", "parts": [{"type": "text", "text": "hi"}]},
                                      "metadata": {"skillId": "echo"}})
    check("A2A 兼容 tasks/send 与 v0.2 parts", alias["result"]["status"]["state"] == "completed")
    check("A2A 可取消 Task", rpc(server, "tasks/cancel", {"id": alias["result"]["id"]})["result"]["status"]["state"] == "canceled")

    check("A2A 流式方法明确拒绝", rpc(server, "message/stream")["error"]["code"] == a2a.METHOD_NOT_FOUND)
    check("A2A 未知 skill 拒绝", rpc(server, "message/send", {
        "message": {"parts": [a2a.text_part("x")]}, "metadata": {"skillId": "nope"}})["error"]["code"] == a2a.INVALID_PARAMS)
    check("A2A 空任务文本拒绝", rpc(server, "message/send", {"message": {"parts": []}})["error"]["code"] == a2a.INVALID_PARAMS)
    check("A2A 异步配置拒绝", rpc(server, "message/send", {
        "message": {"parts": [a2a.text_part("x")]}, "configuration": {"blocking": False}})["error"]["code"] == a2a.INVALID_PARAMS)
    check("A2A 未知 Task 拒绝", rpc(server, "tasks/get", {"id": "not-exist"})["error"]["code"] == a2a.INVALID_PARAMS)

    # 真实 HTTP 客户端 ↔ 服务端
    srv, httpd, _ = a2a.start_background(port=0)
    try:
        client = a2a.A2AClient(srv.url)
        check("A2A 客户端健康检查", client.is_available())
        remote_card = client.agent_card()
        check("A2A 客户端取到 Agent Card", remote_card["protocolVersion"] == a2a.A2A_PROTOCOL_VERSION)
        remote = client.send("国产适配", skill_id="knowledge_search")
        check("A2A HTTP 发送任务", remote["status"]["state"] == "completed")
        check("A2A 返回 artifacts", len(remote["artifacts"]) >= 1)
        check("A2A HTTP 查询任务", client.get(remote["id"])["id"] == remote["id"])
        check("A2A HTTP 取消任务", client.cancel(remote["id"])["status"]["state"] == "canceled")
        try:
            client.send("x", skill_id="missing")
            stream_ok = False
        except a2a.A2AError:
            stream_ok = True
        check("A2A 客户端能收到服务端错误", stream_ok)
    finally:
        httpd.shutdown()
        httpd.server_close()


# ----------------------------------------------------------------------
def test_vector_store() -> None:
    store = vs.VectorStore(TMP / "chroma")
    stats = store.stats()
    check("向量库后端是合法值", stats["backend"] in {"chroma", "fallback-hash-cosine"})
    check("向量库如实标注非语义 embedding", stats["embedding_is_semantic"] is False)
    check("向量库状态不含绝对路径", ":\\" not in stats["persist_dir"] and not stats["persist_dir"].startswith("/"),
          stats["persist_dir"])
    if vs.chromadb is not None:
        check("Chroma 已安装时必须使用真后端", store.is_real_chroma and stats["backend"] == "chroma")
    else:
        check("Chroma 缺失时降级必须留痕", not store.is_real_chroma and bool(stats["init_error"]), str(stats["init_error"]))

    indexed = store.index_directory()
    check("索引到知识库片段", indexed["indexed_chunks"] > 0, str(indexed))
    check("索引写入去重生效", indexed["deduped"] >= 0 and indexed["indexed_chunks"] <= indexed["scanned_chunks"])
    check("索引结果如实声明非语义", indexed["embedding_is_semantic"] is False)

    hits = store.search("国产适配", top_k=3)
    check("向量检索有命中", len(hits) > 0)
    check("Citation 带来源/哈希/相似度", all(c.source and len(c.sha256) == 64 and c.score >= 0 for c in hits))
    check("Citation 后端与 store 一致", all(c.backend == store.backend for c in hits))
    check("时间权重在 [0.8,1.0]", all(0.8 <= c.time_weight <= 1.0 for c in hits))
    check("引文全部可核验", all(store.verify(c)["valid"] for c in hits))

    block = store.citation_block(hits)
    check("引用块含 sha256 与编号", "sha256=" in block and "[1]" in block)
    check("引用块声明非语义模型", "非语义模型" in block)

    good = store.verify_answer("依据 [1] 的材料……", hits)
    bad = store.verify_answer("依据 [9] 的材料……", hits)
    none = store.verify_answer("没有任何引用。", hits)
    check("合法引用编号通过", good["ok"] is True)
    check("幻觉引用编号被拦截", bad["ok"] is False and bad["unknown_ids"] == [9])
    check("无引用不算溯源成功", none["ok"] is False)

    tampered = vs.Citation(**{**hits[0].to_dict(), "text": "这段原文根本不存在"})
    check("篡改引文核验失败", store.verify(tampered)["valid"] is False)
    broken = vs.Citation(**{**hits[0].to_dict(), "source": "../../Windows/win.ini"})
    check("越界来源核验失败", store.verify(broken)["valid"] is False)

    fallback = vs.VectorStore(TMP / "fallback", prefer_chroma=False)
    check("强制降级后端正确标记", fallback.backend == "fallback-hash-cosine" and not fallback.is_real_chroma)
    fallback.index_directory()
    check("降级后端也能检索", len(fallback.search("会议", 2)) > 0)


def test_rag_tools() -> None:
    registry = ToolRegistry().extend(rag_tools.RAG_TOOLS)
    r = registry.dispatch("vector_search", {"query": "国产适配", "top_k": 2})
    check("vector_search 工具执行成功", r.ok, str(r.error))
    check("vector_search 返回结构化 citations", len(r.meta.get("citations", [])) > 0)
    check("vector_search meta 如实标注后端", r.meta.get("backend") in {"chroma", "fallback-hash-cosine"})
    check("vector_search 输出声明非语义", "非语义模型" in r.content or "非语义" in r.content)

    status = registry.dispatch("vector_index_status", {})
    check("vector_index_status 不含绝对路径", ":\\" not in status.content and "TEMPAD" not in status.content)

    source = "data/inbox/eval_fixtures/notice_source.txt"
    quote = Path(ROOT / source).read_text(encoding="utf-8").splitlines()[0]
    ok = registry.dispatch("verify_quote", {"source": source, "quote": quote})
    check("verify_quote 真引文通过", ok.ok and ok.meta["found"] is True)
    bad = registry.dispatch("verify_quote", {"source": source, "quote": "这段是编造的内容XYZ123"})
    check("verify_quote 假引文失败", bad.ok and bad.meta["found"] is False)
    escape = registry.dispatch("verify_quote", {"source": "../../Windows/win.ini", "quote": "x"})
    check("verify_quote 拒绝路径穿越", not escape.ok)


# ----------------------------------------------------------------------
def test_edge() -> None:
    device, edge_node, cloud = edge.build_default_nodes()
    check("云侧默认拒绝业务数据", cloud.accepts_business_data is False)
    try:
        edge.NodeDescriptor("bad", edge.ROLE_CLOUD, accepts_business_data=True)
        raised = False
    except edge.PolicyViolation:
        raised = True
    check("云侧节点构造强制安全策略", raised)

    router = edge.TaskRouter(edge.build_default_nodes(edge_url="http://127.0.0.1:9"))
    local = router.route(edge.TaskProfile("整理纪要", privacy="local_only", complexity=0.99))
    check("local_only 再复杂也不出机", local.role == edge.ROLE_DEVICE and not local.mask_required)
    escalated = router.route(edge.TaskProfile("长文档分析", privacy="intranet_masked", complexity=0.9))
    check("复杂任务上送边侧并要求脱敏", escalated.role == edge.ROLE_EDGE and escalated.mask_required)
    unmet = router.route(edge.TaskProfile("训练", privacy="intranet_masked", complexity=0.9, requires=("gpu_training",)))
    check("边侧缺能力时留端侧", unmet.role == edge.ROLE_DEVICE)
    simple = router.route(edge.TaskProfile("问答", privacy="intranet_masked", complexity=0.2))
    check("日常任务端侧闭环", simple.role == edge.ROLE_DEVICE)

    try:
        router.route(edge.TaskProfile("客户名单", payload_kind="business_data", metadata={"target_node": "cloud-01"}))
        blocked = False
    except edge.PolicyViolation:
        blocked = True
    check("路由层拒绝业务数据上云", blocked)
    weight = router.route(edge.TaskProfile("", payload_kind="model_weights"))
    check("权重任务走云侧", weight.role == edge.ROLE_CLOUD)
    try:
        router.route(edge.TaskProfile("夹带业务文本", payload_kind="model_weights"))
        no_smuggle = False
    except edge.PolicyViolation:
        no_smuggle = True
    check("权重任务禁止夹带业务文本", no_smuggle)

    masked = edge.desanitize("联系 zhangwei@example.com 或 13800138000，路径 C:\\secret\\a.txt")
    check("脱敏覆盖邮箱/手机/路径", masked.replacements.get("email") == 1
          and masked.replacements.get("phone") == 1 and masked.replacements.get("abs_path") == 1,
          str(masked.replacements))
    check("脱敏后不含原始 PII", "zhangwei@example.com" not in masked.text and "13800138000" not in masked.text)

    cloud_ch = edge.CloudWeightChannel(TMP / "manifest.json")
    manifest = cloud_ch.publish_manifest("qwen2.5:7b", "a" * 64, "v1")
    check("云侧发布权重清单", manifest["contains_business_data"] is False and manifest["direction"] == "cloud -> device")
    check("端侧拉取权重清单", cloud_ch.pull_manifest()["model"] == "qwen2.5:7b")
    probe = TMP / "weights.bin"
    probe.write_bytes(b"abc")
    check("权重 sha256 校验正确", cloud_ch.verify_weights(probe, hashlib.sha256(b"abc").hexdigest()))
    check("权重 sha256 校验能识错", not cloud_ch.verify_weights(probe, "0" * 64))
    try:
        cloud_ch.push_business_data("客户名单")
        cloud_blocked = False
    except edge.PolicyViolation:
        cloud_blocked = True
    check("云侧上传业务数据被策略拦截", cloud_blocked)
    check("拦截留审计且不含业务原文", cloud_ch.audit[-1]["event"] == "blocked_business_data"
          and "客户名单" not in json.dumps(cloud_ch.audit, ensure_ascii=False))

    # 真实边侧 HTTP 节点
    node = edge.EdgeNode()
    url = node.start(port=0)
    qpath = TMP / "queue.sqlite3"
    if qpath.exists():
        qpath.unlink()
    queue = edge.OfflineQueue(qpath)
    try:
        online = edge.build_default_nodes(edge_url=url, edge_online=True)
        hub = edge.CoordinationHub(edge.TaskRouter(online), queue=queue, cloud=cloud_ch)
        routed = hub.submit("长文档分析", privacy="intranet_masked", complexity=0.9)
        check("复杂任务真实送到边侧", routed["role"] == edge.ROLE_EDGE and routed["ok"] is True, str(routed)[:200])
        check("边侧 executor 不冒充 32B", routed.get("executor") == "local_tool:search_knowledge_base")
        check("边侧服务端确实收到 Task", len(node.received) == 1)

        masked_task = hub.submit("发邮件给 li@example.com 电话 13900139000",
                                 privacy="intranet_masked", complexity=0.9)
        check("上送前完成脱敏", masked_task["masked"] is True and masked_task["mask_detail"].get("email") == 1)
        check("脱敏后文本不含 PII", "li@example.com" not in masked_task.get("masked_text", ""))

        # 边侧离线 → 排队 → 恢复 → 同步
        offline = edge.build_default_nodes(edge_url=url, edge_online=False)
        offline_hub = edge.CoordinationHub(edge.TaskRouter(offline), queue=queue, cloud=cloud_ch)
        queued = offline_hub.submit("离线批量分析", privacy="intranet_masked", complexity=0.9)
        check("边侧离线进入队列", queued["queued"] is True and queue.stats().get("pending") == 1)
        before = offline_hub.flush()
        check("边侧离线时 flush 不误报成功", before["synced"] == 0)
        flushed = hub.flush()
        check("恢复后同步成功", flushed == {"attempted": 1, "synced": 1, "failed": 0}, str(flushed))
        check("队列状态落库", queue.stats().get("synced") == 1 and not queue.stats().get("pending"))

        weight_task = hub.submit("", payload_kind="model_weights")
        check("权重任务由云侧处理", weight_task["role"] == edge.ROLE_CLOUD and weight_task["ok"] is True)
    finally:
        node.stop()
        queue.close()


def test_no_self_deception() -> None:
    """护栏：新能力不得悄悄改变已有评测口径，也不得把降级说成真 Chroma。"""
    from main import build_registry

    cli_names = set(build_registry().names())
    check("CLI 入口注册新工具", {"vector_search", "verify_quote"} <= cli_names)

    evaluator_src = (ROOT / "eval" / "evaluator.py").read_text(encoding="utf-8")
    check("评测器未引入新工具（保持两轮成绩口径）", "rag_tools" not in evaluator_src and "vector_search" not in evaluator_src)

    tasks = json.loads((ROOT / "eval" / "benchmark_tasks.json").read_text(encoding="utf-8"))
    by_id = {t["id"]: t for t in tasks["tasks"]}
    still_skipped = [tid for tid in ("C01", "C03", "C04", "C06", "C07") if by_id[tid]["requires"]]
    check("5 条复杂任务 requires 未被清空刷分", len(still_skipped) == 5, str(still_skipped))

    sources = [
        ROOT / "protocols" / "mcp_server.py", ROOT / "protocols" / "a2a.py",
        ROOT / "edge" / "coordination.py", ROOT / "memory" / "vector_store.py",
        ROOT / "tools" / "rag_tools.py",
    ]
    # 模式串拼接构造，避免卫生扫描把「检测规则本身」误判为泄露
    bs = chr(92)
    bad_patterns = ("D:" + "/" + "Projects", "C:" + bs + "Users")
    leaked = [p.name for p in sources
              if any(x in p.read_text(encoding="utf-8") for x in bad_patterns)]
    check("新模块源码无本机绝对路径", not leaked, str(leaked))
    check("知识库原 BM25 工具仍可用", ToolRegistry()
          .extend(knowledge_tools.KNOWLEDGE_TOOLS)
          .dispatch("search_knowledge_base", {"query": "国产适配"}).ok)


def main() -> int:
    test_mcp()
    test_a2a()
    test_vector_store()
    test_rag_tools()
    test_edge()
    test_no_self_deception()
    print(f"\n{'=' * 46}\n通过 {PASS} / 失败 {FAIL} / 共 {PASS + FAIL}")
    print(f"Chroma 后端：{'真实 chromadb' if vs.chromadb is not None else '未安装，降级为 fallback-hash-cosine（已如实标注）'}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
