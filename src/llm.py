import asyncio
import json
import re
from typing import Any, ClassVar, List, Optional
from urllib.parse import urlparse

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import ConfigDict

from .compose import compose_answer, parse_compose_context
from .config import (
    CHAT_TIMEOUT_SECONDS,
    LLM_EGRESS_POLICY,
    LLM_MODE,
    LLM_PRIVATE_HOSTS,
    OLLAMA_BASE_URL,
    OLLAMA_MODEL,
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    OPENAI_MODEL,
    QWEN_MODEL,
)
from .masking import mask_pii_text
from .retry import NonRetryableError, RetryPolicy, with_retry

# 需要前置工具结果才能正确填参的工具：延后到下一轮生成（如医保结算依赖锁号产出的预约号）
_DEFERRED_TOOLS = {"medicare_settle"}

# fake 模式下：若患者消息里点名了某个真实科室，则锁号/查号源就用该科室，
# 让「点选哪个科室就锁哪个科室」在离线演示里自洽（真实模型由 LLM 自行解析，不受影响）。
_FAKE_DEPARTMENTS = ["神经内科", "呼吸内科", "消化内科", "感染科", "皮肤科", "心血管内科"]

# fake 模式下：命中这些关键词即判定为「医院事务类问题」，触发 hospital_rag 内部资料 RAG，
# 而非症状医学科普（search_health_info）。用于确定性演示「相关问题才启用内部资料检索」。
_HOSPITAL_KEYWORDS = [
    "就诊",
    "挂号",
    "门诊",
    "预约挂号",
    "医保",
    "报销",
    "结算",
    "社保",
    "异地",
    "垫付",
    "缴费",
    "取药",
    "检查",
    "化验",
    "抽血",
    "空腹",
    "ct",
    "核磁",
    "mri",
    "超声",
    "b超",
    "影像",
    "体检",
    "健康体检",
    "入职体检",
    "地址",
    "位置",
    "怎么去",
    "交通",
    "地铁",
    "公交",
    "停车",
    "导航",
    "院区",
    "几号楼",
    "便民",
    "轮椅",
    "寄存",
    "wifi",
    "母婴",
    "导诊",
    "互联网医院",
    "线上",
    "复诊",
    "在线问诊",
    "续方",
    "急诊",
    "120",
    "抢救",
    "绿色通道",
    "住院",
    "病房",
    "陪护",
    "探视",
    "科室介绍",
    "重点科室",
    "专家",
    "几点",
    "开诊",
    "营业时间",
    "上班时间",
    "流程",
]


# fake 模式下：intake 命名空间里，仅当用户提及「即将做检查/化验」或询问检查前准备时才
# 追加 hospital_rag（区别于「解读已有报告」read_lab_report，避免误触发院内资料 RAG）。
_INTAKE_EXAM_KEYWORDS = [
    "抽血",
    "空腹",
    "憋尿",
    "b超",
    "B超",
    "ct",
    "CT",
    "核磁",
    "mri",
    "MRI",
    "超声",
    "胃镜",
    "肠镜",
    "造影",
    "穿刺",
    "体检",
    "影像",
    "检查前",
    "化验前",
    "做检查",
    "做化验",
    "检查注意",
    "化验注意",
    "检查准备",
    "化验准备",
    "检查须知",
    "化验须知",
]


# ── intake 自动入库（演示/测试模式也能真实落库）──────────────────────────────
# fake 模式下没有真实 LLM 去「理解后调用 record_*」；这里用确定性抽取把患者消息里
# 可结构化的检验数值 / 生命体征 / 病例信号，自动映射成 record_* 工具调用，
# 让「病例 + 体检报告自动写入数据库」在离线演示里真实发生（真实模型由 LLM 自行解析）。
# 关键词按「长→短」排序，配合区间去重，避免「空腹血糖」被同时当作「血糖」重复落库。
_LAB_PATTERNS = [
    ("空腹血糖", "空腹血糖", "record_lab_result", "mmol/L"),
    ("餐后血糖", "餐后血糖", "record_lab_result", "mmol/L"),
    ("糖化血红蛋白", "糖化血红蛋白", "record_lab_result", "%"),
    ("HbA1c", "糖化血红蛋白", "record_lab_result", "%"),
    ("高密度脂蛋白胆固醇", "高密度脂蛋白胆固醇", "record_lab_result", "mmol/L"),
    ("低密度脂蛋白胆固醇", "低密度脂蛋白胆固醇", "record_lab_result", "mmol/L"),
    ("高密度脂蛋白", "高密度脂蛋白胆固醇", "record_lab_result", "mmol/L"),
    ("低密度脂蛋白", "低密度脂蛋白胆固醇", "record_lab_result", "mmol/L"),
    ("总胆固醇", "总胆固醇", "record_lab_result", "mmol/L"),
    ("甘油三酯", "甘油三酯", "record_lab_result", "mmol/L"),
    ("谷丙转氨酶", "谷丙转氨酶", "record_lab_result", "U/L"),
    ("谷草转氨酶", "谷草转氨酶", "record_lab_result", "U/L"),
    ("ALT", "谷丙转氨酶", "record_lab_result", "U/L"),
    ("AST", "谷草转氨酶", "record_lab_result", "U/L"),
    ("尿素氮", "尿素氮", "record_lab_result", "mmol/L"),
    ("白细胞计数", "白细胞计数", "record_lab_result", "×10^9/L"),
    ("血红蛋白", "血红蛋白", "record_lab_result", "g/L"),
    ("红细胞计数", "红细胞计数", "record_lab_result", "×10^12/L"),
    ("血小板计数", "血小板计数", "record_lab_result", "×10^9/L"),
    ("白细胞", "白细胞计数", "record_lab_result", "×10^9/L"),
    ("红细胞", "红细胞计数", "record_lab_result", "×10^12/L"),
    ("血小板", "血小板计数", "record_lab_result", "×10^9/L"),
    ("肌酐", "肌酐", "record_lab_result", "μmol/L"),
    ("尿酸", "尿酸", "record_lab_result", "μmol/L"),
    ("血糖", "血糖", "record_lab_result", "mmol/L"),
    ("胆固醇", "总胆固醇", "record_lab_result", "mmol/L"),
]
_VITAL_PATTERNS = [
    ("血压", "血压", "record_vital", "mmHg"),
    ("心率", "心率", "record_vital", "次/分"),
    ("脉搏", "心率", "record_vital", "次/分"),
    ("体温", "体温", "record_vital", "℃"),
    ("血氧", "血氧", "record_vital", "%"),
    ("SpO2", "血氧", "record_vital", "%"),
]
# 触发「沉淀一条病例小结」的病史类信号（数值已抽取时也会沉淀，用于整份体检报告）
_CASE_KEYWORDS = [
    "病史",
    "既往",
    "过敏",
    "用药",
    "服药",
    "诊断",
    "主诉",
    "现病史",
    "查出",
    "确诊",
    "我在吃",
    "在服",
    "高血压",
    "糖尿病",
    "冠心病",
    "哮喘",
]


def _fake_extract_records(hint: str) -> list:
    """从 intake 用户消息抽取可结构化的检验/生命体征/病例，返回 record_* 工具调用列表。"""
    if not hint:
        return []
    recs: list = []
    consumed: list = []  # 已消费字符区间，避免基础词在复合词内重复命中

    def _overlap(s, e):
        return any(not (e <= ss or s >= ee) for ss, ee in consumed)

    for kw, item, tool, unit in _LAB_PATTERNS + _VITAL_PATTERNS:
        for m in re.finditer(re.escape(kw), hint):
            s, e = m.start(), m.end()
            if _overlap(s, e):
                continue
            after = hint[e : e + 24]
            vm = re.search(r"\d+(?:\.\d+)?(?:[/\d.]+)?", after)
            if not vm:
                continue
            val = vm.group(0)
            consumed.append((s, e + vm.end()))  # 连同数值一起标记为已消费
            if tool == "record_vital":
                recs.append({"name": tool, "args": {"type": item, "value": val, "unit": unit}})
            else:
                recs.append(
                    {
                        "name": tool,
                        "args": {
                            "item": item,
                            "result": val,
                            "ref_range": "",
                            "abnormal": False,
                            "report_date": "",
                        },
                    }
                )
    # 病例小结：命中病史信号，或已抽取到数值（即整份体检报告）→ 沉淀一条结构化病历
    if recs or any(k in hint for k in _CASE_KEYWORDS):
        cat = "general"
        if "过敏" in hint:
            cat = "过敏史"
        elif "用药" in hint or "服药" in hint or "在吃" in hint or "在服" in hint:
            cat = "用药史"
        elif (
            "既往" in hint
            or "病史" in hint
            or "高血压" in hint
            or "糖尿病" in hint
            or "冠心病" in hint
            or "哮喘" in hint
        ):
            cat = "既往史"
        recs.append({"name": "record_case_summary", "args": {"text": hint, "category": cat}})
    return recs


def _fake_department_from_hint(hint: str) -> str:
    if not hint:
        return "神经内科"
    for name in _FAKE_DEPARTMENTS:
        if name in hint:
            return name
    return "神经内科"


def _fake_item_name_from_hint(hint: str) -> str:
    """从「解读我的xxx报告」类消息中提取检验项目名，供 fake 模式精准调用 read_lab_report。"""
    if not hint:
        return ""
    # 优先匹配【项目名】、我的项目报告、解读项目报告 等常见句式
    for pat in [r"【(.+?)】", r"我的(.+?)检验报告", r"我的(.+?)报告", r"解读(.+?)报告"]:
        m = re.search(pat, hint)
        if m:
            return m.group(1).strip()
    return ""


def _chunk_text(text: str, size: int = 4):
    """把一段文本切成小块，模拟 LLM 的 token 级流式输出。"""
    out = []
    for i in range(0, len(text), size):
        out.append(text[i : i + size])
    return out


class FakeLLM(BaseChatModel):
    """确定性假模型：无需任何外部 API，既支持 token 级流式，也支持 bind_tools 函数调用。

    通过继承 BaseChatModel，FakeLLM 在 fake 模式下也能被 graph.astream_events
    以 on_chat_model_stream 事件捕获，从而让 SSE 真的流式推送 token，而非事后切分。

    函数调用：bind_tools 后，FakeLLM 会根据 intent_hint 在「该子 Agent 命名空间」内
    确定性地返回 tool_calls（首轮）；工具结果回到上下文后，再走 compose 文本汇总。
    这让 fake 模式也能演示「LLM 自主选工具 → ToolNode 执行」的真实 ReAct 范式。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    tools: List[Any] = []
    intent_hint: Optional[str] = None
    patient_id_hint: Optional[str] = None
    human_hint: Optional[str] = None

    # fake 模式下：各意图对应的「应调用的工具名」（即该命名空间全部工具）
    FAKE_INTENT_TOOLS: ClassVar[dict] = {
        "triage": ["search_health_info", "search_department", "dept_map_rag"],
        "booking": [
            "query_availability",
            "lock_appointment",
            "confirm_appointment",
            "medicare_settle",
        ],
        "intake": ["read_lab_report", "clinical_kb"],
        "followup": ["read_vitals", "plan_reminder", "memory_append"],
        "emergency": ["handoff", "call_120"],
    }

    # 各工具参数的默认取值（callable 可引用运行时 hint）
    PROP_DEFAULTS: ClassVar[dict] = {
        "symptom": (lambda self: self.human_hint or "头痛"),
        "query": (lambda self: self.human_hint or ""),
        "department": (lambda self: _fake_department_from_hint(self.human_hint)),
        "item_name": (lambda self: _fake_item_name_from_hint(self.human_hint)),
        "date": "today",
        "slot": "09:30",
        "appointment_id": "APT-1001",
        "text": "按时服药、监测血压",
        "note": (lambda self: self.human_hint or ""),
    }

    @property
    def _llm_type(self) -> str:
        return "fake-medical"

    def bind_tools(self, tools, **kwargs):
        new = FakeLLM()
        new.tools = list(tools)
        new.intent_hint = self.intent_hint
        new.patient_id_hint = self.patient_id_hint
        new.human_hint = self.human_hint
        return new

    # ---- 工具选择（fake 模式确定性决策） ----
    def _has_tool_result(self, messages) -> bool:
        return any(getattr(m, "type", None) == "tool" for m in messages)

    def _select_tool_names(self):
        names = self.FAKE_INTENT_TOOLS.get(
            self.intent_hint or "triage", [t.name for t in self.tools]
        )
        # 挂号意图：仅当用户提到「医保/结算」时才包含 medicare_settle（敏感操作）
        # 否则只生成 query_availability + lock_appointment（均非敏感，自动执行）
        if self.intent_hint == "booking" and "medicare_settle" in names:
            hint = (self.human_hint or "").lower()
            if "医保" not in hint and "结算" not in hint:
                names = [n for n in names if n != "medicare_settle"]
        # 挂号确认语：用户仅说「确认预约/确认挂号」时，只调用 confirm_appointment，
        # 避免重复锁号或产生新的预约。
        if self.intent_hint == "booking" and names:
            hint = (self.human_hint or "").strip()
            if any(h in hint for h in ["确认预约", "确认挂号", "确定预约"]):
                names = [n for n in names if n == "confirm_appointment"]
        # 分诊意图：用户输入过于模糊（问候/无具体症状）时不调工具，直接回复
        if self.intent_hint == "triage":
            hint = (self.human_hint or "").strip()
            # 医院事务类问题（就诊/挂号/医保/检查须知/导航/复诊等）→ 启用内部资料 RAG，
            # 不走症状医学科普 search_health_info，避免把「医院怎么运作」误答成「症状科普」。
            if any(k in hint.lower() for k in _HOSPITAL_KEYWORDS):
                return ["hospital_rag", "search_department"]
            _vague = [
                "你好",
                "您好",
                "hi",
                "hello",
                "嗨",
                "在吗",
                "身体不舒服",
                "不太舒服",
                "感觉不舒服",
                "咨询一下",
                "想问问",
                "请问",
                "帮忙看看",
                "谢谢",
                "感谢",
                "好的",
            ]
            _symptom_kw = [
                "痛",
                "疼",
                "痒",
                "肿",
                "烧",
                "咳",
                "泻",
                "吐",
                "晕",
                "溃疡",
                "疹",
                "血",
                "失眠",
                "便秘",
                "过敏",
                "感冒",
                "发烧",
                "头痛",
                "腹痛",
                "胸痛",
                "皮疹",
                "外伤",
            ]
            if len(hint) < 5 or any(v in hint for v in _vague):
                if not any(kw in hint for kw in _symptom_kw):
                    return []  # 不调任何工具
        # 诊前问诊意图：仅当用户提及「即将做检查/化验」或询问检查前准备时，才追加
        # hospital_rag 主动提示院内检查须知；普通报告解读仍走 read_lab_report / clinical_kb。
        if self.intent_hint == "intake":
            _hint = (self.human_hint or "").lower()
            if any(k in _hint for k in _INTAKE_EXAM_KEYWORDS) and "hospital_rag" not in names:
                names = names + ["hospital_rag"]
        return names

    @staticmethod
    def _appointment_id_from(messages):
        """从工具结果中提取真实 appointment_id（模拟 LLM 依据上下文填参）。

        真实模型会从 ``lock_appointment`` 的返回文本里解析出预约号；fake 模式用
        正则等价实现，避免沿用硬编码占位符 ``APT-1001``，导致后续医保结算步骤
        命中「预约不存在或不属于当前患者」——即复现真实模型的正确链路。
        """
        for m in messages or ():
            if getattr(m, "type", None) == "tool":
                mt = re.search(r"appointment_id=(APT-\d+)", getattr(m, "content", "") or "")
                if mt:
                    return mt.group(1)
        return None

    def _default_arg(self, name, messages=None):
        if name == "appointment_id":
            real = self._appointment_id_from(messages)
            if real:
                return real
        v = self.PROP_DEFAULTS.get(name, "")
        return v(self) if callable(v) else v

    def _build_tool_calls(self, messages, call_specs):
        """按 call_specs = [(tool_name, args_or_None), ...] 逐个产出工具调用。

        args 为 None 时用 PROP_DEFAULTS 填参；否则用给定的精确参数（自动入库场景）。
        同一工具可出现多次（如体检报告中多个项目各调一次 record_lab_result），
        tool_call_id 带递增下标保证唯一，兼容 LangGraph 对同一工具多次调用的重建。
        """
        calls = []
        seen = {}
        for name, args in call_specs:
            t = next((x for x in self.tools if x.name == name), None)
            if t is None:
                continue
            if args is None:
                schema = getattr(t, "args", {}) or {}
                props = schema.get("properties", schema) if isinstance(schema, dict) else {}
                args = {k: self._default_arg(k, messages) for k in props}
            idx = seen.get(name, 0)
            seen[name] = idx + 1
            calls.append(
                {"name": name, "args": args, "id": f"call_{name}_{idx}", "type": "tool_call"}
            )
        return calls

    @staticmethod
    def _already_submitted(messages, name) -> bool:
        """该工具本轮是否已提交（敏感工具会留占位 ToolMessage 等待审批）。

        tool_call_id 形如 ``call_{name}_{idx}``，用前缀匹配即可覆盖多次调用。
        """
        for m in messages or ():
            if getattr(m, "type", None) == "tool" and getattr(m, "tool_call_id", "").startswith(
                f"call_{name}"
            ):
                return True
        return False

    def _compose(self, messages) -> str:
        """文本汇总：支持两种调用场景。

        场景A（agent 子节点）：messages 含 ToolMessages（工具执行结果），
          intent/patient_id 来自运行时 hint（bind_tools 后由 agent 节点设置）。
        场景B（final_answer 汇总节点）：messages 只有一条结构化 HumanMessage
          （格式「意图:xxx\n工具结果:xxx\n红线:xxx\n患者:xxx」），无 ToolMessage。
        """
        # 优先检测场景B：最后一条 HumanMessage 是否为结构化上下文
        last_human_content = ""
        for m in messages:
            if isinstance(m, HumanMessage):
                last_human_content = m.content

        if "意图:" in last_human_content or "工具结果:" in last_human_content:
            # 场案B：final_answer 传来的结构化上下文，直接解析
            intent, tool_result, redline, pid = parse_compose_context(last_human_content)
            return compose_answer(intent, tool_result, redline, pid)

        # 场景A：从 ToolMessages 提取工具结果，从 hint 取意图/患者
        tool_parts = []
        for m in messages:
            if getattr(m, "type", None) == "tool" or isinstance(m, ToolMessage):
                content = getattr(m, "content", "") or ""
                if (
                    content
                    and content
                    != "[工具调用已记录，尚未执行，等待人工审批；请继续调用本意图所需的其它工具]"
                ):
                    tool_parts.append(content)
        tool_result = "\n".join(tool_parts)
        intent = self.intent_hint or "triage"
        pid = self.patient_id_hint or "unknown"
        return compose_answer(intent, tool_result, "", pid)

    def _maybe_tool_calls(self, messages):
        """按「多轮 ReAct」顺序产出工具调用，而非一次性全量生成。

        - 第 1 轮（上下文无工具结果）：先产出**无前置依赖**的工具（查号源 / 锁号 / 转诊）。
        - 第 2 轮起：才产出**依赖前置结果**的工具（如医保结算需要真实 appointment_id），
          此时 appointment_id 能从上一轮锁号结果中解析出真实值 —— 复现真实模型的填参链路。
        - 已提交等待审批的敏感工具不再重复生成，避免审批载荷里出现重复项。
        """
        if not self.tools:
            return None
        base = [n for n in self._select_tool_names() if not self._already_submitted(messages, n)]
        if not base:
            return None
        # 每个 (工具名, 参数) 作为一条调用规格；args 为 None 表示用默认填参。
        # 同一工具可多次出现（如体检报告多个项目各调一次 record_lab_result）。
        call_specs = [(n, None) for n in base]
        # 诊前问诊（intake）与分诊（triage）：确定性抽取用户消息里的检验数值/
        # 生命体征/病例信号，自动追加 record_* 工具调用，使「病例 + 体检报告自动入库」
        # 在演示模式真实落库（真实模型由 LLM 自行解析，不受此影响）。
        if self.intent_hint in ("intake", "triage"):
            for r in _fake_extract_records(self.human_hint or ""):
                call_specs.append((r["name"], r["args"]))
        deferred = [s for s in call_specs if s[0] in _DEFERRED_TOOLS]
        immediate = [s for s in call_specs if s[0] not in _DEFERRED_TOOLS]
        chosen = immediate if not self._has_tool_result(messages) else deferred
        if not chosen:
            return None
        return self._build_tool_calls(messages, chosen)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        calls = self._maybe_tool_calls(messages)
        if calls:
            return ChatResult(
                generations=[ChatGeneration(message=AIMessage(content="", tool_calls=calls))]
            )
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=self._compose(messages)))]
        )

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        calls = self._maybe_tool_calls(messages)
        if calls:
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {"name": c["name"], "args": "", "id": c["id"], "index": i}
                        for i, c in enumerate(calls)
                    ],
                )
            )
            return
        for piece in _chunk_text(self._compose(messages)):
            yield ChatGenerationChunk(message=AIMessageChunk(content=piece))

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        calls = self._maybe_tool_calls(messages)
        if calls:
            # 流式模式下 LangGraph 会用 tool_call_chunks 重建 tool_calls，
            # 因此 args 必须给出「完整 JSON 字符串」（单 chunk 即完整片段）
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": c["name"],
                            "args": json.dumps(c["args"], ensure_ascii=False),
                            "id": c["id"],
                            "index": i,
                        }
                        for i, c in enumerate(calls)
                    ],
                )
            )
            return
        for piece in _chunk_text(self._compose(messages)):
            yield ChatGenerationChunk(message=AIMessageChunk(content=piece))


def _is_private_endpoint(base: str) -> bool:
    """判断模型端点是否属于本地/私有化部署（数据不出域）。"""
    try:
        host = (urlparse(base).hostname or "").lower()
    except Exception:
        return False
    return host in LLM_PRIVATE_HOSTS


def _assert_egress_policy(base: str) -> None:
    """PHI 出境管控：strict 策略下禁止将患者数据发往外部模型端点。

    医疗数据合规红线：检验报告、生命体征、症状描述均属 PHI/敏感个人信息，
    未经脱敏与安全评估不得传输至第三方 API（个人信息保护法第 38-39 条）。
    - allow ：原样出境（仅限无真实患者数据的本地开发，生产禁止）。
    - masked：出境前做 PII/PHI 脱敏（见 acompose）。
    - strict：仅允许本地/私有化端点，外网端点直接拒绝启动。
    """
    if LLM_EGRESS_POLICY == "allow":
        return
    if _is_private_endpoint(base):
        return
    if LLM_EGRESS_POLICY == "strict":
        raise RuntimeError(
            "安全启动校验失败：LLM_EGRESS_POLICY=strict 下禁止将患者数据发往外部端点 "
            f"（当前 {base}）。请改用本地/私有化模型（LLM_MODE=ollama 或内网端点），"
            "或将 LLM_PRIVATE_HOSTS 显式纳入可信内网端点；"
            "确需出境时请设置 LLM_EGRESS_POLICY=masked 并完成出境安全评估与单独同意。"
        )
    # masked：放行，由 acompose 在出境前脱敏


def _ollama_reachable(base_url: str) -> None:
    """轻量探测 Ollama 是否可达（避免等到首个 token 才报错）。"""
    import urllib.request

    url = f"{base_url.rstrip('/')}/api/tags"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=2):
        pass


def _mask_messages(messages):
    """出境前脱敏：对发给模型的文本做 PII/PHI 掩码，保留临床语义。"""
    out = []
    for m in messages:
        content = getattr(m, "content", None)
        if isinstance(content, str) and content:
            try:
                m = m.model_copy(update={"content": mask_pii_text(content)})
            except Exception:  # 兼容不可变/自定义消息对象：退化不改
                pass
        out.append(m)
    return out


def get_llm():
    if LLM_MODE == "fake":
        return FakeLLM()
    if LLM_MODE == "ollama":
        try:
            from langchain_ollama import ChatOllama
        except ImportError:
            print("[warn] langchain-ollama 未安装，回退到 FakeLLM 演示模式")
            return FakeLLM()
        try:
            _ollama_reachable(OLLAMA_BASE_URL)
        except Exception as e:
            print(f"[warn] Ollama 服务不可达（{e}），回退到 FakeLLM 演示模式")
            return FakeLLM()
        return ChatOllama(base_url=OLLAMA_BASE_URL, model=OLLAMA_MODEL, streaming=True)
    if LLM_MODE in ("openai", "qwen"):
        from langchain_openai import ChatOpenAI

        base = OPENAI_BASE_URL or "https://api.openai.com/v1"
        model = QWEN_MODEL if LLM_MODE == "qwen" else OPENAI_MODEL
        # PHI 出境管控：外网端点在 strict 策略下直接拒绝启动
        _assert_egress_policy(base)
        # 熔断第一层：单次请求超时 + 失败自动重试（指数退避，最多 2 次）
        return ChatOpenAI(
            model=model,
            api_key=OPENAI_API_KEY,
            base_url=base,
            streaming=True,
            request_timeout=CHAT_TIMEOUT_SECONDS,
            max_retries=2,
        )
    raise ValueError(f"未知 LLM_MODE: {LLM_MODE}")


def _content_of(chunk) -> str:
    # 兼容 ChatGenerationChunk（.message.content）与裸 AIMessageChunk（.content）
    msg = getattr(chunk, "message", None)
    c = getattr(msg, "content", "") if msg is not None else getattr(chunk, "content", "")
    if isinstance(c, list):  # 部分模型返回 content part 列表
        return "".join(getattr(x, "text", str(x)) for x in c)
    return c or ""


async def acompose(llm, messages, timeout: Optional[int] = None) -> str:
    """异步流式汇总 LLM 输出为完整文本（节点内使用，同时向 astream_events 暴露 token）。

    外层已对 chat 端点做了 SSE 级超时；此处再对单次 LLM 聚合加一层超时兜底，
    避免 deepseek/ollama 异常挂起导致 acompose 永不返回（熔断）。超时/异常向上抛出，
    由调用方 final_answer 统一安全降级。
    """
    if timeout is None:
        timeout = CHAT_TIMEOUT_SECONDS

    # 出境脱敏：masked 策略下，患者文本在离开本机前先做 PII/PHI 掩码
    if LLM_EGRESS_POLICY == "masked":
        messages = _mask_messages(messages)

    async def _attempt():
        collected: list[str] = []
        async for chunk in llm.astream(messages):
            collected.append(_content_of(chunk))
        return "".join(collected)

    async def _factory():
        # 单次尝试带自身超时；重试在 with_retry 内逐次重跑（每次独立的 wait_for）
        return await asyncio.wait_for(_attempt(), timeout=timeout)

    # 抗造：对 LLM 调用的瞬时失败（超时/连接抖动）指数退避重试；
    # 鉴权/参数类错误标记为 NonRetryableError 立即终止（不浪费重试）。
    policy = RetryPolicy(
        max_attempts=3,
        base_delay=0.2,
        max_delay=5.0,
        retry_on=(asyncio.TimeoutError, ConnectionError, TimeoutError),
    )
    try:
        return await with_retry(policy, _factory)
    except (asyncio.TimeoutError, ConnectionError, TimeoutError):
        raise  # 重试仍失败 → 交由 final_answer 统一安全降级
    except NonRetryableError:
        raise
