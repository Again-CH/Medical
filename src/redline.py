EMERGENCY_KEYWORDS = ["胸痛", "呼吸困难", "大出血", "卒中", "昏迷", "休克", "心肺复苏", "心梗"]
VIOLATION_KEYWORDS = ["给我开药", "开药", "诊断我", "开处方"]


def check_redline(text: str):
    """红线引擎：急症 / 违规请求前置拦截，AI 不下诊断、不开药。"""
    for kw in EMERGENCY_KEYWORDS:
        if kw in text:
            return True, f"命中急症关键词：{kw}"
    for kw in VIOLATION_KEYWORDS:
        if kw in text:
            return True, f"违规请求：{kw}（AI 不执行诊断/开药）"
    return False, ""
