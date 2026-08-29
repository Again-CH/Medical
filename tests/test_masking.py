"""脱敏工具单测：手机号/姓名/邮箱/身份证掩码，且保留医疗文本、幂等。"""

from src.masking import mask_name, mask_phone, mask_pii, mask_pii_text


def test_mask_phone_mobile():
    assert mask_phone("13812345678") == "138****5678"


def test_mask_phone_short_returns_mask():
    assert mask_phone("123") == "****"


def test_mask_phone_keeps_format_for_landline():
    # 固话位数足够时同样保留前后、打星中间
    assert mask_phone("01012345678") == "010****5678"


def test_mask_name():
    assert mask_name("张伟") == "张**"
    assert mask_name("欧阳娜娜") == "欧**"
    assert mask_name("王") == "**"


def test_mask_pii_text_masks_direct_identifiers():
    text = "手机13812345678，身份证11010119900307123X，邮箱a@b.com，请回电"
    out = mask_pii_text(text)
    assert "13812345678" not in out
    assert "11010119900307123X" not in out
    assert "a@b.com" not in out
    assert "请回电" in out  # 非标识符内容保留


def test_mask_pii_text_preserves_medical_content():
    # 关键：绝不能误伤症状/体征/科室等医疗文本
    text = "我胸痛，血压128/82，心率72，建议挂心血管内科"
    assert mask_pii_text(text) == text


def test_mask_pii_text_idempotent():
    text = "联系13812345678咨询"
    once = mask_pii_text(text)
    assert "13812345678" not in once
    assert mask_pii_text(once) == once  # 重复脱敏结果不变


def test_mask_pii_recursive():
    obj = {"a": "call 13812345678", "b": ["x 13900000000 y"]}
    out = mask_pii(obj)
    assert "13812345678" not in str(out)
    assert "13900000000" not in str(out)
