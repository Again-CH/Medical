"""本地轻量 RAG 知识库（医疗诊疗 Agent 的「临床知识 + 科室画像」检索层）。

设计定位（对照链路图 ③ 合规横切层「长期记忆 + 配置 / kb.py RAG 知识库(SNOMED/Milvus 替换点)」）：
- 当前用**本地内嵌语料 + 关键词/字符重叠打分**实现「真检索」——不再是占位字符串，
  能对症状/主诉返回相关科室画像与临床指引，使分诊、诊前问诊两个子 Agent 的 RAG 工具
  真正可用、可演示。
- **替换点**：生产环境把 `retrieve()` 内部换成 SNOMED CT 编码匹配 / Milvus 向量检索即可，
  工具层（dept_map_rag / clinical_kb）与编排层一行不用改。

语料结构：每条 = {id, type, title, tags, text}
- type="dept"     → 科室画像（用于 dept_map_rag 症状→科室映射）
- type="guideline" → 临床指引（用于 clinical_kb 报告/主诉解读）
"""

from __future__ import annotations

from .embeddings import embed  # 零依赖向量化（生产可切换真实模型）

# ---------------------------------------------------------------------------
# 内嵌语料（演示级真实内容；生产替换为 HIS/知识库同步）
# ---------------------------------------------------------------------------
CORPUS: list[dict] = [
    # ---- 科室画像（dept_map RAG） ----
    {
        "id": "dept-neuro",
        "type": "dept",
        "title": "神经内科",
        "tags": ["头痛", "头晕", "眩晕", "脑血管病", "卒中", "癫痫", "帕金森", "麻木", "失眠"],
        "text": "诊治头痛、头晕、眩晕、肢体麻木、抽搐及脑血管病（卒中筛查与二级预防）。"
        "突发剧烈头痛或伴口角歪斜、言语不清、单侧无力属卒中预警，需立即急诊。",
    },
    {
        "id": "dept-resp",
        "type": "dept",
        "title": "呼吸内科",
        "tags": ["咳嗽", "咳痰", "哮喘", "肺部感染", "肺炎", "慢阻肺", "胸腔积液", "气促", "喘"],
        "text": "诊治咳嗽、咳痰、喘息、气促、肺部感染与慢阻肺。急性咳嗽伴发热多考虑呼吸道感染；"
        "慢性咳嗽（>8 周）需排查哮喘与胸片。",
    },
    {
        "id": "dept-gastro",
        "type": "dept",
        "title": "消化内科",
        "tags": ["腹痛", "腹泻", "胃肠", "胃炎", "肠炎", "肝胆", "胰腺", "反酸", "便血", "腹胀"],
        "text": "诊治腹痛、腹泻、反酸、腹胀及肝胆胰疾病。上腹痛多与胃相关，右下腹痛需警惕阑尾；"
        "伴呕血黑便/剧烈持续腹痛属急腹症，及时就诊。",
    },
    {
        "id": "dept-infect",
        "type": "dept",
        "title": "感染科",
        "tags": ["发热", "感染", "传染病", "肝炎", "流感", "新冠", "脓肿", "淋巴结肿大"],
        "text": "诊治发热待查、呼吸道感染（流感/新冠）、病毒性肝炎与其他传染病。 "
        "持续高热伴寒战、意识改变需尽快评估感染源。",
    },
    {
        "id": "dept-derm",
        "type": "dept",
        "title": "皮肤科",
        "tags": ["皮疹", "皮炎", "过敏", "痤疮", "湿疹", "荨麻疹", "瘙痒", "带状疱疹"],
        "text": "诊治皮疹、瘙痒、湿疹、荨麻疹、痤疮与带状疱疹。急性风团伴呼吸困难需警惕过敏性休克；"
        "水疱沿神经分布多为带状疱疹。",
    },
    {
        "id": "dept-cardio",
        "type": "dept",
        "title": "心血管内科",
        "tags": ["胸闷", "心悸", "高血压", "冠心病", "胸痛", "心律失常", "水肿", "气短"],
        "text": "诊治胸闷、胸痛、心悸、高血压与冠心病。胸闷伴大汗、左肩放射痛、濒死感应警惕急性心梗，"
        "立即呼叫急救；长期高血压需规律监测与用药。",
    },
    # ---- 临床指引（clinical_kb RAG） ----
    {
        "id": "gl-headache",
        "type": "guideline",
        "title": "头痛问诊要点",
        "tags": ["头痛", "头晕", "偏头痛"],
        "text": "区分偏头痛（搏动性、畏光、一侧）与紧张性头痛（压迫感、双侧）。"
        "危险信号：突发炸裂样头痛、伴神经定位体征（口角歪斜/肢体无力/言语不清）提示卒中，须急诊。",
    },
    {
        "id": "gl-cough",
        "type": "guideline",
        "title": "咳嗽鉴别",
        "tags": ["咳嗽", "咳痰", "肺炎", "哮喘"],
        "text": "急性咳嗽（<3 周）多因感染；伴黄脓痰、发热提示细菌性支气管炎/肺炎，可查胸片。"
        "迁延咳嗽伴喘息考虑咳嗽变异性哮喘，需肺功能检查。",
    },
    {
        "id": "gl-abdomen",
        "type": "guideline",
        "title": "腹痛评估",
        "tags": ["腹痛", "腹泻", "阑尾炎", "胆囊"],
        "text": "先定位：上腹痛多为胃十二指肠，右下腹固定压痛警惕阑尾炎，右上腹伴肩背放射痛警惕胆囊。"
        "剧烈持续腹痛、板状腹、伴休克征象为急腹症，需紧急处理。",
    },
    {
        "id": "gl-fever",
        "type": "guideline",
        "title": "发热处理",
        "tags": ["发热", "感染", "流感"],
        "text": "体温 37.3–38 为低热、39 以上高热。物理降温+补液，持续高热或伴寒战/皮疹/意识改变及时就医。"
        "退热药按需使用，避免重叠过量；儿童发热注意热性惊厥。",
    },
    {
        "id": "gl-rash",
        "type": "guideline",
        "title": "皮疹判别",
        "tags": ["皮疹", "瘙痒", "过敏", "荨麻疹"],
        "text": "风团样、来去迅速多为荨麻疹（过敏）；斑丘疹伴瘙痒常见于湿疹/接触性皮炎。"
        "皮疹伴呼吸困难、喉头水肿为严重过敏，立即急救；水疱沿神经分布多为带状疱疹。",
    },
    {
        "id": "gl-chest",
        "type": "guideline",
        "title": "胸闷鉴别",
        "tags": ["胸闷", "胸痛", "心悸", "高血压"],
        "text": "心源性（活动后加重、放射至左臂/下颌）与肺源性（伴咳嗽气喘）需鉴别。"
        "胸闷伴大汗、濒死感、持续不缓解警惕急性冠脉综合征，立即呼叫 120；青年焦虑相关多呈针刺样、与情绪相关。",
    },
    {
        "id": "gl-htn",
        "type": "guideline",
        "title": "高血压管理",
        "tags": ["高血压", "心悸", "水肿"],
        "text": "非同日三次 ≥140/90 可诊断。生活方式（减盐、运动、控重）为基础，"
        "必要时联合降压药；家庭自测血压、定期复诊，警惕靶器官损害（心/肾/眼底）。",
    },
]

# ---------------------------------------------------------------------------
# 院内资料（hospital_rag RAG）：面向「医院事务」的权威公开资料，区别于症状医学科普
# - type="hospital" → 就诊流程 / 门诊时间 / 医保报销 / 检查须知 / 体检 / 院区导航 /
#   便民服务 / 互联网医院复诊 / 急诊·住院须知 / 科室介绍
# - 落库 knowledge_documents(doc_type='hospital')，由 pgvector 做院内资料检索；
#   生产环境可由 HIS / 官网 / 企微知识库同步扩充，亦可经 POST /api/knowledge 增量写入。
# ---------------------------------------------------------------------------
HOSPITAL_CORPUS: list[dict] = [
    {
        "id": "hosp-visit-flow",
        "type": "hospital",
        "title": "门诊就诊流程与挂号须知",
        "tags": ["就诊", "挂号", "流程", "门诊", "预约", "初诊", "建档", "候诊", "签到"],
        "text": "康宁医院门诊就诊流程：① 首次就诊请持身份证在门诊大厅自助机或人工窗口建档领卡；"
        "② 可通过本院 APP / 微信小程序 / 官网提前 1-7 天预约挂号，也可现场挂号；"
        "③ 签到后到对应科室候诊区等候叫号；④ 就诊后如需检查 / 取药，凭医生开具的电子单据在缴费处或自助机结算，"
        "再到相应科室执行。温馨提示：复诊请携带既往病历与检查报告。",
    },
    {
        "id": "hosp-outpatient-hours",
        "type": "hospital",
        "title": "门诊开诊时间与急诊安排",
        "tags": ["门诊时间", "营业时间", "几点", "开诊", "上班", "急诊时间", "挂号截止"],
        "text": "门诊开诊时间：周一至周五 08:00-12:00、13:30-17:00；周六 08:00-12:00（部分科室）；"
        "周日及法定节假日仅开放急诊与部分便民门诊。急诊 24 小时开放。挂号截止时间：上午号 11:30，下午号 16:30。",
    },
    {
        "id": "hosp-medicare",
        "type": "hospital",
        "title": "医保报销与结算政策",
        "tags": ["医保", "报销", "结算", "社保", "新农合", "异地", "统筹", "垫付", "备案"],
        "text": "本院为医保定点医院。本地参保人持社保卡 / 医保电子凭证直接结算，个人仅付自付部分。"
        "异地医保请提前在参保地办理异地就医备案，备案后可直接结算；未备案者需先全额垫付再回参保地手工报销。"
        "门诊统筹起付线与报销比例按本市政策执行。",
    },
    {
        "id": "hosp-lab-prep",
        "type": "hospital",
        "title": "抽血化验注意事项",
        "tags": ["抽血", "化验", "空腹", "检验", "血检", "肝功能", "血糖", "糖耐", "采血"],
        "text": "抽血化验注意事项：① 多数生化项目（肝功能、血脂、血糖、肾功能）需空腹 8-12 小时，可少量饮水，"
        "建议安排在上午 07:30-10:30 采血；② 糖耐量试验需空腹并遵医嘱服药；③ 采血后按压穿刺点 3-5 分钟，"
        "凝血功能差者延长；④ 服用抗凝药、激素者请提前告知医生，以免影响结果判读。",
    },
    {
        "id": "hosp-imaging",
        "type": "hospital",
        "title": "影像检查（CT / 核磁 / 超声 / B 超）须知",
        "tags": ["CT", "核磁", "MRI", "超声", "彩超", "B超", "放射", "造影", "检查", "金属"],
        "text": "影像检查须知：① CT / DR 检查前请去除金属物品；增强 CT 需评估肾功能并签署知情同意；"
        "② 腹部超声 / B超需空腹（胆囊）或憋尿（盆腔），请遵医嘱准备；③ 核磁共振 (MRI) 严禁携带金属 / 心脏起搏器者进入；"
        "④ 检查结果通常 30 分钟-2 小时出具，可在自助机或 APP 查询。",
    },
    {
        "id": "hosp-health-checkup",
        "type": "hospital",
        "title": "体检中心与体检须知",
        "tags": ["体检", "健康体检", "入职体检", "体检套餐", "体检报告", "体检预约"],
        "text": "体检中心位于门诊楼 3 层。提供个人 / 入职 / 婚育 / 老年等体检套餐，需提前 1-3 天预约。"
        "体检前 3 天清淡饮食、禁酒，当日空腹；女性避开生理期。体检报告一般 3-5 个工作日出具，支持线上查看与解读门诊。",
    },
    {
        "id": "hosp-navigation",
        "type": "hospital",
        "title": "院区导航、交通与停车",
        "tags": [
            "地址",
            "位置",
            "怎么去",
            "交通",
            "公交",
            "地铁",
            "停车",
            "导航",
            "院区",
            "几号楼",
        ],
        "text": "院区地址：康宁市康宁大道 88 号。地铁 2 号线康宁医院站 B 口步行 3 分钟；多条公交线路直达。"
        "自驾请走南门地下停车场，前 2 小时免费，车位紧张建议公共交通。门诊楼、急诊楼、住院部以连廊相通，"
        "首层设导诊台与楼层索引。",
    },
    {
        "id": "hosp-convenience",
        "type": "hospital",
        "title": "便民服务设施",
        "tags": ["便民", "轮椅", "寄存", "租借", "WiFi", "饮水", "母婴室", "失物", "导诊", "打印"],
        "text": "便民服务：门诊大厅设免费轮椅 / 平车租借（凭身份证押金）、行李寄存柜、直饮水机、母婴室、无障碍卫生间；"
        "全院覆盖康宁医院免费 WiFi（连接后微信登录）。导诊台提供就医咨询与陪诊预约；报告可在一楼自助机打印。",
    },
    {
        "id": "hosp-online",
        "type": "hospital",
        "title": "互联网医院与线上复诊",
        "tags": ["互联网医院", "线上", "复诊", "在线问诊", "开药", "续方", "远程", "配送"],
        "text": "互联网医院：常见病 / 慢性病复诊患者可通过本院 APP 或小程序进行在线问诊、续方开药与药品配送到家，"
        "支持医保在线结算（部分地区）。初诊、急症、需体格检查者请到院就诊。线上问诊时间为 08:00-20:00。",
    },
    {
        "id": "hosp-emergency-flow",
        "type": "hospital",
        "title": "急诊流程与绿色通道",
        "tags": ["急诊", "急救", "120", "绿色通道", "急症", "抢救", "预检"],
        "text": "急诊流程：病情危急请立即拨打 120 或由家属护送至急诊科（24 小时）。本院设急诊绿色通道，"
        "对胸痛、卒中、创伤、高危孕产妇等优先抢救。到院后预检分诊，危重者直接进入抢救室，轻症按号候诊。"
        "请携带身份证、医保卡及既往病历。",
    },
    {
        "id": "hosp-inpatient",
        "type": "hospital",
        "title": "住院须知与探视管理",
        "tags": ["住院", "病房", "陪护", "探视", "医嘱", "手术", "床位", "押金"],
        "text": "住院须知：凭医生开具的住院证与身份证 / 医保卡到住院处办理入院，需缴纳押金。病房实行探视时间管理"
        "（通常为 15:00-19:00），每位患者限 1 名固定陪护；请遵守医嘱、配合护理，贵重物品自行保管，"
        "严禁在病区使用大功率电器。",
    },
    {
        "id": "hosp-dept-intro",
        "type": "hospital",
        "title": "重点科室与特色介绍",
        "tags": ["科室介绍", "特色", "专家", "重点科室", "专科", "擅长", "门诊出诊"],
        "text": "重点科室：神经内科（卒中中心、头痛眩晕专病）、心血管内科（胸痛中心、冠脉介入）、"
        "消化内科（胃肠镜早筛）、呼吸内科（慢阻肺与哮喘管理）、骨科（关节微创）、妇产科、儿科。"
        "各科室专家出诊信息可在 APP 查询，复杂病例可多学科会诊 (MDT)。",
    },
]

_HOSPITAL_TAGS: set[str] = set()
for _d in HOSPITAL_CORPUS:
    _HOSPITAL_TAGS.update(_d["tags"])

_TAGS: set[str] = set()
for _d in CORPUS:
    _TAGS.update(_d["tags"])

_STOP = set("的了在是与和有及、，。；：？！()（）. ")


def _extract_tags(query: str) -> set[str]:
    """从 query 抽取命中的关键词标签（子串匹配，支持中文复合词）。"""
    q = query.lower()
    hits = {t for t in _TAGS if t and t.lower() in q}
    return hits


def _char_tokens(text: str) -> set[str]:
    """字符级 token（去停用字 + 去空白），用于兜底重叠打分。"""
    return {c for c in text if c not in _STOP}


def _score(doc: dict, qtags: set[str], qchars: set[str]) -> int:
    doc_tags = set(doc["tags"])
    tag_overlap = len(qtags & doc_tags)
    doc_chars = _char_tokens(doc["title"] + "".join(doc["tags"]))
    char_overlap = len(qchars & doc_chars)
    # 关键词命中权重远高于字符重叠，避免常用字干扰
    return tag_overlap * 100 + char_overlap


# ---------------------------------------------------------------------------
# 检索层：优先 pgvector（Postgres 启用时），否则回退内存关键词打分
# ---------------------------------------------------------------------------


# 启动时统一灌入的内置语料：企业科室画像/临床指引 + 院内资料
_SEED_CORPORA: list[tuple[str, list[dict]]] = [
    ("builtin_corpus", CORPUS),
    ("hospital_material", HOSPITAL_CORPUS),
]


def seed_knowledge() -> int:
    """把内置语料（科室画像 + 临床指引 + 院内资料）幂等灌入 knowledge_documents。

    仅 Postgres 启用时生效；返回实际写入/更新的条数，DB 未启用或失败返回 0。
    """
    from .db import KnowledgeDocument, get_session, is_db_enabled

    if not is_db_enabled():
        return 0
    try:
        with get_session() as s:
            n = 0
            for source, corpus in _SEED_CORPORA:
                for d in corpus:
                    vec = embed(f"{d['title']} {d['text']}")
                    existing = s.get(KnowledgeDocument, d["id"])
                    if existing is not None:
                        existing.doc_type = d["type"]
                        existing.title = d["title"]
                        existing.tags = ",".join(d.get("tags", []))
                        existing.content = d["text"]
                        existing.source = source
                        existing.embedding = vec
                    else:
                        s.add(
                            KnowledgeDocument(
                                doc_id=d["id"],
                                doc_type=d["type"],
                                title=d["title"],
                                tags=",".join(d.get("tags", [])),
                                content=d["text"],
                                source=source,
                                embedding=vec,
                            )
                        )
                    n += 1
            s.commit()
            return n
    except Exception as e:  # noqa: BLE001
        print(f"[kb] seed_knowledge failed: {e}")
        return 0


def add_knowledge(
    doc_id: str,
    doc_type: str,
    title: str,
    content: str,
    tags: list[str] | None = None,
    source: str = "enterprise",
) -> str:
    """保存/覆盖一条企业知识到向量库（幂等，按 doc_id 覆盖）。

    返回落库后的 doc_id；DB 未启用时抛 RuntimeError（企业知识必须持久化）。
    """
    from .db import KnowledgeDocument, get_session, is_db_enabled

    if not is_db_enabled():
        raise RuntimeError("DATABASE_URL 未设置，企业知识无法持久化（请启用 Postgres）")
    vec = embed(f"{title} {content}")
    with get_session() as s:
        existing = s.get(KnowledgeDocument, doc_id)
        if existing is not None:
            existing.doc_type = doc_type
            existing.title = title
            existing.tags = ",".join(tags or [])
            existing.content = content
            existing.source = source
            existing.embedding = vec
        else:
            s.add(
                KnowledgeDocument(
                    doc_id=doc_id,
                    doc_type=doc_type,
                    title=title,
                    tags=",".join(tags or []),
                    content=content,
                    source=source,
                    embedding=vec,
                )
            )
        s.commit()
    return doc_id


def _search_pgvector(query: str, doc_type: str | None, top_k: int) -> str | None:
    """pgvector 余弦近邻检索；无结果或异常返回 None（交由内存回退）。"""
    from .db import KnowledgeDocument, get_session, is_db_enabled

    if not is_db_enabled():
        return None
    try:
        q = embed(query)
        with get_session() as s:
            stmt = s.query(KnowledgeDocument).filter(KnowledgeDocument.embedding.isnot(None))
            if doc_type:
                stmt = stmt.filter(KnowledgeDocument.doc_type == doc_type)
            stmt = stmt.order_by(KnowledgeDocument.embedding.cosine_distance(q)).limit(top_k)
            rows = stmt.all()
            if not rows:
                return None
            return "\n".join(f"· {r.title}：{r.content}" for r in rows)
    except Exception as e:  # noqa: BLE001
        print(f"[kb] pgvector search failed, fallback to memory: {e}")
        return None


def _search_memory(query: str, doc_type: str | None, top_k: int) -> str:
    """内存关键词/字符重叠打分（DB 不可用时的回退，行为与改造前一致）。"""
    qtags = _extract_tags(query)
    qchars = _char_tokens(query)
    scored = []
    for doc in CORPUS:
        if doc_type and doc["type"] != doc_type:
            continue
        s = _score(doc, qtags, qchars)
        if s > 0:
            scored.append((s, doc))
    scored.sort(key=lambda x: -x[0])
    top = scored[:top_k]
    if not top:
        return "[kb] 未命中知识库条目，建议人工分诊或挂号就诊"
    return "\n".join(f"· {d['title']}：{d['text']}" for _, d in top)


def search_knowledge(query: str, doc_type: str | None = None, top_k: int = 3) -> str:
    """统一检索入口：优先 pgvector，失败回退内存打分。"""
    pg = _search_pgvector(query, doc_type, top_k)
    if pg is not None:
        return pg
    return _search_memory(query, doc_type, top_k)


def retrieve(query: str, top_k: int = 3) -> str:
    """通用临床知识检索：返回最相关的 top_k 条片段（科室画像 + 临床指引混合）。

    现在优先走 pgvector 余弦检索（knowledge_documents 表），DB 不可用自动回退内存打分；
    工具层（dept_map_rag / clinical_kb）与编排层一行不用改。
    """
    return search_knowledge(query, doc_type=None, top_k=top_k)


def retrieve_department(symptom: str, top_k: int = 3) -> str:
    """症状→科室映射 RAG：联合检索 ``dept``（科室画像）与 ``dept_map``（症状-科室对应表，更全）两类语料，
    给 LLM 更完整的科室映射依据。

    - ``dept``：6 条科室画像（症状关键词 → 科室简介）；
    - ``dept_map``：18 条「症状-科室对应表」（含「科室匹配.md」总表与「科室匹配详情.md」逐科展开），
      覆盖发热/胃痛/心慌/头痛/关节疼/皮疹等更细的症状→科室映射。

    优先 pgvector 余弦检索，DB 不可用自动回退内存打分（dept_map 仅 DB 模式存在）。
    """
    dept = search_knowledge(symptom, doc_type="dept", top_k=max(1, top_k // 2))
    dept_map = search_knowledge(symptom, doc_type="dept_map", top_k=top_k)
    parts: list[str] = []
    for seg in (dept, dept_map):
        # 过滤掉各自的「未命中」占位，避免把兜底文案混进正常结果
        if seg and "[kb] 未命中" not in seg and "[院内资料] 未检索" not in seg:
            parts.append(seg)
    if not parts:
        return dept or dept_map or "[kb] 未命中知识库条目，建议人工分诊或挂号就诊"
    return "\n".join(parts)


def _search_hospital_memory(query: str, top_k: int = 3) -> str:
    """院内资料内存关键词/字符重叠打分（DB 不可用时的回退）。"""
    qtags = {t for t in _HOSPITAL_TAGS if t and t.lower() in query.lower()}
    qchars = _char_tokens(query)
    scored = []
    for doc in HOSPITAL_CORPUS:
        s = _score(doc, qtags, qchars)
        if s > 0:
            scored.append((s, doc))
    scored.sort(key=lambda x: -x[0])
    top = scored[:top_k]
    if not top:
        return "[院内资料] 未检索到相关院内资料，建议致电本院服务热线或前往导诊台咨询"
    return "[院内资料]\n" + "\n".join(f"· {d['title']}：{d['text']}" for _, d in top)


def retrieve_hospital(query: str, top_k: int = 3) -> str:
    """院内资料 RAG：返回与「医院事务」最相关的 top_k 条片段（就诊流程 / 医保 / 检查须知等）。

    优先走 pgvector 余弦检索（knowledge_documents 中 doc_type='hospital'），
    DB 不可用自动回退内存打分；工具层（hospital_rag）与编排层一行不用改。
    """
    pg = _search_pgvector(query, "hospital", top_k)
    if pg is not None:
        return "[院内资料]\n" + pg
    return _search_hospital_memory(query, top_k)
