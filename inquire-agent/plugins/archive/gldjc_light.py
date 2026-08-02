"""
plugins/gldjc.py — 广材网搜索插件

⚠️ 已由 gldjc_ssr.py 替代（V5.2 起，Playwright + __NUXT__ SSR 方案）。
本文件为归档保留，如需启用需适配 V5.2 数据结构（SearchResult/Supplier 字段已变更）。
"""

import base64
import asyncio
import time
import re
from playwright.async_api import async_playwright
from plugins.base import SearchPlugin, STEALTH_ARGS
from core.router import SearchResult
from utils.logger import get_logger

logger = get_logger(__name__)


class GldjcPlugin(SearchPlugin):
    name = "gldjc"
    priority = 2  # 建筑材料优先
    state_file = "accounts/gldjc_state.json"
    login_indicator = ".user-info, .login-status, [class*='userName']"

    _playwright = None
    _browser = None

    @classmethod
    async def _get_browser(cls):
        if cls._playwright is None:
            cls._playwright = await async_playwright().start()
            cls._browser = await cls._playwright.chromium.launch(
                channel="msedge",
                headless=False,
                args=["--window-position=-3000,0"] + STEALTH_ARGS,
            )
            logger.info("[广材网] 浏览器实例已启动")
        return cls._browser

    @classmethod
    async def close_browser(cls):
        if cls._browser:
            await cls._browser.close()
        if cls._playwright:
            await cls._playwright.stop()
        cls._browser = None
        cls._playwright = None

    def _home_url(self) -> str:
        return "https://www.gldjc.com/"

    async def search(self, keyword: str, spec: str = "") -> SearchResult:
        search_url = f"https://www.gldjc.com/scj/so.html?l=1&keyword={keyword}"

        browser = await self._get_browser()
        context = await browser.new_context(
            storage_state=self.state_file,
            viewport={"width": 1280, "height": 1200},
        )
        page = await context.new_page()

        screenshot_b64 = ""
        screenshot_path = ""
        try:
            await page.goto(
                search_url, timeout=30000, wait_until="domcontentloaded"
            )
            await asyncio.sleep(4)

            content = await page.content()
            if "请登录" in content or "登录后查看" in content:
                return SearchResult.failed("登录态失效", keyword)

            # DOM 中提取电话（正则匹配，绕过 m-vague 遮罩）
            dom_phones = {}  # {supplier_name_fragment: phone}
            phone_pattern = re.compile(r'1[3-9]\d{9}')
            # 找每个电话号码附近的文字（作为供应商名线索）
            for match in phone_pattern.finditer(content):
                phone = match.group()
                # 取电话前后各80字符
                start = max(0, match.start() - 80)
                end = min(len(content), match.end() + 80)
                context_text = content[start:end]
                # 清理HTML标签
                context_clean = re.sub(r'<[^>]+>', ' ', context_text)
                dom_phones[context_clean.strip()] = phone
            logger.debug(f"[广材网] DOM中提取到 {len(dom_phones)} 个电话")

            # 截图 → base64
            screenshot_bytes = await page.screenshot(type="png", full_page=False)
            screenshot_b64 = base64.b64encode(screenshot_bytes).decode()

            ts = int(time.time())
            screenshot_path = f"data/images/{keyword}_{ts}.png"
            await page.screenshot(path=screenshot_path)

            logger.info(f"[广材网] 搜索 {keyword}: 截图 {len(screenshot_b64)//1024}KB")

        except Exception as e:
            logger.error(f"[广材网] 搜索异常: {keyword} - {e}")
        finally:
            await context.close()

        # 截图 → 视觉 LLM 提取价格
        from extract.extractor import extract_prices_from_image, check_price_anomaly, supplement_phone_numbers
        suppliers, error = [], None
        if screenshot_b64:
            suppliers, error = await extract_prices_from_image(
                screenshot_b64, keyword, "广材网"
            )
            if error == "login_required":
                return SearchResult.failed("登录态失效", keyword)
            if suppliers:
                # 匹配 DOM 中提取的电话到供应商
                for s in suppliers:
                    if isinstance(s, dict) and not s.get("phone", "").strip():
                        supplier_name = s.get("supplier", "")
                        # 在 DOM 电话上下文中搜索供应商名片段
                        for ctx, phone in dom_phones.items():
                            # 取供应商名关键部分（前4个字或全名）
                            name_key = supplier_name[:4] if len(supplier_name) > 4 else supplier_name
                            if name_key and name_key in ctx:
                                s["phone"] = phone
                                logger.info(f"[广材网] 📞 匹配电话: {supplier_name} → {phone}")
                                break

                suppliers = await check_price_anomaly(keyword, suppliers, spec)
                # 只用LLM补电话当DOM也没找到时
                suppliers = await supplement_phone_numbers(keyword, suppliers)
            if not suppliers:
                logger.info(f"[广材网] 视觉提取无结果: {keyword} → 将降级到AI兜底")

        return SearchResult(
            source="广材网",
            keyword=keyword,
            spec=spec,
            suppliers=suppliers,
            screenshot_path=screenshot_path,
            search_url=search_url,
        )
