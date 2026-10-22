"""
数据脱敏：诊断记录、维修案例入库前，以及发给大模型之前，遮蔽能定位到人或车的信息。

    手机号     13812345678         → 138****5678
    车牌       粤B12345 / 粤BD12345 → 粤B***45 / 粤B****45
    车架号VIN  LSVAU2180N2183294   → LSV**********3294
    身份证号   110101199003071234  → 110101********1234

故障码（P0AA6、U0111 这类 5 位码）和数据流读数不受影响。
原文只在本次请求的内存里用来诊断，不落盘。
"""
import re

_PROVINCES = "京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼"
_VIN_CHARS = "A-HJ-NPR-Za-hj-npr-z0-9"  # VIN 不用 I、O、Q

ID_RE = re.compile(r"(?<!\d)(\d{6})\d{8}(\d{3}[\dXx])(?![\dXx])")
VIN_RE = re.compile(rf"(?<![A-Za-z0-9])([{_VIN_CHARS}]{{3}})([{_VIN_CHARS}]{{10}})([{_VIN_CHARS}]{{4}})(?![A-Za-z0-9])")
PLATE_RE = re.compile(rf"([{_PROVINCES}])\s*([A-Za-z])[·\s]?([A-Za-z0-9]{{5,6}})(?![A-Za-z0-9])")
PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")


def _vin(m):
    s = m.group(0)
    # 17 位里既有字母又有数字才当成 VIN，避免误伤普通英文单词
    if not (re.search(r"\d", s) and re.search(r"[A-Za-z]", s)):
        return s
    return m.group(1) + "*" * 10 + m.group(3)


def _plate(m):
    tail = m.group(3)
    return m.group(1) + m.group(2).upper() + "*" * (len(tail) - 2) + tail[-2:]


def mask(text):
    """返回脱敏后的文字；None 和空串原样返回。"""
    if not text:
        return text
    text = ID_RE.sub(lambda m: m.group(1) + "*" * 8 + m.group(2), text)
    text = VIN_RE.sub(_vin, text)
    text = PLATE_RE.sub(_plate, text)
    text = PHONE_RE.sub(lambda m: m.group(1) + "****" + m.group(2), text)
    return text
