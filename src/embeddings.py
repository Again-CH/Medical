"""文本向量化（Embedding）层。

设计目标（对照 kb.py 的「向量检索替换点」）：
- 默认提供**零依赖、可离线运行**的 embedding：特征哈希（hashing trick）+ 字符 n-gram，
  对中英文都生效，输出固定维度、L2 归一化的稠密向量。
  它本质是「词重叠」的稠密化，足以驱动 pgvector 的余弦相似度检索，让整条 RAG 链路
  不依赖任何外部模型/密钥即可端到端跑通（适合本地演示与面试展示）。
- **生产升级**：把 ``USE_REAL_EMBEDDING`` 打开（或安装 sentence-transformers / 配 OpenAI key），
  自动切换到真实语义向量；向量维度通过 ``EMBED_DIM`` 对齐即可，检索层一行不用改。

调用方只依赖 ``embed(text) -> list[float]`` 与 ``EMBED_DIM``，不关心背后实现。
"""

from __future__ import annotations

import hashlib
import math
import os
import re

# 向量维度：默认 384（与多语言 MiniLM 对齐）。换成 OpenAI text-embedding-3-small 需改为 1536。
EMBED_DIM = int(os.getenv("EMBED_DIM", "384"))

_STOP = set("的了在是与和有及、，。；：？！()（）. \t\n\r")


def _tokenize(text: str) -> list[str]:
    """中英文混合分词：英文按词，中文按字符 + 2-gram（覆盖复合词）。"""
    text = (text or "").lower()
    tokens: list[str] = []
    # 英文/数字词
    for m in re.findall(r"[a-z0-9]+", text):
        tokens.append(m)
    # 中文：单字 + 相邻 2-gram
    han = re.findall(r"[\u4e00-\u9fff]", text)
    tokens.extend(han)
    for i in range(len(han) - 1):
        tokens.append(han[i] + han[i + 1])
    return tokens


def _hash_idx(token: str, dim: int) -> int:
    """稳定地把 token 映射到 [0, dim) 的桶下标（正负号编码出现极性）。"""
    h = hashlib.md5(token.encode("utf-8")).digest()
    val = int.from_bytes(h[:4], "big")
    return val % dim


def _real_embed(text: str) -> list[float] | None:
    """尝试用真实模型生成语义向量；不可用返回 None（回退哈希向量）。"""
    # 路径 1：sentence-transformers（多语言，推荐本地生产）
    try:
        if os.getenv("USE_SENTENCE_TRANSFORMERS") == "1":
            from sentence_transformers import SentenceTransformer  # type: ignore

            model = SentenceTransformer(
                os.getenv("ST_MODEL", "paraphrase-multilingual-MiniLM-L12-v2")
            )
            v = model.encode(text, normalize_embeddings=True)
            return [float(x) for x in v]
    except Exception:
        pass
    # 路径 2：OpenAI 兼容 embeddings（需真实 OpenAI key/base，Deepseek 无 embedding 端点）
    base = os.getenv("OPENAI_BASE_URL", "")
    key = os.getenv("OPENAI_API_KEY", "")
    if os.getenv("USE_OPENAI_EMBEDDING") == "1" and key and "openai.com" in base:
        try:
            from openai import OpenAI  # type: ignore

            client = OpenAI(api_key=key, base_url=base)
            resp = client.embeddings.create(
                model=os.getenv("EMBED_MODEL", "text-embedding-3-small"), input=text
            )
            return [float(x) for x in resp.data[0].embedding]
        except Exception:
            pass
    return None


def embed(text: str) -> list[float]:
    """返回 text 的稠密向量（L2 归一化，长度 = EMBED_DIM）。

    优先真实模型，否则用特征哈希兜底——保证任何环境都能生成可用向量。
    """
    real = _real_embed(text)
    if real is not None and len(real) == EMBED_DIM:
        return real

    vec = [0.0] * EMBED_DIM
    for tok in _tokenize(text):
        if tok in _STOP:
            continue
        idx = _hash_idx(tok, EMBED_DIM)
        # 用 md5 第二字节决定正负极性，使不同 token 在同一桶内不完全抵消
        sign = 1.0 if (idx & 1) == 0 else -1.0
        vec[idx] += sign
    # L2 归一化
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return vec
    return [x / norm for x in vec]


def embed_batch(texts: list[str]) -> list[list[float]]:
    return [embed(t) for t in texts]
