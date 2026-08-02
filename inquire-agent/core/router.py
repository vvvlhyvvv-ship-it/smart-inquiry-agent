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
    # P0-12: 单位换算相关
    unit_original: str = ""     # 原始单位（广材网返回的，换算前的）
    convert_note: str = ""      # 换算过程备注（如"10元/块 ÷ 0.8m ÷ 0.8m = 15.63元/m²"）
    # P0-14: 产品规格详情（广材网返回的完整规格串，用于换算尺寸解析）
    spec_detail: str = ""
    # 核实价（人工回填，供应商级——同材料不同供应商各有自己的核实价）
    verified_price: Optional[float] = None
    verified_note: str = ""

    def to_dict(self) -> dict:
        return {
            "supplier": self.supplier,
            "price": self.price,
            "unit": self.unit,
            "phone": self.phone,
            "product_title": self.product_title,
            "confidence": self.confidence,
            "is_anomaly": self.is_anomaly,
            "unit_original": self.unit_original,
            "convert_note": self.convert_note,
            "spec_detail": self.spec_detail,
            "verified_price": self.verified_price,
            "verified_note": self.verified_note,
        }


@dataclass
class SearchResult:
    """一次搜索的结构化结果"""
    source: str             # 来源平台：gldjc_ssr / ai_knowledge / ai_websearch / ai_fallback / failed
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
# 数据来源标签映射（统一常量，供 main.py / report.py 引用）
# ============================================================
SOURCE_LABEL = {
    "gldjc_ssr":     "广材网",
    "ai_knowledge":  "AI知识推理",
    "ai_websearch":  "AI联网搜索",
    "ai_fallback":   "AI知识兜底",
    "verified":      "人工核实",
}
