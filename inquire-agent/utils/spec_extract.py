"""
utils/spec_extract.py — 材料规格硬字段抽取（v5.2）
用途：从材料规格文本中抽取关键参数，喂给 LLM 提高估价精度
借鉴：material-price-audit/matching.py（取其正则抽取，弃其"否决制"）
"""

import re


def extract_spec_features(spec: str) -> dict:
    """
    从规格文本中抽取硬字段：
    - 电压: AC220V / DC24V
    - 功率: 100W / 50W/m
    - 防护等级: IP65
    - 尺寸: 1250X400 / φ12 / DN100
    - 其他数字参数: 色温4000K、4端口、8通道
    """
    s = (spec or "").strip()
    feats = {}

    # 电压
    m = re.search(r"(?i)(AC|DC)\s*[-:]?\s*(\d+(?:\.\d+)?)\s*V", s)
    if m:
        feats["voltage"] = f"{m.group(1).upper()}{m.group(2)}V"

    # 功率
    m = re.search(r"(?i)(\d+(?:\.\d+)?)\s*W\s*(?:[/／]\s*(m|米))?", s)
    if m:
        feats["power"] = f"{m.group(1)}W{'/m' if m.group(2) else ''}"

    # 防护等级
    m = re.search(r"(?i)IP\s*(\d{2})", s)
    if m:
        feats["ip"] = f"IP{m.group(1)}"

    # 直径/口径（φ12 / DN100 / 100mm）
    m = re.search(r"[φΦ]\s*(\d+(?:\.\d+)?)", s)
    if m:
        feats["diameter"] = f"φ{m.group(1)}"
    m = re.search(r"(?i)DN\s*(\d+)", s)
    if m:
        feats["dn"] = f"DN{m.group(1)}"

    # 尺寸（1250X400 或 1000×500×80）（审查 P2-7 + P0-1：加边界约束 + 可选第三维）
    m = re.search(r"(?<![\dA-Za-z])(\d{2,4})\s*[xX×*]\s*(\d{2,4})(?:\s*[xX×*]\s*(\d{2,4}))?(?![\d年A-Za-z])", s)
    if m:
        dims = [m.group(1)]
        if m.group(2): dims.append(m.group(2))
        if m.group(3): dims.append(m.group(3))  # group(3) 由 (?:...)? 可选组提供，匹配到才存在
        feats["dimensions"] = "x".join(dims)

    # 色温（审查 P2-7：排除 K 后面跟字母/数字）
    m = re.search(r"(?<![\d.])(\d{3,5})\s*K(?![A-Za-z0-9])", s)
    if m:
        feats["kelvin"] = f"{m.group(1)}K"

    # 端口/通道
    m = re.search(r"(\d+)\s*端口", s)
    if m:
        feats["ports"] = f"{m.group(1)}端口"
    m = re.search(r"(\d+)\s*通道", s)
    if m:
        feats["channels"] = f"{m.group(1)}通道"

    return feats


def build_spec_context(spec: str) -> str:
    """把抽取的硬字段拼成 LLM 上下文行"""
    feats = extract_spec_features(spec)
    if not feats:
        return ""
    parts = [f"{k}={v}" for k, v in feats.items()]
    return f"关键规格参数：{'；'.join(parts)}"
