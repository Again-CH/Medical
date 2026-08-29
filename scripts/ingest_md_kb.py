"""将 Downloads 下的 6 份医疗 Markdown 知识文件切片后写入 Postgres 向量库（knowledge_documents）。

- 切片规则：去掉 H1 标题；按 `##` 大节切分，若大节内还有 `###` 子节（如疾病诊疗的疾病条目、
  科室匹配详情的科室条目），则进一步按 `###` 切成更细的检索单元；无子节时整节作为一条文档。
- 向量：复用 src/embeddings.embed（384 维，与 knowledge_documents.embedding 对齐）。
- 落库：复用 src/kb.add_knowledge（按 doc_id 幂等 upsert）。
- doc_type 规划：
   急诊分级.md      -> triage       （急诊四级分级）
   疾病诊疗.md      -> disease      （各疾病诊疗条目）
   检查指标.md      -> labref       （血常规 / 生化指标）
   科室匹配.md      -> dept_map     （症状→科室总表）
   科室匹配详情.md  -> dept_map     （各科室症状展开）
   症状鉴别.md      -> differential （胸痛/头痛/腹痛鉴别）

运行：在仓库根目录用 venv 执行，脚本会自动从 .env 注入 DATABASE_URL。
"""

from __future__ import annotations

import os
import re
import sys


# ---- 加载 .env（仅取 DATABASE_URL / EMBED_DIM，避免硬编码） ----
def _load_env(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)


_load_env(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))
# 兼容从仓库根目录运行
_load_env(os.path.join(os.getcwd(), ".env"))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.db import KnowledgeDocument, get_session, is_db_enabled  # noqa: E402
from src.kb import add_knowledge  # noqa: E402

DOWNLOADS = "/Users/mac/Downloads"

# (文件路径, doc_type, 主题词) — 主题词用于 doc_id 前缀与 tags
FILES = [
    (f"{DOWNLOADS}/急诊分级.md", "triage", "急诊分级"),
    (f"{DOWNLOADS}/疾病诊疗.md", "disease", "疾病诊疗"),
    (f"{DOWNLOADS}/检查指标.md", "labref", "检查指标"),
    (f"{DOWNLOADS}/科室匹配.md", "dept_map", "科室匹配"),
    (f"{DOWNLOADS}/科室匹配详情.md", "dept_map", "科室匹配"),
    (f"{DOWNLOADS}/症状鉴别.md", "differential", "症状鉴别"),
]


def _split_sections(body_lines):
    """按 `##` 大节切分，返回 [(h2_title_or_None, [lines])]。"""
    sections, cur_title, cur = [], None, []
    for ln in body_lines:
        if ln.startswith("## "):
            if cur_title is not None or cur:
                sections.append((cur_title, cur))
            cur_title = ln[3:].strip()
            cur = []
        else:
            cur.append(ln)
    if cur_title is not None or cur:
        sections.append((cur_title, cur))
    return sections


def _split_subsections(section_lines):
    """按 `###` 子节切分，返回 [(h3_title_or_None, [lines])]。"""
    subs, cur_title, cur = [], None, []
    for ln in section_lines:
        if ln.startswith("### "):
            if cur_title is not None or cur:
                subs.append((cur_title, cur))
            cur_title = ln[4:].strip()
            cur = []
        else:
            cur.append(ln)
    if cur_title is not None or cur:
        subs.append((cur_title, cur))
    return subs


def _safe(s: str) -> str:
    return re.sub(r"[\s/\\]+", "-", (s or "").strip())[:80]


def parse_file(path: str):
    """解析一个 md 文件，返回切片列表：[(title, category, content)]。"""
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    lines = raw.splitlines()

    h1 = None
    body = []
    for ln in lines:
        if ln.startswith("# ") and h1 is None:
            h1 = ln[2:].strip()
            continue
        body.append(ln)

    chunks = []
    sections = _split_sections(body)
    if not sections:
        # 完全没有 ##：整文件一条
        chunks.append((h1 or "文档", None, "\n".join(body).strip()))
        return chunks

    for h2, sec_lines in sections:
        subs = _split_subsections(sec_lines)
        has_sub = any(t is not None for t, _ in subs)
        if has_sub:
            for h3, sub_lines in subs:
                if h3 is None:
                    continue  # 大节内、首个 ### 之前的导语，跳过
                content = "\n".join(sub_lines).strip()
                if content:
                    chunks.append((h3, h2, content))
        else:
            content = "\n".join(sec_lines).strip()
            if content:
                chunks.append((h2 or h1 or "文档", None, content))
    return chunks


def main() -> int:
    if not is_db_enabled():
        print("[FAIL] DATABASE_URL 未设置，无法写入向量库（请启用 Postgres）")
        return 1

    total = 0
    for idx, (path, doc_type, topic) in enumerate(FILES, start=1):
        if not os.path.exists(path):
            print(f"[SKIP] 文件不存在：{path}")
            continue
        chunks = parse_file(path)
        print(f"\n=== {os.path.basename(path)}  ({doc_type})  切片数={len(chunks)} ===")
        for title, category, content in chunks:
            doc_id = f"md-{idx:02d}-{_safe(title)}"
            tags = [topic]
            if category:
                tags.append(category)
            tags.append(title)
            tags = list(dict.fromkeys([t for t in tags if t]))  # 去重
            add_knowledge(
                doc_id=doc_id,
                doc_type=doc_type,
                title=title,
                content=content,
                tags=tags,
                source=f"md_upload:{os.path.basename(path)}",
            )
            # 打印前 40 字预览
            preview = content.replace("\n", " ")[:42]
            print(f"  [{doc_id}] <{doc_type}> {title}  | {preview}")
            total += 1

    print(f"\n[OK] 本次共写入 {total} 条医疗知识切片到 knowledge_documents（向量库）。")

    # ---- 落库校验：统计总数 + 新增 doc_type 分布 ----
    with get_session() as s:
        all_rows = s.query(KnowledgeDocument).all()
        by_type = {}
        for r in all_rows:
            by_type[r.doc_type] = by_type.get(r.doc_type, 0) + 1
        print(f"[VERIFY] knowledge_documents 总条数 = {len(all_rows)}")
        print(f"[VERIFY] doc_type 分布 = {by_type}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
