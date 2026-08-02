"""
core/ai_engine.py — AI 询价核心引擎（V5.2）

替代 v4.0 的 Playwright 爬虫方案。
通过 LLM 知识推理 + 联网搜索聚合材料价格。

三档降级链（对应三种置信度）：
  ① Agnes 2.0 Flash 知识推理（免费主力）→ confidence=medium
  ② DeepSeek 联网搜索（付费，需用户开启）→ confidence=high
  ③ DeepSeek 知识兜底（付费，无联网）   → confidence=low
"""

import json
import asyncio
from typing import Optional
from core.router import SearchResult, SupplierInfo
from utils.logger import get_logger

logger = get_logger(__name__)


# P0-10④: 精简prompt（重试时用，降reasoning消耗）
_SIMPLE_PROMPT = """请估算{name} {spec}的市场价格，返回{supplier_count}家供应商报价。
只返回JSON数组（不要其他文字）：
[{{"supplier":"供应商","price":数字,"unit":"{unit_str}","phone":"联系电话","confidence":"medium"}}]
"""

# ============================================================
# Prompt 模板
# ============================================================

_SYSTEM_PROMPT = "你是造价工程师的AI询价助手。请基于专业知识或联网搜索结果，给出材料的市场参考报价。只返回JSON。"

_KNOWLEDGE_PROMPT = """你是造价工程师的AI询价助手。请基于你的专业知识，估算以下材料的市场参考价格。

材料名称：{name}
规格型号：{spec}
单位：{unit}
{brand_line}{qty_line}{location_line}

要求：
1. 给出 {supplier_count} 家常见供应商/品牌的参考报价（基于你的训练知识）
2. 价格必须为数字，单位与材料单位一致
3. 每家供应商必须提供真实可拨打的联系电话，优先填手机号码（1开头的11位完整号码，如13812345678），其次座机（含区号，如010-12345678）。禁止使用XXX/XXXX等占位符，禁止部分隐藏。如无法确定真实手机号，给出该品牌官方400客服电话或厂商公布的销售热线。
4. 如不完全了解该材料，仍给出最合理的估算，并在 note 中说明
5. 这是AI知识推理价格，不含实时联网数据，仅供预算参考

返回JSON数组（只返回JSON，不要任何其他文字）：
[
  {{
    "supplier": "供应商或品牌名称",
    "price": 数字,
    "unit": "{unit_str}",
    "phone": "联系电话（含区号或手机号）",
    "product_title": "产品简述（20字内）",
    "confidence": "medium",
    "note": "价格依据简述"
  }}
]
"""

_WEBSEARCH_PROMPT = """你是造价工程师的AI询价助手。请联网搜索以下材料的最新市场报价。

材料名称：{name}
规格型号：{spec}
单位：{unit}
{brand_line}{qty_line}{location_line}

要求：
1. 联网搜索该材料的近期市场报价，聚合 {supplier_count} 家供应商/品牌
2. 价格必须为数字，优先采用含税含运费报价
3. 每家供应商必须提供真实可拨打的联系电话，优先填手机号码（1开头的11位完整号码，如13812345678），其次座机（含区号，如010-12345678）。禁止使用XXX/XXXX等占位符，禁止部分隐藏。从搜索结果中提取真实号码，或给出该品牌官方400客服电话。
4. 每条结果必须附带来源信息（source_url、source_title）
5. 如搜不到实时数据，返回空数组 []，不要编造

返回JSON数组（只返回JSON，不要任何其他文字）：
[
  {{
    "supplier": "供应商或品牌名称",
    "price": 数字,
    "unit": "{unit_str}",
    "phone": "联系电话（含区号或手机号）",
    "product_title": "产品简述（20字内）",
    "source_url": "价格来源网页URL",
    "source_title": "来源网页标题",
    "confidence": "high",
    "note": "价格依据简述"
  }}
]
"""


# ============================================================
# AI 询价引擎
# ============================================================

class AIEngine:
    """
    AI 询价核心引擎
    - 主力：Agnes 2.0 Flash（免费，知识推理）
    - 备用：DeepSeek（付费，联网搜索 + 知识兜底）
    - 触发联网：用户勾选「含联网实时价」开关
    """

    def __init__(self, enable_web_search: bool = True, supplier_count: int = 3):
        self.enable_web_search = enable_web_search
        self.supplier_count = supplier_count

    async def inquiry_single(self, material: dict) -> SearchResult:
        """
        单项材料询价（四档降级链，统一出口 _finalize）

        返回 SearchResult，source 字段标识来源：
          - "gldjc_ssr"     : 广材网 SSR 直抓（high，真实市场价）
          - "ai_knowledge"  : Agnes 知识推理（medium，经云筑AI校准）
          - "ai_websearch"  : DeepSeek 联网搜索（high，经云筑AI校准）
          - "ai_fallback"   : DeepSeek 知识兜底（low，经云筑AI校准）
          - "failed"        : 全部失败
        """
        name = material.get("name", "").strip()
        spec = material.get("spec", "").strip()
        unit = material.get("unit", "").strip()
        brand = material.get("brand", "").strip()
        qty = material.get("qty") or material.get("quantity") or ""
        location = material.get("location") or material.get("delivery_location") or ""

        if not name:
            return SearchResult.failed("材料名称为空", keyword=name)

        # 公共变量
        brand_line = f"参考品牌：{brand}\n" if brand else ""
        qty_line = f"数量：{qty}\n" if qty else ""
        location_line = f"交货地点：{location}\n" if location else ""

        # ---------- 第①档：广材网 SSR 直抓（精确价+电话，不校准） ----------
        try:
            from plugins.gldjc_ssr import GldjcSSRPlugin
            gldjc = GldjcSSRPlugin()
            result = await gldjc.search(name, spec, supplier_count=self.supplier_count)
            if result.success:
                # P0-12: 广材网结果也走_finalize做单位换算（但不做云筑校准）
                return await self._finalize(result, spec, location, user_unit=unit)
        except Exception as e:
            logger.warning(f"广材网查询失败，降级 AI: {e}")

        # ---------- 第②档：Agnes 知识推理（免费主力） ----------
        result = await self._try_agnes(name, spec, unit, brand_line, qty_line, location_line)
        if result.success:
            return await self._finalize(result, spec, location, user_unit=unit)

        # ---------- 第③档：DeepSeek 知识兜底（P2调整：兜底优先，联网放最后） ----------
        result = await self._try_deepseek_fallback(
            name, spec, unit, brand_line, qty_line, location_line
        )
        if result.success:
            return await self._finalize(result, spec, location, user_unit=unit)

        # ---------- 第④档：DeepSeek 联网搜索（最后兜底，用户开启时） ----------
        if self.enable_web_search:
            result = await self._try_deepseek_websearch(
                name, spec, unit, brand_line, qty_line, location_line
            )
            if result.success:
                return await self._finalize(result, spec, location, user_unit=unit)

        # 全部失败
        return SearchResult.failed("AI询价四档均失败", keyword=name)

    async def _finalize(self, result: SearchResult, spec: str = "", location: str = "",
                        user_unit: str = "") -> SearchResult:
        """
        统一收尾出口：
        1. 单位换算（P0-12: 广材网单位→用户单位）
        2. 云筑AI 校准（按材料维度一次，P0-2）
        3. 广材网精确价来源不校准（gldjc_ssr 已是真实价）
        """
        if result.source == "failed":
            return result

        # P0-12/P0-13: 单位换算（三态处理，fail保留原始单位）
        if user_unit and result.suppliers:
            from utils.unit_convert import convert_price
            for s in result.suppliers:
                if s.unit and s.unit != user_unit:
                    # P0-14: 尺寸来源优先用广材网产品规格
                    spec_for_convert = getattr(s, "spec_detail", "") or spec or ""
                    new_price, note, status = convert_price(s.price, s.unit, user_unit, spec_for_convert)
                    logger.info(f"[换算] {result.keyword}: {s.price}/{s.unit}→{user_unit} status={status} 新价={new_price} spec_detail=[{spec_for_convert[:60]}]")
                    if status == "ok":
                        # 换算成功：改价格+改单位+记过程
                        s.unit_original = s.unit
                        s.price = new_price
                        s.unit = user_unit
                        s.convert_note = note
                    elif status == "fail":
                        # P0-13②: 无法换算 → 保留原始单位和价格，只标注原因
                        s.unit_original = s.unit
                        s.convert_note = f"未换算：{note}"
                        # 不改 s.unit 和 s.price
                elif s.unit and s.unit == user_unit:
                    pass  # 单位相同，无需换算
                elif not s.unit:
                    # 广材网返回的unit为空，补上用户单位
                    s.unit = user_unit

        if result.source == "gldjc_ssr":
            return result  # 广材网精确价不做云筑校准

        # P0-2：只取供应商均价做一次校准，不逐个调
        prices = [s.price for s in result.suppliers if s.price and s.price > 0]
        if not prices:
            return result
        avg_price = sum(prices) / len(prices)

        try:
            from plugins.yzw_calibrator import YzwCalibrator
            calibrator = YzwCalibrator()
            cal = await calibrator.calibrate(
                price=avg_price,
                name=result.keyword,
                spec=spec,
                region=location,
            )
        except Exception as e:
            logger.warning(f"云筑AI校准异常，跳过: {e}")
            return result

        if cal["status"] == "revise":
            # 修正比例统一应用到所有供应商（保持相对价差）
            ratio = cal["price"] / avg_price
            for s in result.suppliers:
                if s.price > 0:
                    s.price = round(s.price * ratio, 2)
                s.confidence = cal["confidence"]
                s.is_anomaly = True
            logger.info(f"[校准] {result.keyword}: {cal.get('reason', '')}")
        elif cal["status"] == "pass":
            for s in result.suppliers:
                s.confidence = "high"
        # no_data：保持原样（confidence 不变）

        return result

    async def inquiry_batch(
        self,
        materials: list,
        progress_callback=None,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> list:
        """
        批量询价（顺序执行，控制 API 调用频率）

        - 顺序执行，每项间隔 1-2 秒（避免限流）
        - 通过 cancel_event 支持中途取消
        - 通过 progress_callback 推送进度
        """
        results = []
        total = len(materials)

        for idx, material in enumerate(materials, 1):
            # 取消检查
            if cancel_event and cancel_event.is_set():
                logger.info(f"⏹ 询价任务被取消，已完成 {len(results)}/{total}")
                break

            name = material.get("name", "")
            if progress_callback:
                await progress_callback("item_start", {
                    "index": idx,
                    "total": total,
                    "material": name,
                })

            try:
                result = await self.inquiry_single(material)
            except Exception as e:
                logger.error(f"询价异常: {name} - {e}")
                result = SearchResult.failed(str(e), keyword=name)

            results.append(result)

            if progress_callback:
                await progress_callback("item_done", {
                    "index": idx,
                    "total": total,
                    "material": name,
                    "success": result.success,
                    "source": result.source,
                    "supplier_count": len(result.suppliers),
                })

            # 项间间隔（最后一项不用等）
            if idx < total:
                await asyncio.sleep(3)  # P0-2: 间隔1.5s→3s，避免Agnes限流

        if progress_callback:
            await progress_callback("all_done", {
                "total": total,
                "completed": len(results),
            })

        return results

    # ============================================================
    # 三档降级实现
    # ============================================================

    async def _try_agnes(
        self, name, spec, unit, brand_line, qty_line, location_line
    ) -> SearchResult:
        """第①档：Agnes 知识推理"""
        from extract.extractor import call_llm

        prompt = _KNOWLEDGE_PROMPT.format(
            name=name, spec=spec, unit=unit, unit_str=unit or "元",
            supplier_count=self.supplier_count,
            brand_line=brand_line, qty_line=qty_line, location_line=location_line,
        )

        # P0-2: Agnes 失败重试1次（P0-10②: 退避5s→20s，限流窗口通常1分钟）
        raw = await call_llm("primary", prompt, _SYSTEM_PROMPT)
        if not raw:
            logger.info(f"[{name}] Agnes 首次调用失败，20秒后用精简prompt重试...")
            await asyncio.sleep(20)
            # P0-10④: 重试用精简prompt（降reasoning消耗）
            simple_prompt = _SIMPLE_PROMPT.format(name=name, spec=spec, unit_str=unit or "元", supplier_count=self.supplier_count)
            raw = await call_llm("primary", simple_prompt, _SYSTEM_PROMPT)
        if not raw:
            logger.info(f"[{name}] Agnes 重试仍失败，准备降级")
            return SearchResult(source="ai_knowledge", keyword=name, spec=spec,
                                material_unit=unit, error_message="Agnes调用失败")

        suppliers = self._parse_suppliers(raw, default_confidence="medium")
        if not suppliers:
            logger.info(f"[{name}] Agnes 未返回有效价格，准备降级")
            return SearchResult(source="ai_knowledge", keyword=name, spec=spec,
                                material_unit=unit, error_message="Agnes无有效价格")

        logger.info(f"[{name}] Agnes 知识推理成功，{len(suppliers)}家供应商")
        return SearchResult(
            source="ai_knowledge", keyword=name, spec=spec,
            material_unit=unit, suppliers=suppliers,
        )

    async def _try_deepseek_websearch(
        self, name, spec, unit, brand_line, qty_line, location_line
    ) -> SearchResult:
        """第②档：DeepSeek 联网搜索"""
        from extract.extractor import call_llm

        prompt = _WEBSEARCH_PROMPT.format(
            name=name, spec=spec, unit=unit, unit_str=unit or "元",
            supplier_count=self.supplier_count,
            brand_line=brand_line, qty_line=qty_line, location_line=location_line,
        )

        raw = await call_llm("fallback", prompt, _SYSTEM_PROMPT)
        if not raw:
            logger.info(f"[{name}] DeepSeek 联网搜索失败，降级到知识兜底")
            return SearchResult(source="ai_websearch", keyword=name, spec=spec,
                                material_unit=unit, error_message="DeepSeek联网失败")

        suppliers = self._parse_suppliers(raw, default_confidence="high")
        if not suppliers:
            logger.info(f"[{name}] DeepSeek 联网无结果，降级到知识兜底")
            return SearchResult(source="ai_websearch", keyword=name, spec=spec,
                                material_unit=unit, error_message="DeepSeek联网无结果")

        logger.info(f"[{name}] DeepSeek 联网搜索成功，{len(suppliers)}家供应商")
        return SearchResult(
            source="ai_websearch", keyword=name, spec=spec,
            material_unit=unit, suppliers=suppliers,
        )

    async def _try_deepseek_fallback(
        self, name, spec, unit, brand_line, qty_line, location_line
    ) -> SearchResult:
        """第③档：DeepSeek 知识兜底（无联网）"""
        from extract.extractor import call_llm

        prompt = _KNOWLEDGE_PROMPT.format(
            name=name, spec=spec, unit=unit, unit_str=unit or "元",
            supplier_count=self.supplier_count,
            brand_line=brand_line, qty_line=qty_line, location_line=location_line,
        )

        # P0-10③: DeepSeek 兜底也加重试1次（精简prompt）
        raw = await call_llm("fallback", prompt, _SYSTEM_PROMPT)
        if not raw:
            logger.info(f"[{name}] DeepSeek 兜底首次失败，10秒后用精简prompt重试...")
            await asyncio.sleep(10)
            simple_prompt = _SIMPLE_PROMPT.format(name=name, spec=spec, unit_str=unit or "元", supplier_count=self.supplier_count)
            raw = await call_llm("fallback", simple_prompt, _SYSTEM_PROMPT)
        if not raw:
            logger.warning(f"[{name}] DeepSeek 知识兜底失败：API返回空或异常（查看上方extractor日志）")
            return SearchResult(source="ai_fallback", keyword=name, spec=spec,
                                material_unit=unit, error_message="DeepSeek兜底失败")

        suppliers = self._parse_suppliers(raw, default_confidence="low")
        if not suppliers:
            logger.warning(f"[{name}] DeepSeek 知识兜底：API返回了内容但解析出0家供应商")
            return SearchResult(source="ai_fallback", keyword=name, spec=spec,
                                material_unit=unit, error_message="DeepSeek兜底无结果")

        logger.info(f"[{name}] DeepSeek 知识兜底成功，{len(suppliers)}家（low）")
        return SearchResult(
            source="ai_fallback", keyword=name, spec=spec,
            material_unit=unit, suppliers=suppliers,
        )

    # ============================================================
    # 工具方法
    # ============================================================

    def _parse_suppliers(self, raw: str, default_confidence: str = "medium") -> list:
        """解析 LLM 返回的 JSON 供应商列表，转为 SupplierInfo"""
        if not raw:
            return []

        raw = raw.strip()
        # 去除 markdown 代码块包裹
        if raw.startswith("```"):
            lines = raw.split("\n")
            # 去掉首尾的 ``` 行
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            raw = "\n".join(lines)

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning(f"JSON解析失败: {e}, 原始: {raw[:200]}")
            return []

        if not isinstance(data, list):
            data = [data]

        suppliers = []
        seen = set()  # P0-11④: AI返回供应商去重
        for item in data:
            if not isinstance(item, dict):
                continue
            price = item.get("price", 0)
            try:
                price = float(price)
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue

            # P0-11④: 按供应商名去重
            sup_name = item.get("supplier", "未知供应商").strip()
            if sup_name in seen:
                continue
            seen.add(sup_name)

            suppliers.append(SupplierInfo(
                supplier=sup_name,
                price=price,
                unit=item.get("unit", ""),
                phone=self._clean_phone(item.get("phone", "")),
                product_title=item.get("product_title", ""),
                confidence=item.get("confidence", default_confidence),
                is_anomaly=False,
            ))

        return suppliers

    @staticmethod
    def _clean_phone(phone: str) -> str:
        """清理电话号码：含占位符(X/x/*)的假号码返回空字符串"""
        if not phone:
            return ""
        phone = phone.strip()
        # 含 X/x/* 说明是占位符，用户无法拨打
        if any(c in phone for c in "Xx*"):
            return ""
        return phone
