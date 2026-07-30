"""
core/router.py — 材料路由 + SearchResult/SupplierInfo 数据类
"""

from dataclasses import dataclass, field
from typing import Optional


# ============================================================
# 核心数据类（整个系统的基础数据结构）
# ============================================================

@dataclass
class SupplierInfo:
    """供应商报价信息"""
    supplier: str           # 供应商名称
    price: float            # 报价（数字）
    unit: str = ""          # 价格单位（元/吨、元/个）
    phone: str = ""         # 联系电话
    product_title: str = "" # 产品标题（精简后）
    confidence: str = "medium"  # high / medium / low
    is_anomaly: bool = False    # 是否价格异常

    def to_dict(self) -> dict:
        return {
            "supplier": self.supplier,
            "price": self.price,
            "unit": self.unit,
            "phone": self.phone,
            "product_title": self.product_title,
            "confidence": self.confidence,
            "is_anomaly": self.is_anomaly,
        }


@dataclass
class SearchResult:
    """一次搜索的结构化结果"""
    source: str             # 来源平台：1688 / mysteel / gldjc / ai_fallback
    keyword: str            # 搜索关键词
    spec: str = ""          # 规格参数
    material_unit: str = "" # 材料单位（如 株、吨、m²）
    suppliers: list = field(default_factory=list)  # List[SupplierInfo]
    screenshot_path: str = ""
    search_url: str = ""
    task_id: str = ""
    error_message: str = ""

    @property
    def success(self) -> bool:
        return len(self.suppliers) > 0 and not self.error_message

    @classmethod
    def failed(cls, reason: str, keyword: str = "") -> "SearchResult":
        """创建失败结果"""
        return cls(source="failed", keyword=keyword, error_message=reason)

    @classmethod
    def ai_fallback(cls, keyword: str, spec: str = "") -> "SearchResult":
        """创建 AI 推理降级结果"""
        return cls(
            source="ai_fallback",
            keyword=keyword,
            spec=spec,
            suppliers=[],
            error_message="所有平台均搜索失败，请人工确认价格",
        )

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "keyword": self.keyword,
            "spec": self.spec,
            "material_unit": self.material_unit,
            "suppliers": [s.to_dict() for s in self.suppliers],
            "screenshot_path": self.screenshot_path,
            "search_url": self.search_url,
            "success": self.success,
            "error_message": self.error_message,
        }


# ============================================================
# 材料路由规则
# ============================================================

ROUTING_RULES = {
    "mysteel": {
        "keywords": [
            "钢筋", "螺纹钢", "盘螺", "线材", "圆钢", "型钢", "钢板",
            "钢管", "H型钢", "工字钢", "槽钢", "角钢", "扁钢", "方钢",
            "钢结构", "不锈钢", "钢绞线", "钢丝", "钢格板", "镀锌",
        ],
        "priority": 1,
    },
    "gldjc": {
        "keywords": [
            "水泥", "混凝土", "砂浆", "砂", "石", "砖", "砌块",
            "防水", "保温", "涂料", "油漆", "门窗", "玻璃",
            "管材", "管件", "电缆", "电线", "桥架", "开关", "插座",
            "瓷砖", "石材", "地板", "吊顶", "卫浴", "阀门",
            "泵", "风机", "空调", "消防", "报警", "监控",
        ],
        "priority": 2,
    },
}


def route_material(material_name: str) -> str:
    """根据材料名称返回目标平台"""
    name = material_name.lower()

    for kw in ROUTING_RULES["mysteel"]["keywords"]:
        if kw in name:
            return "mysteel"

    for kw in ROUTING_RULES["gldjc"]["keywords"]:
        if kw in name:
            return "gldjc"

    return "gldjc"  # 默认走广材网


def get_fallback_chain(platform: str) -> list:
    """返回指定平台的降级链（广材网优先，钢材→钢铁网，1688最后）"""
    order = {
        "mysteel": ["mysteel", "gldjc", "1688"],   # 钢材：钢铁网→广材网→1688
        "gldjc":   ["gldjc", "mysteel", "1688"],    # 通用：广材网→钢铁网→1688
        "1688":    ["gldjc", "mysteel", "1688"],     # 特殊：也先试广材网→钢铁网→1688
    }
    return order.get(platform, ["gldjc", "mysteel", "1688"])
