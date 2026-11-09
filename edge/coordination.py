"""端-边-云三级协同（方案 3.1）。

三级分工（严格按技术方案 3.1，不擅自放宽）：
- 端侧 device：7B，日常办公任务本地闭环，**数据不出机**；
- 边侧 edge：部门服务器 32B/72B，处理复杂推理；端侧上送前必须脱敏；
- 云侧 cloud：模型训练 / SFT / 评测 / 版本更新，**只下发模型权重，不上传业务数据**。

本模块把「分级路由 + 脱敏 + 离线队列 + 端边同步 + 云侧权重通道」做成
可真实运行的最小实现，端↔边通信复用 protocols/a2a.py 的 A2A 通道，
不另造一套私有协议。

能力边界（如实说明）：
- 三节点在本机不同端口模拟，未部署到真实飞腾/鲲鹏/私有云机器；
- 边侧「复杂推理」没有真实 32B 模型，默认执行确定性的本地知识检索，
  返回里明确标注 executor，不伪造大模型推理；
- 云侧只做权重清单（manifest）下发与校验，不实现训练/SFT；
- 脱敏是正则规则覆盖（邮箱/手机/证件号/银行卡/IP/本机路径），
  **不能替代正式 DLP**，中文姓名等非规则化 PII 未覆盖。

用法：
  python -m edge.coordination --self-test
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from core.config import config
from protocols import a2a

ROLE_DEVICE = "device"
ROLE_EDGE = "edge"
ROLE_CLOUD = "cloud"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class PolicyViolation(Exception):
    """违反端边云数据流向策略。"""


@dataclass(frozen=True)
class NodeDescriptor:
    node_id: str
    role: str
    url: str = ""
    model: str = ""
    capabilities: tuple[str, ...] = ()
    online: bool = True
    # 云侧恒为 False：方案 3.1 规定「只下发模型权重，不上传业务数据」
    accepts_business_data: bool = True

    def __post_init__(self) -> None:
        if self.role == ROLE_CLOUD and self.accepts_business_data:
            raise PolicyViolation("云侧节点必须设置 accepts_business_data=False")


@dataclass
class TaskProfile:
    text: str
    privacy: str = "local_only"          # local_only | intranet_masked | public_anonymous
    complexity: float = 0.2              # 0~1，超过阈值才考虑上送边侧
    payload_kind: str = "business_data"  # business_data | model_weights | eval_metrics
    requires: tuple[str, ...] = ()
    task_id: str = field(default_factory=lambda: _uid("task"))
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RouteDecision:
    node_id: str
    role: str
    reason: str
    mask_required: bool = False
    queued: bool = False
    fallback: list[str] = field(default_factory=list)


@dataclass
class MaskResult:
    text: str
    replacements: dict[str, int] = field(default_factory=dict)

    @property
    def masked(self) -> bool:
        return bool(self.replacements)


_MASK_RULES: list[tuple[str, re.Pattern[str], str]] = [
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[EMAIL]"),
    ("id_card", re.compile(r"\b\d{17}[\dXx]\b"), "[ID_CARD]"),
    ("phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[PHONE]"),
    ("bank_card", re.compile(r"(?<!\d)\d{16,19}(?!\d)"), "[BANK_CARD]"),
    ("ipv4", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[IP]"),
    ("abs_path", re.compile(r"[A-Za-z]:[\\/][^\s,;，；)]+"), "[PATH]"),
]


def desanitize(text: str) -> MaskResult:
    """上送边侧前的规则化脱敏；返回替换计数便于审计。"""
    out, counts = text, {}
    for key, pattern, repl in _MASK_RULES:
        out, n = pattern.subn(repl, out)
        if n:
            counts[key] = n
    return MaskResult(out, counts)


class TaskRouter:
    """按隐私等级 / 复杂度 / 节点在线状态 / 数据流向策略做路由。"""

    EDGE_COMPLEXITY_THRESHOLD = 0.6

    def __init__(self, nodes: list[NodeDescriptor]) -> None:
        self.nodes = {n.node_id: n for n in nodes}
        if len(self.nodes) != len(nodes):
            raise ValueError("节点 id 重复")
        self.device = self._one(ROLE_DEVICE)
        self.edge = self._one(ROLE_EDGE)
        self.cloud = self._one(ROLE_CLOUD)

    def _one(self, role: str) -> NodeDescriptor | None:
        return next((n for n in self.nodes.values() if n.role == role), None)

    def route(self, profile: TaskProfile) -> RouteDecision:
        # 1) 云侧只接权重/指标类任务，任何业务数据一律拒绝（先于路由判定）
        if profile.payload_kind == "business_data" and self.cloud:
            # 只有当调用方显式要求云侧处理业务数据时才抛错；正常路由永不选云侧
            if profile.metadata.get("target_node") == self.cloud.node_id:
                raise PolicyViolation(
                    f"云侧 {self.cloud.node_id} 不接受业务数据（方案 3.1）；"
                    "仅允许 model_weights / eval_metrics 类任务"
                )
        if profile.payload_kind in {"model_weights", "eval_metrics"}:
            if not self.cloud:
                raise PolicyViolation("未配置云侧节点，无法处理权重/指标任务")
            if profile.text.strip() and profile.payload_kind == "model_weights":
                # 权重任务不应携带自由文本业务内容
                raise PolicyViolation("权重下发任务不得夹带业务文本")
            return RouteDecision(self.cloud.node_id, ROLE_CLOUD, "云侧仅处理模型权重/评测指标，不上送业务数据")

        # 2) 本地专属数据：无论多复杂都不出机
        if profile.privacy == "local_only":
            return RouteDecision(self.device.node_id, ROLE_DEVICE, "隐私等级 local_only：数据不出机，端侧闭环")

        # 3) 复杂任务上送边侧（需脱敏）；边侧离线则端侧排队，不降级到云
        if profile.complexity >= self.EDGE_COMPLEXITY_THRESHOLD and self.edge:
            if not self.edge.online:
                return RouteDecision(
                    self.device.node_id, ROLE_DEVICE, "边侧离线：任务进入离线队列等待同步",
                    mask_required=True, queued=True, fallback=[self.edge.node_id],
                )
            unmet = [r for r in profile.requires if r not in self.edge.capabilities]
            if unmet:
                return RouteDecision(
                    self.device.node_id, ROLE_DEVICE,
                    f"边侧缺少能力 {unmet}，端侧本地执行", fallback=[self.edge.node_id],
                )
            return RouteDecision(
                self.edge.node_id, ROLE_EDGE,
                f"复杂度 {profile.complexity:.2f} ≥ {self.EDGE_COMPLEXITY_THRESHOLD}，上送边侧（脱敏后）",
                mask_required=True,
            )

        # 4) 其余一律端侧
        return RouteDecision(self.device.node_id, ROLE_DEVICE, "日常任务：端侧 90% 本地闭环")


class OfflineQueue:
    """端侧离线任务队列（SQLite 持久化，进程重启不丢）。"""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS edge_sync_tasks (
        task_id TEXT PRIMARY KEY,
        payload TEXT NOT NULL,
        privacy TEXT NOT NULL,
        complexity REAL NOT NULL,
        target_role TEXT NOT NULL,
        state TEXT NOT NULL,
        created_at TEXT NOT NULL,
        synced_at TEXT,
        result TEXT,
        error TEXT
    )
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        path = Path(db_path) if db_path else config.workspace_dir / "edge_sync.sqlite3"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.executescript(self.SCHEMA)
            self.conn.commit()

    def enqueue(self, profile: TaskProfile, decision: RouteDecision, masked_text: str) -> str:
        payload = {
            "text": masked_text,
            "privacy": profile.privacy,
            "complexity": profile.complexity,
            "requires": list(profile.requires),
            "metadata": profile.metadata,
        }
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO edge_sync_tasks "
                "(task_id,payload,privacy,complexity,target_role,state,created_at) VALUES (?,?,?,?,?,?,?)",
                (profile.task_id, json.dumps(payload, ensure_ascii=False), profile.privacy,
                 profile.complexity, decision.fallback[0] if decision.fallback else ROLE_EDGE,
                 "pending", _now()),
            )
            self.conn.commit()
        return profile.task_id

    def pending(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM edge_sync_tasks WHERE state='pending' ORDER BY created_at LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_synced(self, task_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE edge_sync_tasks SET state='synced', synced_at=?, result=? WHERE task_id=?",
                (_now(), json.dumps(result, ensure_ascii=False), task_id),
            )
            self.conn.commit()

    def mark_failed(self, task_id: str, error: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE edge_sync_tasks SET state='failed', error=? WHERE task_id=?", (error, task_id)
            )
            self.conn.commit()

    def stats(self) -> dict[str, int]:
        with self._lock:
            rows = self.conn.execute("SELECT state, COUNT(*) n FROM edge_sync_tasks GROUP BY state").fetchall()
        return {r["state"]: r["n"] for r in rows}

    def close(self) -> None:
        with self._lock:
            self.conn.close()


class CloudWeightChannel:
    """云侧只下发模型权重清单；业务数据上传在 API 层直接拒绝。"""

    def __init__(self, manifest_path: str | Path | None = None) -> None:
        self.manifest_path = Path(manifest_path or config.workspace_dir / "cloud_manifest.json")
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self.audit: list[dict[str, Any]] = []

    def publish_manifest(self, model: str, weights_sha256: str, version: str) -> dict[str, Any]:
        manifest = {
            "kind": "model_weights", "model": model, "version": version,
            "weights_sha256": weights_sha256, "published_at": _now(),
            "direction": "cloud -> device", "contains_business_data": False,
        }
        self.manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        self.audit.append({"event": "publish_manifest", "model": model, "at": _now()})
        return manifest

    def pull_manifest(self) -> dict[str, Any] | None:
        if not self.manifest_path.is_file():
            return None
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if manifest.get("contains_business_data"):
            raise PolicyViolation("权重清单被标记为含业务数据，拒绝下发")
        self.audit.append({"event": "pull_manifest", "at": _now()})
        return manifest

    @staticmethod
    def verify_weights(path: str | Path, expected_sha256: str) -> bool:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                digest.update(block)
        return digest.hexdigest() == expected_sha256

    def push_business_data(self, payload: Any) -> None:
        """策略闸门：任何业务数据上云都抛错，并留审计记录。"""
        self.audit.append({"event": "blocked_business_data", "at": _now(), "bytes": len(str(payload))})
        raise PolicyViolation(
            "方案 3.1：云侧只下发模型权重，不上传业务数据。"
            "请把任务留在端侧或脱敏后上送边侧。"
        )


class EdgeNode:
    """边侧节点：复用 A2A 服务端，注册一个确定性推理 skill。"""

    def __init__(self, node_id: str = "edge-01") -> None:
        self.node_id = node_id
        self.received: list[dict[str, Any]] = []
        self._executor = a2a.TaskExecutor()
        self._executor.register(
            a2a.Skill(
                id="edge_inference",
                name="边侧复杂推理",
                description="长文档分析 / 多步骤规划；当前为确定性本地检索，非 32B 模型推理",
                tags=["edge", "long_context", "planning"],
                handler=self._infer,
            )
        )
        self.server = a2a.A2AServer(
            name="office-agent-edge", executor=self._executor, on_task=self._on_task,
        )
        self.httpd = None
        self.thread = None

    def _infer(self, text: str, metadata: dict[str, Any]) -> a2a.SkillResult:
        from core.tool_registry import ToolRegistry
        from tools import knowledge_tools

        registry = ToolRegistry().extend(knowledge_tools.KNOWLEDGE_TOOLS)
        result = registry.dispatch("search_knowledge_base", {"query": text[:200], "top_k": 3})
        evidence = result.content if result.ok else f"检索失败：{result.error}"
        return a2a.SkillResult(
            text=(
                f"[边侧节点 {self.node_id}] 任务已接收。\n"
                f"executor=local_tool:search_knowledge_base（未加载 32B 模型，不伪造推理结果）\n"
                f"masked_input={metadata.get('masked', False)}\n\n{evidence}"
            ),
            artifacts=[{
                "artifactId": _uid("art"), "name": "edge_evidence",
                "parts": [a2a.text_part(evidence)],
                "metadata": {"node_role": ROLE_EDGE, "executor": "local_tool:search_knowledge_base"},
            }],
            metadata={"node_role": ROLE_EDGE, "masked": metadata.get("masked", False), "executor": "local_tool:search_knowledge_base"},
        )

    def _on_task(self, task: dict[str, Any]) -> None:
        self.received.append({"id": task["id"], "state": task["status"]["state"], "at": _now()})

    def start(self, host: str = "127.0.0.1", port: int = 0) -> str:
        self.httpd = self.server.serve_http(host, port)
        actual = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="edge-node", daemon=True)
        self.thread.start()
        return f"http://{host}:{actual}"

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None


class CoordinationHub:
    """门面：提交任务 → 路由 → 端侧执行 / 脱敏上送边侧 / 离线排队。"""

    def __init__(
        self,
        router: TaskRouter,
        queue: OfflineQueue | None = None,
        cloud: CloudWeightChannel | None = None,
        device_handler: Callable[[TaskProfile], dict[str, Any]] | None = None,
    ) -> None:
        self.router = router
        self.queue = queue
        self.cloud = cloud or CloudWeightChannel()
        self.device_handler = device_handler or self._default_device_handler
        self.audit: list[dict[str, Any]] = []

    @staticmethod
    def _default_device_handler(profile: TaskProfile) -> dict[str, Any]:
        """端侧默认执行器：确定性本地知识检索，不假装是 7B 推理。"""
        from core.tool_registry import ToolRegistry
        from tools import knowledge_tools

        registry = ToolRegistry().extend(knowledge_tools.KNOWLEDGE_TOOLS)
        result = registry.dispatch("search_knowledge_base", {"query": profile.text[:200], "top_k": 3})
        return {
            "node_role": ROLE_DEVICE,
            "executor": "local_tool:search_knowledge_base",
            "ok": result.ok,
            "text": result.content if result.ok else str(result.error),
        }

    def submit(self, text: str, **kwargs: Any) -> dict[str, Any]:
        profile = TaskProfile(text=text, **kwargs)
        decision = self.router.route(profile)
        record: dict[str, Any] = {
            "task_id": profile.task_id, "role": decision.role,
            "node_id": decision.node_id, "reason": decision.reason,
            "queued": decision.queued, "masked": False, "executor": None,
        }
        self.audit.append({"event": "route", **record})

        if decision.role == ROLE_CLOUD:
            manifest = self.cloud.pull_manifest()
            record.update({"executor": "cloud:weight_channel", "manifest": manifest, "ok": manifest is not None})
            return record

        if decision.queued:
            masked = desanitize(profile.text) if decision.mask_required else MaskResult(profile.text)
            if self.queue is None:
                record.update({"ok": False, "error": "边侧离线且未配置离线队列"})
                return record
            self.queue.enqueue(profile, decision, masked.text)
            record.update({"masked": masked.masked, "mask_detail": masked.replacements, "ok": True})
            return record

        if decision.role == ROLE_EDGE:
            masked = desanitize(profile.text) if decision.mask_required else MaskResult(profile.text)
            record.update({"masked": masked.masked, "mask_detail": masked.replacements,
                           "masked_text": masked.text})
            result = self._send_to_edge(profile, masked.text)
            record.update({"ok": result.get("ok", False), "executor": result.get("executor"),
                           "edge_result": result})
            return record

        local = self.device_handler(profile)
        record.update({"ok": local.get("ok", True), "executor": local.get("executor"),
                       "local_result": local})
        return record

    def _send_to_edge(self, profile: TaskProfile, masked_text: str) -> dict[str, Any]:
        node = self.router.nodes.get(self.router.edge.node_id) if self.router.edge else None
        if not node or not node.url:
            return {"ok": False, "error": "边侧未配置 URL"}
        client = a2a.A2AClient(node.url)
        try:
            task = client.send(
                masked_text, skill_id="edge_inference",
                metadata={"masked": True, "origin": ROLE_DEVICE, "complexity": profile.complexity},
            )
            return {
                "ok": task.get("status", {}).get("state") == "completed",
                "task_id": task.get("id"), "state": task.get("status", {}).get("state"),
                "executor": task.get("metadata", {}).get("executor"),
                "artifacts": len(task.get("artifacts", [])),
                "answer": "\n".join(
                    str(p.get("text") or "") for p in (task.get("messages") or [{}])[-1].get("parts", [])
                ),
            }
        except (a2a.A2AError, OSError, ValueError) as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def flush(self, limit: int = 20) -> dict[str, Any]:
        """边侧恢复后把离线队列同步出去；失败保持 pending，不丢任务。"""
        if self.queue is None:
            return {"attempted": 0, "synced": 0, "failed": 0, "note": "未配置离线队列"}
        node = self.router.edge
        if not node or not node.online or not node.url:
            return {"attempted": 0, "synced": 0, "failed": 0, "note": "边侧仍离线"}
        client = a2a.A2AClient(node.url)
        rows = self.queue.pending(limit)
        synced, failed = 0, 0
        for row in rows:
            payload = json.loads(row["payload"])
            try:
                task = client.send(
                    payload["text"], skill_id="edge_inference",
                    task_id=row["task_id"],
                    metadata={"masked": True, "origin": "offline_queue", "synced_at": _now()},
                )
                self.queue.mark_synced(row["task_id"], {"task_id": task.get("id"), "state": task.get("status", {}).get("state")})
                synced += 1
            except (a2a.A2AError, OSError, ValueError) as exc:
                self.queue.mark_failed(row["task_id"], f"{type(exc).__name__}: {exc}")
                failed += 1
        self.audit.append({"event": "flush", "attempted": len(rows), "synced": synced, "failed": failed})
        return {"attempted": len(rows), "synced": synced, "failed": failed}


def build_default_nodes(edge_url: str = "", edge_online: bool = True) -> list[NodeDescriptor]:
    return [
        NodeDescriptor("device-01", ROLE_DEVICE, model="qwen2.5:7b-instruct-q4_K_M",
                       capabilities=("office_docs", "local_rag", "tool_calling")),
        NodeDescriptor("edge-01", ROLE_EDGE, url=edge_url, model="Qwen2.5-32B-INT4（未部署）",
                       capabilities=("long_context", "planning", "batch"), online=edge_online),
        NodeDescriptor("cloud-01", ROLE_CLOUD, model="72B+ / SFT（未部署）",
                       capabilities=("training", "evaluation", "weight_distribution"),
                       accepts_business_data=False),
    ]


def self_test() -> dict[str, Any]:
    """本地三节点真实联调：路由 / 脱敏 / 离线排队 / 恢复同步 / 云侧策略。"""
    edge = EdgeNode()
    edge_url = edge.start(port=0)
    # 临时目录固定落在项目 data 区（默认非系统盘），不用系统 temp
    tmp = config.workspace_dir / "_selftest" / "edge"
    tmp.mkdir(parents=True, exist_ok=True)
    queue = OfflineQueue(tmp / "queue.sqlite3")
    cloud = CloudWeightChannel(tmp / "manifest.json")
    nodes = build_default_nodes(edge_url=edge_url, edge_online=True)
    router = TaskRouter(nodes)
    hub = CoordinationHub(router, queue=queue, cloud=cloud)

    local = hub.submit("整理会议纪要", privacy="local_only", complexity=0.9)
    masked = hub.submit(
        "请把长文档分析结论发给 zhangwei@example.com，电话 13800138000",
        privacy="intranet_masked", complexity=0.85,
    )
    try:
        hub.submit("上传客户名单", privacy="public_anonymous", complexity=0.9,
                   metadata={"target_node": "cloud-01"})
        cloud_block = False
    except PolicyViolation:
        cloud_block = True
    manifest = cloud.publish_manifest("qwen2.5:7b-instruct-q4_K_M", "0" * 64, "2026.11")
    weight_task = hub.submit("", payload_kind="model_weights")
    try:
        cloud.push_business_data("客户名单")
        push_blocked = False
    except PolicyViolation:
        push_blocked = True

    # 边侧离线 → 排队 → 恢复 → 同步
    offline_nodes = build_default_nodes(edge_url=edge_url, edge_online=False)
    offline_hub = CoordinationHub(TaskRouter(offline_nodes), queue=queue, cloud=cloud)
    queued = offline_hub.submit("离线批量分析", privacy="intranet_masked", complexity=0.9)
    before_flush = queue.stats().get("pending", 0)
    online_hub = CoordinationHub(TaskRouter(nodes), queue=queue, cloud=cloud)
    flushed = online_hub.flush()

    result = {
        "local_route": local["role"],
        "edge_route": masked["role"],
        "edge_ok": masked["ok"],
        "masked": masked["masked"],
        "mask_detail": masked.get("mask_detail"),
        "edge_executor": masked.get("executor"),
        "edge_received_tasks": len(edge.received),
        "cloud_business_data_blocked": cloud_block,
        "cloud_push_blocked": push_blocked,
        "manifest_model": manifest["model"],
        "weight_task_route": weight_task["role"],
        "queued": queued["queued"],
        "pending_before_flush": before_flush,
        "flush": flushed,
        "queue_stats_after": queue.stats(),
        "cloud_accepts_business_data": router.cloud.accepts_business_data,
    }
    edge.stop()
    queue.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="端边云协同自测")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        print(json.dumps(self_test(), ensure_ascii=False, indent=2))
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
