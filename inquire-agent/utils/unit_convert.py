"""
utils/unit_convert.py — 价格单位换算（P0-12）
用途：广材网/AI返回的价格单位与用户清单单位不一致时，按规格尺寸换算
规则：根据材料规格中的尺寸（长×宽×高）进行面积/体积/长度换算
"""

import re


# 单位分组（同组视为同量纲，可换算）
UNIT_GROUPS = {
    "area": ["m²", "㎡", "平方米", "平方", "m2", "M2"],
    "piece_area": ["块", "片", "张", "件"],      # 面积类（需尺寸换算）
    "length": ["m", "米", "metre"],
    "piece_length": ["根", "条", "卷"],           # 长度类（需尺寸换算）
    "weight": ["t", "吨", "kg", "千克", "公斤"],
    "piece_weight": ["袋", "包"],                 # 重量类（需规格换算）
    "count": ["个", "套", "台", "樘", "组", "副", "把", "只", "扇", "根"],
    "volume": ["m³", "立方米", "方"],
}


def _get_unit_group(unit: str) -> str:
    """判断单位属于哪个量纲组"""
    u = unit.strip().lower().replace("元/", "").replace("/", "")
    for group, units in UNIT_GROUPS.items():
        if u in [x.lower() for x in units]:
            return group
    return ""


def _parse_dimensions(spec: str) -> dict:
    """
    从规格文本解析尺寸
    返回: {"length": m, "width": m, "height": m, "count": n}
    支持: 800×800mm / 800*800 / 0.8m×0.8m / 1200*2100mm / 600*600*20
    """
    s = spec or ""
    dims = {}

    # 匹配 数字×数字 或 数字*数字（1-3维），带可选单位
    m = re.search(
        r"(\d+(?:\.\d+)?)\s*[xX×*]\s*(\d+(?:\.\d+)?)(?:\s*[xX×*]\s*(\d+(?:\.\d+)?))?\s*(mm|cm|m)?",
        s,
    )
    if m:
        vals = [float(m.group(1)), float(m.group(2))]
        if m.group(3):
            vals.append(float(m.group(3)))
        unit = m.group(4) or "mm"  # 默认mm

        # 转成米
        factor = {"mm": 0.001, "cm": 0.01, "m": 1.0}.get(unit, 0.001)
        vals_m = [v * factor for v in vals]

        dims["length"] = vals_m[0]
        dims["width"] = vals_m[1]
        if len(vals_m) >= 3:
            dims["height"] = vals_m[2]
        dims["area"] = vals_m[0] * vals_m[1]

    # 匹配 单根/单袋 等包装规格
    m2 = re.search(r"(\d+(?:\.\d+)?)\s*(m|米|kg|千克|公斤)\s*[/／]?\s*(根|条|卷|袋|包|块|片|张)", s)
    if m2:
        val = float(m2.group(1))
        u = m2.group(2)
        pkg = m2.group(3)
        if u in ("m", "米"):
            dims["per_length"] = val
        elif u in ("kg", "千克", "公斤"):
            dims["per_weight"] = val

    return dims


def convert_price(
    price: float,
    from_unit: str,
    to_unit: str,
    spec: str = "",
) -> tuple:
    """
    单位换算

    参数:
      price: 原始价格
      from_unit: 原始单位（如 "块"）
      to_unit: 目标单位（如 "m²"）
      spec: 材料规格（用于解析换算尺寸）

    返回: (换算后价格, 换算过程备注, 状态)
      状态="ok"        换算成功（价格已变，备注有过程）
      状态="no_change"  无需换算（同单位/同量纲）
      状态="fail"       无法换算（价格原值，备注说明原因）
    """
    from_unit = (from_unit or "").strip()
    to_unit = (to_unit or "").strip()

    # 同单位不换算
    if not from_unit or not to_unit or from_unit == to_unit:
        return price, "", "no_change"

    from_group = _get_unit_group(from_unit)
    to_group = _get_unit_group(to_unit)

    # 同组内换算（如 m²→㎡）
    if from_group == to_group and from_group:
        if from_group in ("area", "length", "weight", "volume"):
            return price, "", "no_change"  # 同量纲不同写法，价格不变

    # 数量类互等（个/套/台/樘）
    if from_group == "count" and to_group == "count":
        return price, "", "no_change"

    dims = _parse_dimensions(spec)

    # 块/片/张 → m²（需面积尺寸）
    if from_group == "piece_area" and to_group == "area":
        area = dims.get("area", 0)
        if area > 0:
            new_price = price / area
            l = dims.get("length", 0)
            w = dims.get("width", 0)
            note = f"{price}元/{from_unit} ÷ ({l}m×{w}m={area:.4f}m²) = {new_price:.2f}元/{to_unit}"
            return round(new_price, 2), note, "ok"
        return price, f"单位不一致({from_unit}→{to_unit})，缺换算尺寸", "fail"

    # m² → 块/片/张
    if from_group == "area" and to_group == "piece_area":
        area = dims.get("area", 0)
        if area > 0:
            new_price = price * area
            note = f"{price}元/m² × {area:.4f}m² = {new_price:.2f}元/{to_unit}"
            return round(new_price, 2), note, "ok"
        return price, f"单位不一致({from_unit}→{to_unit})，缺换算尺寸", "fail"

    # m² → 樘/套（门等，需面积）
    if from_group == "area" and to_group == "count":
        area = dims.get("area", 0)
        if area > 0:
            new_price = price * area
            note = f"{price}元/m² × {area:.4f}m² = {new_price:.2f}元/{to_unit}"
            return round(new_price, 2), note, "ok"
        return price, f"单位不一致({from_unit}→{to_unit})，缺换算尺寸", "fail"

    # 樘/套 → m²
    if from_group == "count" and to_group == "area":
        area = dims.get("area", 0)
        if area > 0:
            new_price = price / area
            note = f"{price}元/{from_unit} ÷ {area:.4f}m² = {new_price:.2f}元/{to_unit}"
            return round(new_price, 2), note, "ok"
        return price, f"单位不一致({from_unit}→{to_unit})，缺换算尺寸", "fail"

    # 根/条 → m（需单根长度）
    if from_group == "piece_length" and to_group == "length":
        per_len = dims.get("per_length", 0)
        if per_len > 0:
            new_price = price / per_len
            note = f"{price}元/{from_unit} ÷ {per_len}m/根 = {new_price:.2f}元/{to_unit}"
            return round(new_price, 2), note, "ok"
        return price, f"单位不一致({from_unit}→{to_unit})，缺换算尺寸", "fail"

    # 袋/包 → kg/t（需单袋重量）
    if from_group == "piece_weight" and to_group == "weight":
        per_w = dims.get("per_weight", 0)
        if per_w > 0:
            new_price = price / per_w
            note = f"{price}元/{from_unit} ÷ {per_w}kg/袋 = {new_price:.2f}元/{to_unit}"
            return round(new_price, 2), note, "ok"
        return price, f"单位不一致({from_unit}→{to_unit})，缺换算尺寸", "fail"

    # 不同量纲且无规则 → 不换算
    if from_group != to_group:
        return price, f"单位不一致({from_unit}→{to_unit})，缺换算尺寸", "fail"

    return price, "", "no_change"
