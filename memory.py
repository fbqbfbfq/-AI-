# -*- coding: utf-8 -*-
"""
记忆模块:L1 短期原文(JSON 落盘) + L2 长期要点(向量检索) + 用户画像(常驻提示词)。

v2 变更(见 UPGRADE_DESIGN.md):
- 修复 P1:压缩/向量化/入库任一失败都「不标记已总结」,记忆不丢。
- 修复 P5:批量 embedding(一次请求)。
- 修复 P6:重启后持续补压,直到所有未总结轮次处理完。
- L2 增加字段:importance/emotion/emotion_value/source/access_count/
  last_accessed/last_proactive_used_at/created_at。
- 压缩器结构化输出(要点+重要度+情绪),写库前按余弦相似度去重/更新。
- 检索按「0.7×相似度 + 0.3×动态重要度」排序,动态重要度带 14 天半衰期;
  相似度低于 retrieval_min_similarity(默认 0.45)的记忆不召回。
- 画像改为「用户消息驱动」:UserMessageBuffer 把用户原始消息攒在内存+JSON,
  满 profile_message_batch(默认 20)条后台提取一次画像并与旧画像融合,成功后清空。

- EmbeddingClient : 阿里云 text-embedding-v4(1024 维,归一化),支持批量
- L1Store         : L1 短期记忆,JSON 落盘 + 内存持有(write-through)
- MemoryStore     : L2 元数据(json) + 向量(float32 经 ChromaDB 持久化),检索带重要度/情绪
- UserMessageBuffer: 用户消息缓冲(画像提取用),内存 + JSON 落盘,满批提取后清理
- Compressor      : 用分析模型把一段对话压缩成结构化长期记忆
- ProfileUpdater  : 用户画像增量更新
- MemoryManager   : L1 + 后台压缩编排(默认每 10 轮总结一次,重启可恢复)
                    + 用户消息缓冲/画像提取编排(默认满 20 条提取一次)
"""
import datetime
import json
import math
import queue
import re
import threading
import time
from pathlib import Path

import numpy as np
import requests

import config
import file_util
import logger


# ---------- Embedding ----------

class EmbeddingClient:
    """阿里云 DashScope OpenAI 兼容 embedding 客户端。"""

    def __init__(self, api_key, base_url, model="text-embedding-v4", dim=1024):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dim = dim

    def embed(self, text):
        """返回归一化后的 (dim,) float32 向量。"""
        return self.embed_batch([text])[0]

    def embed_batch(self, texts):
        """批量返回归一化向量列表 [(dim,) float32, ...]。一次请求,省时省钱(P5)。"""
        if not texts:
            return []
        resp = requests.post(
            f"{self.base_url}/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            json={"model": self.model, "input": list(texts), "dimensions": self.dim},
            timeout=60,
        )
        resp.raise_for_status()
        out = []
        for item in resp.json()["data"]:
            vec = np.asarray(item["embedding"], dtype=np.float32)
            norm = float(np.linalg.norm(vec))
            if norm > 0:
                vec = vec / norm
            out.append(vec)
        return out


# ---------- L1 短期记忆(落盘) ----------

class L1Store:
    """L1 短期记忆:JSON 落盘 + 内存持有(write-through),每条带时间。"""

    def __init__(self, path, maxlen=40):
        self.path = Path(path)
        self.dir = self.path.parent
        self.file_name = self.path.stem
        self.maxlen = maxlen
        self.turns = []          # [{"time","user","assistant","summarized"}]
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.turns = data if isinstance(data, list) else []
                self.turns = self.turns[-self.maxlen:]
            except Exception as exc:
                self.turns = []
                try:
                    self.path.unlink()
                except Exception as unlink_exc:
                    logger.log_exception("L1 损坏文件删除失败", path=str(self.path),
                                         error=str(unlink_exc))
                logger.log_error("L1 记忆文件损坏,已删除并重建",
                                 path=str(self.path), error=str(exc))
                self._save()
        else:
            self._save()

    def _save(self):
        file_util.atomic_save_json(self.dir, self.file_name, self.turns)

    def add(self, user_text, assistant_text, now_str):
        with self._lock:
            self.turns.append({
                "time": now_str,
                "user": user_text,
                "assistant": assistant_text,
                "summarized": False,
            })
            if len(self.turns) > self.maxlen:
                self.turns = self.turns[-self.maxlen:]
            self._save()

    def recent(self, n=10):
        with self._lock:
            return list(self.turns[-n:])

    def all(self):
        with self._lock:
            return list(self.turns)

    def unsummarized(self):
        with self._lock:
            return [t for t in self.turns if not t.get("summarized", False)]

    def unsummarized_count(self):
        with self._lock:
            return sum(1 for t in self.turns if not t.get("summarized", False))

    def mark_summarized(self, count):
        with self._lock:
            marked = 0
            for t in self.turns:
                if marked >= count:
                    break
                if not t.get("summarized", False):
                    t["summarized"] = True
                    marked += 1
            self._save()

    def clear(self):
        with self._lock:
            self.turns = []
            self._save()

    def remove_at(self, index):
        """删除第 index 轮(0 起)并立即落盘;索引越界则忽略。"""
        with self._lock:
            if 0 <= index < len(self.turns):
                self.turns.pop(index)
                self._save()

    def reload(self):
        """重读磁盘文件(跨进程同步:网页删除/清空后,bot 进程下一轮前自动同步)。"""
        with self._lock:
            self.turns = []
            self._load()


# ---------- 用户消息缓冲(画像提取用) ----------

class UserMessageBuffer:
    """用户消息缓冲:内存 + JSON 落盘(单用户设计,多角色共用同一份)。

    - add:每条用户消息(非空)带时间追加,立即落盘。
    - claim:攒满 batch 条时原子认领这批(认领期间新消息继续累积,不丢)。
    - release(True):提取成功后只清掉已认领部分;失败 release(False) 保留,下次重试。
    """

    def __init__(self, path, batch=20):
        self.path = Path(path)
        self.dir = self.path.parent
        self.file_name = self.path.stem
        self.batch = max(1, int(batch))
        self.items = []          # [{"time","text"}]
        self._lock = threading.Lock()
        self._claimed = False    # 是否已有一批在提取中(防重复触发)
        self._claimed_n = 0
        self._load()

    def _load(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.items = data if isinstance(data, list) else []
            except Exception as exc:
                self.items = []
                logger.log_error("用户消息缓冲文件损坏,已重建",
                                 path=str(self.path), error=str(exc))
        if self.items:
            self._save()

    def _save(self):
        file_util.atomic_save_json(self.dir, self.file_name, self.items)

    def add(self, text, now_str):
        with self._lock:
            self.items.append({"time": now_str, "text": text})
            self._save()

    def count(self):
        with self._lock:
            return len(self.items)

    def claim(self):
        """攒满 batch 条则原子认领这批,返回消息文本列表;否则返回 None。"""
        with self._lock:
            if self._claimed or len(self.items) < self.batch:
                return None
            self._claimed = True
            self._claimed_n = len(self.items)
            return [m.get("text", "") for m in self.items[:self._claimed_n]]

    def release(self, success):
        """提取结束:成功清掉已认领部分;失败保留原样,下次重试。"""
        with self._lock:
            self._claimed = False
            if success and self._claimed_n:
                del self.items[:self._claimed_n]
            self._claimed_n = 0
            self._save()

    def clear(self):
        with self._lock:
            self.items = []
            self._claimed = False
            self._claimed_n = 0
            self._save()


_USER_MSG_BUFFERS = {}
_USER_MSG_BUFFERS_LOCK = threading.Lock()


def get_user_message_buffer(path, batch=20):
    """按路径共享的用户消息缓冲(同一 JSON 只维护一份内存实例;批大小变化自动重建)。"""
    key = str(Path(path))
    with _USER_MSG_BUFFERS_LOCK:
        buf = _USER_MSG_BUFFERS.get(key)
        if buf is None or buf.batch != int(batch):
            buf = UserMessageBuffer(path, batch=batch)
            _USER_MSG_BUFFERS[key] = buf
        return buf


# ---------- L2 长期记忆(ChromaDB) ----------

try:
    import chromadb
except ImportError:   # 依赖缺失时延迟到 MemoryStore 构造时再报错
    chromadb = None

CHROMA_COLLECTION = "l2_memories"   # 每个 (用户, 角色) 一个集合


def retrieval_k(total):
    """动态检索条数 = ⌊ln(总记忆数)⌋,下限 1,上限 10(更新计划第1条)。"""
    if total <= 0:
        return 0
    return max(1, min(10, int(math.floor(math.log(total)))))


class MemoryStore:
    """L2 记忆:ChromaDB 持久化(HNSW 余弦检索,维度按配置自适应)。

    文本进 documents,元数据进 metadatas(time/importance/emotion/emotion_value/
    source/access_count/last_accessed/last_proactive_used_at/proactive_seq/
    created_at/topic)。旧版 memory.json + vectors.npy 首次打开时自动迁移。
    """

    def __init__(self, store_dir):
        if chromadb is None:
            raise RuntimeError("缺少 chromadb 依赖,请运行: pip install chromadb")
        self.dir = Path(store_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        # 旧版 numpy 存储的文件路径(仅用于迁移检测/清理)
        self.meta_path = self.dir / "memory.json"
        self.vec_path = self.dir / "vectors.npy"
        self.chroma_dir = self.dir / "chroma"
        mi = config.load_system().get("memory_information", {})
        self._client = chromadb.PersistentClient(
            path=str(self.chroma_dir),
            settings=chromadb.Settings(anonymized_telemetry=False),
        )
        hnsw_meta = {
            "hnsw:space": "cosine",
            "hnsw:M": int(mi.get("hnsw_m", 32)),
            "hnsw:construction_ef": int(mi.get("hnsw_ef_construction", 200)),
            "hnsw:search_ef": int(mi.get("hnsw_ef_search", 60)),
        }
        self.col = self._client.get_or_create_collection(
            name=CHROMA_COLLECTION, metadata=hnsw_meta)
        self._migrate_legacy()

    # ---- 旧数据迁移 ----

    def _migrate_legacy(self):
        """旧版 memory.json + vectors.npy → ChromaDB(仅当集合为空时执行一次)。"""
        if not (self.meta_path.exists() and self.vec_path.exists()):
            return
        if self.col.count() > 0:
            return
        try:
            with open(self.meta_path, encoding="utf-8") as f:
                raw = json.load(f)
            vecs = np.load(self.vec_path)
            if not isinstance(raw, list) or len(raw) != len(vecs):
                raise ValueError("旧 L2 文件条数与向量数不一致,放弃迁移")
            ids, docs, metas, embs = [], [], [], []
            for m, v in zip(raw, vecs):
                m = self._normalize_meta(m)
                ids.append(str(m.get("id")
                               or f"l2-{int(time.time() * 1000)}-{len(ids)}"))
                docs.append((m.get("text") or "").strip())
                metas.append(self._meta_of(m))
                embs.append(np.asarray(v, dtype=np.float32).tolist())
            if ids:
                self.col.add(ids=ids, documents=docs,
                             metadatas=metas, embeddings=embs)
            self.meta_path.unlink()
            self.vec_path.unlink()
            logger.log_error("旧 L2 数据已迁移到 ChromaDB 并清理旧文件",
                             count=str(len(ids)), path=str(self.dir))
        except Exception as e:
            logger.log_exception("旧 L2 数据迁移失败(保留旧文件,可重试)",
                                 path=str(self.dir), error=str(e))

    @staticmethod
    def _normalize_meta(m):
        """兼容旧数据:缺字段补默认值。"""
        m = dict(m)
        m.setdefault("importance", 0.5)
        m.setdefault("emotion", "平静")
        m.setdefault("emotion_value", 0.0)
        m.setdefault("source", "compress")
        m.setdefault("access_count", 0)
        m.setdefault("last_accessed", "")
        m.setdefault("last_proactive_used_at", "")
        m.setdefault("proactive_seq", 0)
        m.setdefault("topic", "未分类")
        m.setdefault("created_at", m.get("time", ""))
        return m

    @staticmethod
    def _meta_of(m):
        """dict → ChromaDB 元数据(只保留字符串/数字,text 单独存 documents)。"""
        keys = ("time", "importance", "emotion", "emotion_value", "source",
                "access_count", "last_accessed", "last_proactive_used_at",
                "proactive_seq", "created_at", "topic")
        out = {k: m.get(k) for k in keys if m.get(k) is not None}
        out["importance"] = float(out.get("importance", 0.5))
        out["emotion_value"] = float(out.get("emotion_value", 0.0))
        out["access_count"] = int(out.get("access_count", 0))
        out["proactive_seq"] = int(out.get("proactive_seq", 0))
        out.setdefault("topic", "未分类")
        return out

    @staticmethod
    def _item_of(item_id, doc, meta):
        """ChromaDB 记录 → 统一 dict(与旧接口一致)。"""
        return {"id": item_id, "text": doc or "", **dict(meta)}

    # ---- 读取 ----

    def count(self):
        return self.col.count()

    def _all_records(self):
        """返回 [(id, doc, meta_dict), ...](按时间字符串升序)。"""
        data = self.col.get(include=["documents", "metadatas"])
        recs = [(i, (d or ""), dict(m or {}))
                for i, d, m in zip(data["ids"], data["documents"], data["metadatas"])]
        recs.sort(key=lambda r: str(r[2].get("time", "")))
        return recs

    def items(self):
        return [self._item_of(i, d, m) for i, d, m in self._all_records()]

    def all_topics(self):
        """全部话题名(去重排序),供网页过滤与压缩器参考。"""
        return sorted({str(m.get("topic") or "未分类")
                       for _, _, m in self._all_records()})

    # ---- 重要度 ----

    def _dynamic_importance(self, m):
        """动态重要度 = base_importance × (0.6×频率分 + 0.4×近因分)。半衰期可配。"""
        base = float(m.get("importance", 0.5))
        mi = config.load_system().get("memory_information", {})
        half_life = float(mi.get("importance_half_life_days", 14)) or 14.0

        now = datetime.datetime.now()
        created = file_util.parse_time(m.get("created_at") or m.get("time"))
        last = file_util.parse_time(m.get("last_accessed"))

        days_alive = max(1.0, (now - created).total_seconds() / 86400.0) \
            if created else 1.0
        days_since_last = max(0.0, (now - last).total_seconds() / 86400.0) \
            if last else (max(0.0, (now - created).total_seconds() / 86400.0)
                          if created else half_life)

        freq = min(1.0, math.log1p(int(m.get("access_count", 0)) / days_alive)
                   / math.log1p(20.0))
        recency = math.exp(-days_since_last * math.log(2.0) / half_life)
        return base * (0.6 * freq + 0.4 * recency)

    # ---- 写入(带去重) ----

    @staticmethod
    def _make_id(stamp, idx):
        return f"l2-{stamp}-{idx}"

    def add_items(self, items, vectors, dedup_threshold=0.85):
        """items: list[dict] 含 text/importance/emotion/emotion_value/topic;
        vectors: list[np.ndarray] 归一化。写库前按余弦相似度去重:高度重叠则更新旧条目。"""
        if not items:
            return
        now_str = file_util.format_time()
        stamp = int(time.time() * 1000)
        vecs = [np.asarray(v, dtype=np.float32) for v in vectors]

        existing = self._all_records()
        emb_matrix = None
        if existing:
            data = self.col.get(ids=[i for i, _, _ in existing],
                                include=["embeddings"])
            emb_matrix = np.asarray(data["embeddings"], dtype=np.float32)

        new_ids, new_docs, new_metas, new_embs = [], [], [], []
        up_ids, up_docs, up_metas, up_embs = [], [], [], []
        for i, it in enumerate(items):
            v = vecs[i]
            imp = max(0.0, min(1.0, float(it.get("importance", 0.5))))
            hit = None
            if emb_matrix is not None and len(emb_matrix) > 0:
                sims = emb_matrix @ v
                j = int(np.argmax(sims))
                if float(sims[j]) >= dedup_threshold:
                    hit = existing[j]
            if hit is not None:
                old_id, old_doc, old = hit
                merged = self._normalize_meta(old)
                merged["text"] = (it.get("text") or "").strip()
                merged["importance"] = imp            # 重算重要度(分析模型最新判断)
                merged["emotion"] = it.get("emotion", merged.get("emotion", "平静"))
                merged["emotion_value"] = float(it.get(
                    "emotion_value", merged.get("emotion_value", 0.0)))
                merged["source"] = it.get("source", "compress")
                merged["topic"] = str(it.get("topic") or merged.get("topic") or "未分类")
                merged["time"] = now_str               # 刷新更新时间
                merged["last_accessed"] = now_str
                merged["access_count"] = int(merged.get("access_count", 0)) + 1
                # created_at 保留(存活天数不重置)
                up_ids.append(old_id)
                up_docs.append(merged["text"])
                up_metas.append(self._meta_of(merged))
                up_embs.append(v.tolist())
                continue
            mid = self._make_id(stamp, i)
            meta = {
                "time": now_str,
                "importance": imp,
                "emotion": str(it.get("emotion", "平静")),
                "emotion_value": float(it.get("emotion_value", 0.0)),
                "source": str(it.get("source", "compress")),
                "topic": str(it.get("topic") or "未分类"),
                "access_count": 0,
                "last_accessed": "",
                "last_proactive_used_at": "",
                "proactive_seq": 0,
                "created_at": now_str,
            }
            new_ids.append(mid)
            new_docs.append((it.get("text") or "").strip())
            new_metas.append(self._meta_of(meta))
            new_embs.append(v.tolist())

        if up_ids:
            self.col.upsert(ids=up_ids, documents=up_docs,
                            metadatas=up_metas, embeddings=up_embs)
        if new_ids:
            self.col.add(ids=new_ids, documents=new_docs,
                         metadatas=new_metas, embeddings=new_embs)

    # ---- 检索 ----

    def search(self, query_vec, k=5, sim_weight=0.7, min_similarity=0.45):
        """返回按综合分降序的 top-K;综合分 = sim_weight×相似度 + (1-sim_weight)×动态重要度。

        相似度低于 min_similarity(默认 0.45,即 45% 以上才检索)的候选直接不召回,
        也不计访问统计。先从 ChromaDB 取更多候选(max(3k, 30),上限为总数),
        再过滤门槛、混合加权重排取前 k(命中不足 k 就少返回)。
        """
        total = self.col.count()
        if total <= 0 or k <= 0:
            return []
        q = np.asarray(query_vec, dtype=np.float32)
        cand = min(total, max(3 * k, 30))
        res = self.col.query(query_embeddings=[q.tolist()], n_results=cand,
                             include=["documents", "metadatas", "distances"])
        ids = res["ids"][0]
        docs = res["documents"][0]
        metas = res["metadatas"][0]
        dists = res["distances"][0]

        scored = []
        for cid, doc, meta, dist in zip(ids, docs, metas, dists):
            m = dict(meta)
            sim = float(max(0.0, 1.0 - dist))
            if sim < min_similarity:
                continue    # 相似度不足 45%,不召回
            dyn = float(self._dynamic_importance(m))
            score = sim_weight * sim + (1.0 - sim_weight) * dyn
            scored.append((score, sim, dyn, cid, doc or "", m))
        scored.sort(key=lambda x: -x[0])
        top = scored[:min(k, len(scored))]

        now_str = file_util.format_time()
        out = []
        for score, sim, dyn, cid, doc, m in top:
            out.append({
                "id": cid,
                "text": doc,
                "time": m.get("time", ""),
                "score": float(score),
                "similarity": float(sim),
                "importance": m.get("importance", 0.5),
                "dynamic_importance": float(dyn),
                "emotion": m.get("emotion", "平静"),
                "emotion_value": m.get("emotion_value", 0.0),
                "topic": m.get("topic", "未分类"),
            })
            m["access_count"] = int(m.get("access_count", 0)) + 1
            m["last_accessed"] = now_str
            self.col.update(ids=[cid], metadatas=[self._meta_of(m)])
        return out

    def top_by_importance(self, limit=1):
        """按动态重要度降序取前 limit 条(不更新访问统计)。"""
        recs = self._all_records()
        if not recs:
            return []
        scored = [(self._dynamic_importance(m), i, d, m) for i, d, m in recs]
        scored.sort(key=lambda x: -x[0])
        out = []
        for dyn, i, d, m in scored[:limit]:
            out.append({**self._item_of(i, d, m), "dynamic_importance": float(dyn)})
        return out

    def pick_proactive_topic(self, now_str=None, reuse_after=3, mark=True):
        """取「动态重要度高 且 最近未用作主动话题」的记忆(防重复)。

        - 每条记忆带 proactive_seq;最近 reuse_after 次主动用过的先排除,
          条目太少导致无候选时才允许回退复用。
        - mark=False(网页预览)只挑不记:不推进轮换序号、不落盘。
        """
        recs = self._all_records()
        if not recs:
            return None
        now_str = now_str or file_util.format_time()
        cur_seq = max((int(m.get("proactive_seq") or 0) for _, _, m in recs),
                      default=0) + 1
        cands = [r for r in recs
                 if (int(r[2].get("proactive_seq") or 0)) == 0
                 or cur_seq - (int(r[2].get("proactive_seq") or 0)) >= reuse_after]
        pool = cands if cands else recs
        scored = [(self._dynamic_importance(m),
                   str(m.get("last_proactive_used_at") or ""), i, d, m)
                  for i, d, m in pool]
        # 重要度降序;last_used 升序("" 排最前 = 最久远)
        scored.sort(key=lambda x: (-x[0], x[1]))
        _, _, i, d, m = scored[0]
        if mark:
            m["last_proactive_used_at"] = now_str
            m["proactive_seq"] = cur_seq
            self.col.update(ids=[i], metadatas=[self._meta_of(m)])
        return {
            "id": i,
            "text": d or "",
            "time": m.get("time", ""),
            "importance": m.get("importance", 0.5),
            "emotion": m.get("emotion", "平静"),
            "emotion_value": m.get("emotion_value", 0.0),
            "topic": m.get("topic", "未分类"),
        }

    # ---- 删除 / 清空 / 重读 ----

    def remove_by_id(self, item_id):
        """按 id 删除一条(文档+元数据+向量);找不到返回 False。"""
        if not self.col.get(ids=[str(item_id)])["ids"]:
            return False
        self.col.delete(ids=[str(item_id)])
        return True

    def clear(self):
        ids = self.col.get()["ids"]
        if ids:
            self.col.delete(ids=ids)
        # 旧版遗留文件一并清理
        for legacy in (self.meta_path, self.vec_path):
            if legacy.exists():
                try:
                    legacy.unlink()
                except Exception as e:
                    logger.log_exception("旧 L2 文件清理失败", path=str(legacy),
                                         error=str(e))

    def reload(self):
        """ChromaDB 每次查询都读最新数据,无需手动重读;保留接口兼容。"""
        return None


# ---------- Compressor ----------

COMPRESS_SYSTEM = (
    "你是记忆压缩器。把对话按话题提炼成值得长期记住的要点(对话常包含多个话题,"
    "如美食、玩笑、科技等,要按话题分别产出记忆),并为每条要点给出"
    "话题名、重要度(0~1,越高越重要)和这段对话里 AI 的情绪基调。只输出 JSON。"
)


def _dialogue_text(turns):
    lines = []
    for t in turns:
        prefix = f"[{t.get('time', '')}] " if t.get("time") else ""
        if t.get("user"):
            lines.append(f"{prefix}用户: {t['user']}")
        if t.get("assistant"):
            lines.append(f"{prefix}AI: {t['assistant']}")
    return "\n".join(lines)


class Compressor:
    """用分析模型把一批对话(默认 10 轮)按话题压缩成结构化长期记忆。"""

    def __init__(self, complete_fn):
        self.complete = complete_fn   # 签名: complete(messages) -> str

    def compress(self, turns, profile=None, existing_items=None):
        existing_text = ""
        existing_topics = []
        if existing_items:
            recent = [e for e in existing_items if (e.get("text") or "").strip()][:20]
            existing_text = "\n".join(f"- {e.get('text', '')}" for e in recent)
            existing_topics = sorted({str(e.get("topic") or "未分类")
                                      for e in existing_items
                                      if (e.get("topic") or "").strip()})

        parts = ["以下是最近的一段对话(每行前带发生时间):\n" + _dialogue_text(turns)]
        if profile:
            parts.append("当前用户画像(已知稳定事实,避免重复抽取):\n"
                         + json.dumps(profile, ensure_ascii=False))
        if existing_text:
            parts.append("已有长期记忆(供去重/纠错参考;若新信息与某条重叠或推翻它,"
                         "请在要点里体现更新后的结论):\n" + existing_text)
        if existing_topics:
            parts.append("已有话题列表(尽量归入已有话题;确实没有合适的再新建一个"
                         "3~6字的话题名):\n" + "、".join(existing_topics))
        parts.append(
            "请从中提取值得长期记住的信息,按话题分别产出,以 JSON 数组返回。"
            "每个元素格式:\n"
            '{"text": "10~40字简洁要点", "topic": "3~6字话题名", '
            '"importance": 1~5整数(越高越重要), '
            '"emotion": "平静|开心|兴奋|难过|生气|焦虑|害羞", '
            '"emotion_value": -1.0~1.0浮点(负=负面,正=正面)}\n'
            "最多 5 条;没有值得记的就返回 []。只返回 JSON,不要解释。"
        )
        prompt = "\n\n".join(parts)
        messages = [
            {"role": "system", "content": COMPRESS_SYSTEM},
            {"role": "user", "content": prompt},
        ]
        # 推理模型偶发「思维链吃满预算 → content 为空/截断」;空响应或
        # 解析不出 JSON 数组时重试,连续失败抛异常(由 MemoryManager 保持未总结)。
        last_raw = ""
        for attempt in range(3):
            raw = (self.complete(messages) or "").strip()
            last_raw = raw
            if not raw:
                time.sleep(2 ** attempt)
                continue
            items = self._parse(raw)
            if items or re.fullmatch(r"\[\s*\]", raw):
                return items    # 解析出条目,或模型明确回答「没有值得记的」
            time.sleep(2 ** attempt)
        logger.log_exception("压缩模型连续返回空/非法响应,放弃本批",
                             raw=str(last_raw)[:200])
        raise RuntimeError("压缩模型连续返回空/非法响应")

    @staticmethod
    def _parse(raw):
        try:
            s = raw.strip()
            start = s.find("[")
            end = s.rfind("]")
            if start != -1 and end != -1 and end > start:
                data = json.loads(s[start:end + 1])
                # 兼容模型偶尔输出 {"memories": [...]} 之类的对象包裹
                if isinstance(data, dict):
                    for v in data.values():
                        if isinstance(v, list):
                            data = v
                            break
                    else:
                        return []
                if isinstance(data, list):
                    out = []
                    for x in data:
                        if not isinstance(x, dict):
                            continue
                        text = str(x.get("text", "")).strip()
                        if not text:
                            continue
                        try:
                            imp = float(x.get("importance", 0.5))
                        except (TypeError, ValueError):
                            imp = 0.5
                        if imp > 1.5:
                            imp = imp / 5.0        # 1~5 分制 → 归一化到 0~1
                        imp = max(0.0, min(1.0, imp))
                        try:
                            ev = float(x.get("emotion_value", 0.0))
                        except (TypeError, ValueError):
                            ev = 0.0
                        ev = max(-1.0, min(1.0, ev))
                        topic = str(x.get("topic") or "未分类").strip()
                        out.append({
                            "text": text,
                            "topic": topic[:12] or "未分类",
                            "importance": imp,
                            "emotion": str(x.get("emotion", "平静")),
                            "emotion_value": ev,
                            "source": "compress",
                        })
                    return out
        except Exception as e:
            logger.log_exception("压缩结果 JSON 解析失败,返回空列表", error=str(e))
        return []


# ---------- ProfileUpdater ----------

PROFILE_SYSTEM = ("你是用户画像维护器。画像只记录用户长期稳定、不会轻易变化的"
                  "个人信息,近期事件一律不要写入。")
PROFILE_KEYS = ["称呼", "关系", "性别", "生日", "爱好", "性格", "家庭环境", "居住环境", "长期事实"]
PROFILE_LIST_KEYS = ("爱好", "性格", "长期事实")


class ProfileUpdater:
    """画像增量更新:LLM 对比新记忆与旧画像,只补充/修正长期不变的属性。

    近期事件、临时计划、当前在做的事属于对话记忆(L2),不写入画像。
    """

    def __init__(self, complete_fn, load_fn, save_fn):
        self.complete = complete_fn
        self.load = load_fn
        self.save = save_fn

    @staticmethod
    def _norm(profile):
        return {k: (profile.get(k, "") if k not in PROFILE_LIST_KEYS
                    else list(profile.get(k) or []))
                for k in PROFILE_KEYS}

    def update(self, items):
        profile = self.load()
        schema = json.dumps({k: ([] if k in PROFILE_LIST_KEYS else "")
                             for k in PROFILE_KEYS}, ensure_ascii=False)
        prompt = (
            "当前画像(JSON):\n"
            f"{json.dumps(self._norm(profile), ensure_ascii=False)}\n\n"
            "用户最近发送的消息:\n" + "\n".join(f"- {i}" for i in items) + "\n\n"
            "请结合这些消息增量更新画像(与现有画像融合)。要求:\n"
            "- 画像只记录「长期稳定、不会轻易变化」的用户个人信息:\n"
            "  称呼/关系/性别/生日、爱好、性格、家庭环境、居住环境,以及其他长期事实(如职业、学历)\n"
            "- 近期事件、临时计划、当前正在做的事、一次性活动一律不要写入画像(那些属于对话记忆)\n"
            "- 只补充新信息、修正过时信息;不要删除与新增无关的内容\n"
            "- 称呼/关系/性别/生日保持不变(除非记忆里明确提到)\n"
            f"- 以 JSON 返回,结构为: {schema}\n"
            "- 若新增记忆没有任何长期稳定的新信息,原样返回当前画像\n"
            "只返回 JSON,不要解释。"
        )
        raw = (self.complete([
            {"role": "system", "content": PROFILE_SYSTEM},
            {"role": "user", "content": prompt},
        ]) or "").strip()
        if not raw:
            # 推理模型偶发空响应:抛异常让上层保留消息缓冲重试,避免静默清空
            raise RuntimeError("画像更新模型返回空响应")
        new = self._parse(raw)
        if new is None:
            return False
        if self._norm(new) != self._norm(profile):
            new["updated_at"] = file_util.format_time()
            self.save(new)
            return True
        return False

    @staticmethod
    def _parse(raw):
        try:
            s = raw.strip()
            start = s.find("{")
            end = s.rfind("}")
            if start != -1 and end > start:
                data = json.loads(s[start:end + 1])
                if isinstance(data, dict):
                    return {k: (data.get(k, "") if k not in PROFILE_LIST_KEYS
                                else list(data.get(k) or []))
                            for k in PROFILE_KEYS}
        except Exception as e:
            logger.log_exception("画像 JSON 解析失败", error=str(e))
        return None


# ---------- MemoryManager ----------

class MemoryManager:
    """L1 落盘 + 后台压缩编排 + 用户消息缓冲/画像提取编排。

    - add_turn 写入带时间的 L1 并立即落盘;用户消息(非空)追加进 UserMessageBuffer。
    - 未总结轮数 >= summary_rounds 且后台空闲时,快照压缩成 L2。
    - 用户消息缓冲满 profile_message_batch(默认 20)条时,后台提取画像并与旧画像融合。
    - 失败不标记(P1),重启后持续补压直到清空(P6);画像提取失败保留缓冲,自动重试。
    """

    def __init__(self, store, embedder, compressor, l1_store, enabled=True,
                 summary_rounds=10, context_turns=10, top_k=5,
                 profile_updater=None, profile_loader=None,
                 msg_buffer=None, dedup_threshold=0.85, sim_weight=0.7,
                 min_similarity=0.45):
        self.store = store
        self.embedder = embedder
        self.compressor = compressor
        self.l1 = l1_store
        self.enabled = enabled
        self.summary_rounds = summary_rounds
        self.context_turns = context_turns
        self.top_k = top_k
        self.profile_updater = profile_updater
        self.profile_loader = profile_loader
        self.msg_buffer = msg_buffer
        self.dedup_threshold = dedup_threshold
        self.sim_weight = sim_weight
        self.min_similarity = float(min_similarity)
        self.compacting = False
        self._profiling = False
        self._lock = threading.Lock()
        self._queue = queue.Queue()
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()
        self._l1_mtime = self._fstat(self.l1.path)
        self._resume()

    @staticmethod
    def _fstat(path):
        try:
            return path.stat().st_mtime
        except OSError:
            return -1.0

    def sync_from_disk(self):
        """L1 文件被其他进程(网页)改过时按 mtime 重读内存,保证删除/清空即时生效。

        L2(ChromaDB)每次查询都直读数据库,天然跨进程同步,无需处理。
        """
        m1 = self._fstat(self.l1.path)
        if m1 != self._l1_mtime:
            self.l1.reload()
            self._l1_mtime = self._fstat(self.l1.path)

    def _resume(self):
        """重启恢复:未总结轮数达标补触发总结;消息缓冲满批补触发画像提取(循环直至清空)。"""
        if not self.enabled:
            return
        self._maybe_trigger()
        self._maybe_profile()

    # ---- 后台任务 ----

    def _do_compress(self, batch):
        """压缩一批 L1 轮次到 L2。任何异常向上抛,由 _loop 决定不标记(P1)。"""
        if self.enabled and self.compressor is not None:
            profile = self.profile_loader() if self.profile_loader else None
            items = self.compressor.compress(
                batch, profile=profile, existing_items=self.store.items())
            if items and self.embedder is not None:
                vecs = self.embedder.embed_batch([t["text"] for t in items])
                self.store.add_items(items, vecs,
                                     dedup_threshold=self.dedup_threshold)
        return True

    def _do_profile(self, texts):
        """用攒满的用户消息更新画像(与旧画像融合)。异常向上抛,失败保留缓冲。"""
        if self.enabled and self.profile_updater is not None:
            self.profile_updater.update(list(texts))
        return True

    def _loop(self):
        """后台线程:队列里两种任务——("compress", 轮次列表)/("profile", 消息文本列表)。"""
        while True:
            job = self._queue.get()
            kind, payload = None, None
            ok = False
            try:
                kind, payload = job
                if kind == "compress":
                    ok = self._do_compress(payload)
                elif kind == "profile":
                    ok = self._do_profile(payload)
            except Exception as e:
                logger.log_exception("后台记忆任务失败", error=str(e),
                                     kind=str(kind))
                time.sleep(2.0)   # 失败稍息再重试,避免对故障 API 热循环
            finally:
                if kind == "compress" and ok and payload is not None:
                    # P1 修复:只有全部成功才标记;失败保持未总结,下次重试
                    self.l1.mark_summarized(len(payload))
                    self._l1_mtime = self._fstat(self.l1.path)
                if kind == "profile" and self.msg_buffer is not None \
                        and payload is not None:
                    self.msg_buffer.release(ok)
                with self._lock:
                    if kind == "compress":
                        self.compacting = False
                    if kind == "profile":
                        self._profiling = False
                self._queue.task_done()
                # P6:处理完若还有剩余未总结/未提取,继续补压/补提
                self._maybe_trigger()
                self._maybe_profile()

    def _maybe_trigger(self):
        """未总结轮数达标且后台空闲时,快照一批入队(一次只放一批,由 _loop 末尾续触发)。"""
        while True:
            with self._lock:
                if self.compacting:
                    return
                if self.l1.unsummarized_count() < self.summary_rounds:
                    return
                batch = self.l1.unsummarized()[:self.summary_rounds]
                self.compacting = True
            self._queue.put(("compress", list(batch)))
            return

    def _maybe_profile(self):
        """用户消息攒满 batch 条且后台空闲时,认领一批交给画像更新(失败自动重试)。"""
        if self.profile_updater is None or self.msg_buffer is None or not self.enabled:
            return
        with self._lock:
            if self._profiling:
                return
            self._profiling = True
        texts = self.msg_buffer.claim()
        if not texts:
            with self._lock:
                self._profiling = False
            return
        self._queue.put(("profile", texts))

    def add_turn(self, user_text, assistant_text, now_str):
        self.l1.add(user_text, assistant_text, now_str)
        self._l1_mtime = self._fstat(self.l1.path)
        if not self.enabled:
            return
        if user_text and self.msg_buffer is not None:
            self.msg_buffer.add(user_text, now_str)
        self._maybe_trigger()
        self._maybe_profile()

    def retrieve(self, query_text, k=None):
        if not self.enabled or self.embedder is None:
            return []
        try:
            qv = self.embedder.embed(query_text)
            if k is None:
                # 更新计划第1条:检索条数 = ⌊ln(总记忆数)⌋,下限 1,上限 10
                k = retrieval_k(self.store.count())
            return self.store.search(qv, k or self.top_k, sim_weight=self.sim_weight,
                                     min_similarity=self.min_similarity)
        except Exception as e:
            logger.log_exception("记忆检索失败", error=str(e))
            return []

    def context_messages(self, n=None):
        """取最近 n 轮 L1,转成 [{"role","content"}],带时间前缀(主动消息轮无用户行)。"""
        turns = self.l1.recent(n or self.context_turns)
        msgs = []
        for t in turns:
            tstr = t.get("time", "")
            prefix = f"[{tstr}] " if tstr else ""
            if t.get("user"):
                msgs.append({"role": "user", "content": f"{prefix}用户: {t['user']}"})
            if t.get("assistant"):
                msgs.append({"role": "assistant", "content": f"{prefix}AI: {t['assistant']}"})
        return msgs

    def pick_proactive_topic(self, mark=True):
        """主动发消息用:从高重要度记忆里取一条最近未用过的(防重复)。

        mark=False(网页预览)只挑不记,不推进轮换序号。
        """
        if not self.enabled or self.store is None:
            return None
        try:
            return self.store.pick_proactive_topic(mark=mark)
        except Exception as e:
            logger.log_exception("主动话题选择失败", error=str(e))
            return None

    def clear(self):
        self.l1.clear()
        self.store.clear()
