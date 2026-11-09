"""Chroma 本地向量库 + 结构化引用溯源（方案 4.3 / 4.6）。

真实性与能力边界（**必须随实现一起说明，不要简化成"已实现语义 RAG"**）：
- 向量存储/检索用真实的 `chromadb.PersistentClient` + HNSW cosine 集合，
  持久化目录在 D 盘 `data/workspace/chroma`，不额外起服务。
- **Embedding 不是神经网络语义模型**：用纯标准库的中文 bigram hashing trick
  生成 256 维 L2 归一化向量。它只编码字面特征，同义词/近义改写召回不到。
  这样做是为了不再加载一个 embedding 模型（端侧 7B + 显存预算已紧），
  也避免默认 ONNX 模型下载到系统盘。接口 `embed(texts)` 可替换成真模型。
- chromadb 未安装 / 初始化失败时，退到纯标准库余弦索引，并在每个 Citation
  的 `backend` 字段与 `VectorStore.backend` 里如实写 `fallback-hash-cosine`，
  **绝不把降级结果标成 Chroma**。
- 引用溯源不是"模型复述路径"：每个 Citation 带源文件相对路径、片段序号、
  原文、sha256；`verify()` 重新读取源文件核对原文与哈希，`verify_answer()`
  检查回答里的 [n] 编号是否都指向可核验的引用，用于拦截幻觉引文。
- 写入按片段 sha256 幂等去重；召回按「相似度 × 时间权重」排序（方案 4.6）。

用法：
  python -m memory.vector_store --self-test
  python -m memory.vector_store --index
  python -m memory.vector_store --search "国产适配"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from core.config import config
from tools.knowledge_tools import _DATE, _chunks, _terms

try:  # 可选依赖：没装也要能让项目跑起来，但必须如实标注
    import chromadb
    from chromadb.config import Settings as ChromaSettings
except Exception:  # noqa: BLE001 - 任何导入失败都走降级，并把原因留下来
    chromadb = None
    ChromaSettings = None

EMBED_DIM = 256
COLLECTION_NAME = "office_kb"
_HAN = re.compile(r"[\u4e00-\u9fff]+")
_LATIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_CITE = re.compile(r"[\[【]\s*(\d{1,2})\s*[\]】]")


class VectorStoreUnavailable(RuntimeError):
    pass


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _digest(text: str) -> str:
    return hashlib.sha256(_norm(text).encode("utf-8")).hexdigest()


class HashingEmbedder:
    """确定性字面特征向量：中文单字 + bigram，英文数字按词，hashing trick 投影。

    这不是语义 embedding。替换成 ONNX / 本地模型时只需实现同样的
    `embed(texts) -> list[list[float]]` 与 `embed_one`。
    """

    dimension = EMBED_DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_one(t) for t in texts]

    def embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dimension
        for token in _terms(text):
            # 双哈希降低碰撞偏置，符号位保留词序无关的方向信息
            h = int(hashlib.blake2b(token.encode("utf-8"), digest_size=8).hexdigest(), 16)
            idx, sign = h % self.dimension, 1.0 if (h >> 32) & 1 else -1.0
            vec[idx] += sign * (2.0 if len(token) > 1 else 1.0)
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            return vec
        return [round(v / norm, 6) for v in vec]


def _cosine(a: list[float], b: list[float]) -> float:
    return round(sum(x * y for x, y in zip(a, b)), 6)


@dataclass
class Citation:
    """一条可核验的证据。"""

    citation_id: int
    source: str
    chunk_index: int
    text: str
    score: float
    sha256: str
    raw_score: float = 0.0
    time_weight: float = 1.0
    date_hint: str = ""
    backend: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def short(self) -> str:
        return f"[{self.citation_id}] {self.source}#片段{self.chunk_index}（相似度 {self.score}）"


class _FallbackIndex:
    """chromadb 不可用时的纯标准库余弦索引；明确标为 fallback。"""

    name = "fallback-hash-cosine"

    def __init__(self, path: Path, embedder: HashingEmbedder) -> None:
        self.path = path
        self.embedder = embedder
        self.rows: dict[str, dict[str, Any]] = {}
        if path.is_file():
            try:
                self.rows = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.rows = {}

    def upsert(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            self.rows[row["id"]] = row
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.rows, ensure_ascii=False), encoding="utf-8")

    def query(self, vector: list[float], top_k: int) -> list[dict[str, Any]]:
        scored = [(_cosine(vector, r["embedding"]), r) for r in self.rows.values()]
        scored.sort(key=lambda x: -x[0])
        return [{**r, "distance": 1.0 - s} for s, r in scored[:top_k]]

    def count(self) -> int:
        return len(self.rows)


class _ChromaIndex:
    name = "chroma"

    def __init__(self, path: Path) -> None:
        if chromadb is None:
            raise VectorStoreUnavailable("chromadb 未安装")
        self.client = chromadb.PersistentClient(
            path=str(path), settings=ChromaSettings(anonymized_telemetry=False)
        )
        self.collection = self.client.get_or_create_collection(
            name=COLLECTION_NAME, metadata={"hnsw:space": "cosine"}
        )

    def upsert(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        # Chroma 单次 upsert 有批量上限，分片写入
        for start in range(0, len(rows), 256):
            batch = rows[start : start + 256]
            self.collection.upsert(
                ids=[r["id"] for r in batch],
                embeddings=[r["embedding"] for r in batch],
                documents=[r["text"] for r in batch],
                metadatas=[r["metadata"] for r in batch],
            )

    def query(self, vector: list[float], top_k: int) -> list[dict[str, Any]]:
        out = self.collection.query(
            query_embeddings=[vector], n_results=min(top_k, max(1, self.count())),
            include=["documents", "metadatas", "distances"],
        )
        rows = []
        docs = (out.get("documents") or [[]])[0]
        metas = (out.get("metadatas") or [[]])[0]
        dists = (out.get("distances") or [[]])[0]
        for doc, meta, dist in zip(docs, metas, dists):
            rows.append({"text": doc, "metadata": meta or {}, "distance": dist})
        return rows

    def count(self) -> int:
        return int(self.collection.count())


class VectorStore:
    def __init__(
        self,
        persist_dir: str | Path | None = None,
        *,
        embedder: HashingEmbedder | None = None,
        inbox_dir: Path | None = None,
        prefer_chroma: bool = True,
    ) -> None:
        self.persist_dir = Path(persist_dir or config.workspace_dir / "chroma")
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self.inbox_dir = Path(inbox_dir or config.inbox_dir)
        self.embedder = embedder or HashingEmbedder()
        self.embedder_name = "hashing-bigram-256"
        self._lock = threading.RLock()
        self.init_error = ""
        self._index: Any = None
        if prefer_chroma:
            try:
                self._index = _ChromaIndex(self.persist_dir)
            except Exception as exc:  # noqa: BLE001 - 降级必须连同原因一起留痕
                self.init_error = f"{type(exc).__name__}: {exc}"
                self._index = _FallbackIndex(self.persist_dir / "fallback_index.json", self.embedder)
        else:
            self._index = _FallbackIndex(self.persist_dir / "fallback_index.json", self.embedder)

    @property
    def backend(self) -> str:
        return self._index.name

    @property
    def is_real_chroma(self) -> bool:
        return self.backend == "chroma"

    def stats(self) -> dict[str, Any]:
        # 输出相对路径，避免工具返回把本机绝对路径带进公开报告
        try:
            rel_dir = self.persist_dir.resolve().relative_to(config.project_root.resolve()).as_posix()
        except (ValueError, OSError):
            rel_dir = "<external>"
        return {
            "backend": self.backend,
            "is_real_chroma": self.is_real_chroma,
            "embedder": self.embedder_name,
            "embedding_is_semantic": False,
            "chunks": self._index.count(),
            "persist_dir": rel_dir,
            "init_error": self.init_error or None,
            "chromadb_available": chromadb is not None,
        }

    # ------------------------------------------------------------------
    # 索引
    # ------------------------------------------------------------------
    def _iter_chunks(self) -> list[dict[str, Any]]:
        from tools.doc_tools import _extract_text

        from tools.knowledge_tools import SUPPORTED

        out: list[dict[str, Any]] = []
        if not self.inbox_dir.is_dir():
            return out
        for path in sorted(self.inbox_dir.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in SUPPORTED:
                continue
            if any(part.startswith(".") for part in path.parts):
                continue
            try:
                text = _extract_text(path, path.suffix.lower())
            except Exception:  # noqa: BLE001 - 单个坏文档不应中断索引
                continue
            if not text.strip():
                continue
            try:
                rel = path.relative_to(config.project_root).as_posix()
            except ValueError:
                rel = path.as_posix()
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            for idx, chunk in enumerate(_chunks(text)):
                dates = _DATE.findall(chunk)
                out.append(
                    {
                        "id": _digest(f"{rel}\x00{idx}\x00{chunk}")[:24],
                        "text": chunk,
                        "embedding": self.embedder.embed_one(chunk),
                        "metadata": {
                            "source": rel,
                            "chunk_index": idx,
                            "sha256": _digest(chunk),
                            "date_hint": "、".join(dict.fromkeys(dates[:3])),
                            "mtime": float(mtime),
                            "backend": self.backend,
                        },
                    }
                )
        return out

    def index_directory(self, inbox_dir: str | Path | None = None) -> dict[str, Any]:
        if inbox_dir is not None:
            self.inbox_dir = Path(inbox_dir)
        rows = self._iter_chunks()
        # 幂等去重：同 sha256 的片段只留一条（方案 4.6 写入去重）
        seen: set[str] = set()
        unique = []
        for row in rows:
            key = row["metadata"]["sha256"]
            if key in seen:
                continue
            seen.add(key)
            unique.append(row)
        with self._lock:
            self._index.upsert(unique)
        return {
            "backend": self.backend, "scanned_chunks": len(rows),
            "indexed_chunks": len(unique), "deduped": len(rows) - len(unique),
            "embedder": self.embedder_name, "embedding_is_semantic": False,
        }

    # ------------------------------------------------------------------
    # 检索与引用溯源
    # ------------------------------------------------------------------
    @staticmethod
    def _time_weight(metadata: dict[str, Any]) -> float:
        """时间衰减：以文件 mtime 为准，半衰期约 365 天，权重区间 [0.8, 1.0]。"""
        import time

        mtime = float(metadata.get("mtime") or 0.0)
        if mtime <= 0:
            return 0.9
        age_days = max(0.0, (time.time() - mtime) / 86400.0)
        decay = math.exp(-age_days / 365.0)
        return round(0.8 + 0.2 * decay, 6)

    def search(self, query: str, top_k: int = 5) -> list[Citation]:
        q = (query or "").strip()
        if not q:
            return []
        k = max(1, min(int(top_k), 20))
        with self._lock:
            rows = self._index.query(self.embedder.embed_one(q), k)
        citations = []
        for i, row in enumerate(rows, 1):
            meta = dict(row.get("metadata") or {})
            raw = 1.0 - float(row.get("distance") or 0.0)  # cosine distance -> similarity
            weight = self._time_weight(meta)
            citations.append(
                Citation(
                    citation_id=i,
                    source=str(meta.get("source", "")),
                    chunk_index=int(meta.get("chunk_index", 0)),
                    text=str(row.get("text") or ""),
                    score=round(raw * weight, 6),
                    raw_score=round(raw, 6),
                    time_weight=weight,
                    sha256=str(meta.get("sha256", "")),
                    date_hint=str(meta.get("date_hint", "")),
                    backend=str(meta.get("backend") or self.backend),
                )
            )
        return citations

    def citation_block(self, citations: list[Citation], *, include_text: bool = True) -> str:
        """生成给模型/文章使用的可核验引用块。"""
        lines = [f"引用清单（backend={self.backend}，embedding={self.embedder_name}，非语义模型）：", ""]
        for c in citations:
            extra = f"  时间线索：{c.date_hint}" if c.date_hint else ""
            lines.append(f"{c.short()}  sha256={c.sha256[:12]}…{extra}")
            if include_text:
                preview = c.text.replace("\n", " / ")
                lines.append(f"    原文：{preview[:300]}")
        lines.append("")
        lines.append("引用规则：回答里只能用 [n] 指向上表编号；不得编造未列出的文件或原文。")
        return "\n".join(lines)

    @staticmethod
    def verify(citation: Citation | dict[str, Any]) -> dict[str, Any]:
        """重新读取源文件核对引文原文与 sha256，拦截幻觉引用。"""
        data = citation.to_dict() if isinstance(citation, Citation) else dict(citation)
        source, text, expected = data.get("source", ""), data.get("text", ""), data.get("sha256", "")
        path = (config.project_root / source).resolve()
        root = config.project_root.resolve()
        if not source or root not in path.parents or not path.is_file():
            return {"valid": False, "reason": "源文件不存在或路径越界", "source": source}
        from tools.doc_tools import _extract_text

        try:
            full = _extract_text(path, path.suffix.lower())
        except Exception as exc:  # noqa: BLE001
            return {"valid": False, "reason": f"源文件解析失败：{exc}", "source": source}
        text_found = _norm(text) in _norm(full)
        hash_match = _digest(text) == expected
        return {
            "valid": bool(text_found and hash_match and expected),
            "source": source, "text_found": text_found,
            "hash_match": bool(hash_match and expected),
            "reason": "通过" if text_found and hash_match and expected else "原文或哈希不匹配",
        }

    def verify_answer(self, answer: str, citations: list[Citation]) -> dict[str, Any]:
        """检查回答中的 [n] 编号：不能指向不存在的引用，也不能指向核验失败的引用。"""
        ids = sorted({int(m) for m in _CITE.findall(answer or "")})
        valid_ids = {c.citation_id for c in citations}
        unknown = [i for i in ids if i not in valid_ids]
        by_id = {c.citation_id: c for c in citations}
        unverified = [i for i in ids if i in by_id and not self.verify(by_id[i])["valid"]]
        return {
            "ok": bool(ids) and not unknown and not unverified,
            "referenced_ids": ids,
            "unknown_ids": unknown,
            "unverified_ids": unverified,
            "citation_count": len(citations),
            "note": "回答无任何 [n] 引用时 ok=false（不把无引用当作溯源成功）",
        }


_STORE: VectorStore | None = None
_STORE_LOCK = threading.Lock()


def get_store(persist_dir: str | Path | None = None) -> VectorStore:
    """进程内共享实例，避免每个工具调用都重开 Chroma 客户端。"""
    global _STORE
    with _STORE_LOCK:
        if _STORE is None or persist_dir is not None:
            _STORE = VectorStore(persist_dir)
        return _STORE


def self_test(tmp_dir: str | Path | None = None) -> dict[str, Any]:
    # 临时目录固定落在项目 data 区（默认非系统盘），不用系统 temp
    base = Path(tmp_dir) if tmp_dir else config.workspace_dir / "_selftest" / "vector"
    base.mkdir(parents=True, exist_ok=True)
    store = VectorStore(base / "chroma")
    indexed = store.index_directory()
    hits = store.search("国产适配", top_k=3)
    verified = [store.verify(c)["valid"] for c in hits]
    good = store.verify_answer("依据 [1] 与 [2] 的记载……", hits) if len(hits) >= 2 else {"ok": False}
    bad = store.verify_answer("依据 [9] 的记载……", hits)
    tampered = Citation(**{**hits[0].to_dict(), "text": "完全不存在的原文"}) if hits else None
    tamper_result = store.verify(tampered) if tampered else {"valid": False}
    return {
        "chromadb_available": chromadb is not None,
        "backend": store.backend,
        "is_real_chroma": store.is_real_chroma,
        "init_error": store.init_error or None,
        "indexed": indexed,
        "hits": len(hits),
        "top_sources": [c.source for c in hits],
        "all_citations_verified": all(verified) if verified else False,
        "good_answer": good,
        "hallucinated_answer": bad,
        "tampered_citation_valid": tamper_result["valid"],
        "stats": store.stats(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Chroma 向量库与引用溯源")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--index", action="store_true", help="重建 data/inbox 索引")
    parser.add_argument("--search", metavar="QUERY")
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args(argv)

    if args.self_test:
        print(json.dumps(self_test(), ensure_ascii=False, indent=2))
        return 0
    store = get_store()
    if args.index:
        print(json.dumps(store.index_directory(), ensure_ascii=False, indent=2))
    if args.search:
        citations = store.search(args.search, args.top_k)
        print(store.citation_block(citations))
        if citations:
            print("\n核验：", json.dumps([store.verify(c) for c in citations], ensure_ascii=False))
    if not (args.index or args.search):
        parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
