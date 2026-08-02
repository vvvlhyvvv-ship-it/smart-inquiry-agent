"""
plugins/yzw_calibrator.py — 云筑AI 价格校准器（v5.2，审查修订版）
作用：用云筑AI返回的价格区间校准 LLM 推理价，防 LLM 价格失常
位置：仅后台调用，不体现在前端
修订：P0-2 校准按材料维度一次（不按供应商N次）；P0-3 改 httpx 异步；P2-8 topicCode 走配置
"""

import json
import time
import re
from pathlib import Path
import httpx
from utils.config import get_env  # 读取 .env
from utils.logger import get_logger

logger = get_logger(__name__)

CHAT_URL = "https://agw-stream.yzw.cn/api/mtg-ai/chat/stream"
TOPIC_CODE = get_env("YZW_TOPIC_CODE", "2083484635715223598")  # .env 配置，失效改配置


class YzwCalibrator:
    """云筑AI 价格区间校准器"""

    def __init__(self):
        self.cookies = self._load_cookies()  # accounts/yzw_cookies.json

    @staticmethod
    def _load_cookies() -> dict:
        """审查 P1-1：补 _load_cookies 实现（与 GldjcSSRPlugin 一致）"""
        p = Path("accounts/yzw_cookies.json")
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    async def get_price_range(self, name: str, spec: str = "", region: str = "", price_to_check: float = 0) -> tuple | None:
        """向云筑AI 提问，获取价格区间 (min, max)——按材料维度一次"""
        question = f"查询{region}{name}{spec}的价格" if region else f"查询{name}{spec}的价格"
        body = {
            "question": question,
            "topicCode": TOPIC_CODE,
            "deepThinking": "R1",
        }
        headers = {"Content-Type": "application/json"}
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                async with client.stream("POST", CHAT_URL, json=body, headers=headers) as resp:
                    # 拼接所有 TEXT 类型消息（最终答案），跳过 THINK（思考过程）
                    answer_text = ""
                    async for line in resp.aiter_lines():
                        if line.startswith("data:") and "{" in line:
                            try:
                                d = json.loads(line[5:])
                                if d.get("type") == "TEXT":
                                    answer_text += d.get("content", "")
                            except Exception:
                                pass
            return self._extract_range(answer_text, material_name=name, price_to_check=price_to_check)
        except Exception as e:
            logger.error(f"[云筑AI] 校准失败: {e}")
            return None
        """
        从完整答案文本中提取最小~最大参考价
        P0-3: 加材料名匹配校验 + 区间合理性校验，防串台
        """
        # P0-3-1: 材料名匹配校验——答案文本必须包含材料名关键词
        if material_name:
            # 取材料名前2-3个字作为关键词（如"乔木-五角枫"取"五角枫"）
            key = material_name.replace("乔木-", "").replace("灌木-", "").strip()
            if key and len(key) >= 2 and key not in text and material_name not in text:
                logger.warning(f"[云筑AI] 答案未包含材料名'{material_name}'，判定串台，跳过校准")
                return None

        # 优先匹配带"元"的价格区间（如 "271.26～297.25元/吨"）
        matches_with_unit = re.findall(r"(\d+\.?\d*)\s*[～~]\s*(\d+\.?\d*)\s*元", text)
        if matches_with_unit:
            low, high = matches_with_unit[0]
            low, high = float(low), float(high)
            if 0 < low <= high:
                # P0-3-2: 区间合理性校验——与待校准价差>5倍判定异常
                if price_to_check > 0:
                    if low > price_to_check * 5 or high < price_to_check / 5:
                        logger.warning(f"[云筑AI] 区间[{low}~{high}]与价格{price_to_check}差>5倍，判定异常，跳过校准")
                        return None
                return (low, high)

        # 退化：找所有 ~ 分隔的数字对
        matches = re.findall(r"(\d+\.?\d*)\s*[～~]\s*(\d+\.?\d*)", text)
        if not matches:
            return None
        low, high = matches[0]
        low, high = float(low), float(high)
        if 0 < low <= high:
            if price_to_check > 0:
                if low > price_to_check * 5 or high < price_to_check / 5:
                    logger.warning(f"[云筑AI] 区间[{low}~{high}]与价格{price_to_check}差>5倍，判定异常，跳过校准")
                    return None
            return (low, high)
        return None

    def _extract_range(self, text: str, material_name: str = "", price_to_check: float = 0) -> tuple | None:
        """
        从完整答案文本中提取最小~最大参考价
        P0-3: 加材料名匹配校验 + 区间合理性校验，防串台
        """
        # P0-3-1: 材料名匹配校验——答案文本必须包含材料名关键词
        if material_name:
            key = material_name.replace("乔木-", "").replace("灌木-", "").strip()
            if key and len(key) >= 2 and key not in text and material_name not in text:
                logger.warning(f"[云筑AI] 答案未包含材料名'{material_name}'，判定串台，跳过校准")
                return None

        # 优先匹配带"元"的价格区间
        matches_with_unit = re.findall(r"(\d+\.?\d*)\s*[～~]\s*(\d+\.?\d*)\s*元", text)
        if matches_with_unit:
            low, high = matches_with_unit[0]
            low, high = float(low), float(high)
            if 0 < low <= high:
                if price_to_check > 0 and (low > price_to_check * 5 or high < price_to_check / 5):
                    logger.warning(f"[云筑AI] 区间[{low}~{high}]与价格{price_to_check}差>5倍，判定异常，跳过校准")
                    return None
                return (low, high)

        # 退化：找所有 ~ 分隔的数字对
        matches = re.findall(r"(\d+\.?\d*)\s*[～~]\s*(\d+\.?\d*)", text)
        if not matches:
            return None
        low, high = matches[0]
        low, high = float(low), float(high)
        if 0 < low <= high:
            if price_to_check > 0 and (low > price_to_check * 5 or high < price_to_check / 5):
                logger.warning(f"[云筑AI] 区间[{low}~{high}]与价格{price_to_check}差>5倍，判定异常，跳过校准")
                return None
            return (low, high)
        return None

    async def calibrate(self, price: float, name: str, spec: str = "", region: str = "") -> dict:
        """
        校准 LLM 推理价（P0-2：按材料维度一次调用）
        返回: {"status": "pass"/"revise"/"no_data", "price": 修正后价格, "confidence": ...}
        """
        rng = await self.get_price_range(name, spec, region, price_to_check=price)
        if not rng:
            return {"status": "no_data", "price": price, "confidence": "low"}

        low, high = rng
        if low * 0.9 <= price <= high * 1.1:
            return {"status": "pass", "price": price, "confidence": "high"}
        if price < low * 0.9:
            return {"status": "revise", "price": low, "confidence": "medium",
                    "reason": f"低于云筑区间[{low}~{high}]，已修正至下限"}
        return {"status": "revise", "price": high, "confidence": "medium",
                "reason": f"高于云筑区间[{low}~{high}]，已修正至上限"}
