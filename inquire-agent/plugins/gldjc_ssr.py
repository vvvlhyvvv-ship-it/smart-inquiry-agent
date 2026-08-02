"""
plugins/gldjc_ssr.py — 广材网数据抓取插件（v5.2，Playwright + __NUXT__ 方案）
原理：广材网是 Nuxt SSR，数据在 window.__NUXT__.data[0].searchResData
     用 Playwright 加载页面后 page.evaluate 读取，拿到完整结构化数据
借鉴：material-price-audit 的 _search_gldjc 实现
"""

import asyncio
import time
from datetime import datetime
from pathlib import Path
import json
from core.router import SearchResult, SupplierInfo
from utils.logger import get_logger

logger = get_logger(__name__)

COOKIE_FILE = Path("accounts/gldjc_cookies.json")
SEARCH_URL = "https://www.gldjc.com/scj/so.html?l=1&keyword={keyword}"
MIN_INTERVAL = 5.0
DAILY_LIMIT = 500

# 浏览器 profile 目录（复用登录态，与 main.py 的登录功能共用）
PROFILE_DIR = Path(".browser-profile-gldjc")


class GldjcSSRPlugin:
    """广材网 Playwright 抓取插件"""

    name = "gldjc_ssr"

    _last_search_ts = 0.0
    _daily_count = 0
    _daily_date = ""
    _playwright = None
    _browser = None

    @classmethod
    async def _throttle(cls) -> None:
        wait = MIN_INTERVAL - (time.time() - cls._last_search_ts)
        if wait > 0:
            await asyncio.sleep(wait)
        today = datetime.now().strftime("%Y-%m-%d")
        if cls._daily_date != today:
            cls._daily_date, cls._daily_count = today, 0
        if cls._daily_count >= DAILY_LIMIT:
            raise RuntimeError("广材网今日已达500次上限")

    @staticmethod
    def save_cookies(cookies: dict):
        COOKIE_FILE.parent.mkdir(exist_ok=True)
        COOKIE_FILE.write_text(json.dumps(cookies), encoding="utf-8")
        logger.info(f"[广材网] cookie 已保存: {len(cookies)} 个")

    @staticmethod
    def load_cookies() -> dict:
        if COOKIE_FILE.exists():
            try:
                return json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    async def _get_browser(self):
        """获取复用的 Playwright 持久化 profile 浏览器"""
        cls = type(self)
        if cls._browser:
            try:
                pages = cls._browser.pages
                if pages is not None:
                    return cls._browser
            except Exception:
                pass

        from playwright.async_api import async_playwright
        cls._playwright = await async_playwright().start()
        PROFILE_DIR.mkdir(exist_ok=True)
        cls._browser = await cls._playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR.resolve()),
            headless=True,
            viewport={"width": 1400, "height": 900},
            locale="zh-CN",
            args=["--disable-blink-features=AutomationControlled"],
        )
        logger.info(f"[广材网] 持久化profile浏览器已启动: {PROFILE_DIR}")
        return cls._browser

    async def search(self, keyword: str, spec: str = "", supplier_count: int = 3) -> SearchResult:
        """搜索材料，用 Playwright 读 window.__NUXT__

        P0-4: 加名称匹配(JS端) + 规格匹配(Python端) + 数量截断
        P0-5: 规格数值化区间比较 + 价格离群过滤
        P0-6: wait_for_function 等数据 + 空结果重试1次
        """
        url = SEARCH_URL.format(keyword=keyword)
        cookies = self.load_cookies()
        if not cookies:
            return SearchResult.failed("广材网未配置 cookie，请先登录", keyword=keyword)

        try:
            await self._throttle()
        except RuntimeError as e:
            return SearchResult.failed(str(e), keyword=keyword)

        try:
            context = await self._get_browser()

            # P0-6: 封装页面加载逻辑，支持重试
            async def _load_and_parse(page):
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)

                # 检查是否跳登录页
                if "/login" in page.url:
                    return None, "广材网登录已失效，请重新登录"

                # P0-6①: 用 wait_for_function 等数据出现（替换固定等待）
                try:
                    await page.wait_for_function(
                        "window.__NUXT__ && window.__NUXT__.data && window.__NUXT__.data[0] && "
                        "window.__NUXT__.data[0].searchResData && window.__NUXT__.data[0].searchResData.length > 0",
                        timeout=15000,
                    )
                except Exception:
                    return None, None  # 超时，返回None表示无数据（触发重试）

                # 点击"查看更多报价"展开
                for _ in range(12):
                    more = page.get_by_text("查看更多报价", exact=True)
                    if await more.count() <= 0:
                        break
                    try:
                        btn = more.first
                        if not await btn.is_visible():
                            break
                        await btn.click(force=True, timeout=1500)
                        await page.wait_for_timeout(200)
                    except Exception:
                        break

                return page, None

            page = await context.new_page()
            try:
                # 第一次尝试
                _, err = await _load_and_parse(page)
                if err == "广材网登录已失效，请重新登录":
                    return SearchResult.failed(err, keyword=keyword)

                # P0-6②: 空结果重试1次
                if err is None and _ is None:
                    logger.info(f"[广材网] {keyword}: 首次加载无数据，3秒后重试...")
                    await page.wait_for_timeout(3000)
                    _, err = await _load_and_parse(page)
                    if err is None and _ is None:
                        # P0-6③: 重试仍空 → 返回failed，走AI兜底
                        logger.warning(f"[广材网] {keyword}: 重试仍无数据，降级AI")
                        return SearchResult.failed("广材网页面数据未加载", keyword=keyword)

                if err:
                    return SearchResult.failed(err, keyword=keyword)

                # 读取 window.__NUXT__ 数据（P0-4: 加名称匹配，传入关键词）
                rows = await page.evaluate("""(arg) => {
                    var kw = arg.kw || '';
                    var data = (window.__NUXT__ && window.__NUXT__.data && window.__NUXT__.data[0]) || {};
                    var products = Array.isArray(data.searchResData) ? data.searchResData : [];
                    var out = [];

                    // P0-4: 名称匹配函数
                    function nameMatch(name) {
                        if (!kw) return true;
                        var n = String(name || '').toLowerCase();
                        var k = String(kw).toLowerCase();
                        // 去噪：剥掉前缀再匹配
                        var coreKw = k.replace(/^乔木-|^灌木-|^材料|^苗木/, '').trim();
                        if (coreKw && n.indexOf(coreKw) > -1) return true;
                        return n.indexOf(k) > -1;
                    }

                    for (var pi = 0; pi < products.length; pi++) {
                        var p = products[pi] || {};
                        var name = String(p.name || p.core_word_precise || '').trim();
                        // P0-4: 名称不匹配则跳过该产品
                        if (!nameMatch(name)) continue;

                        var specParts = [];
                        var specArr = p.specDataArr || [];
                        for (var si = 0; si < specArr.length; si++) {
                            var x = specArr[si] || {};
                            var label = String(x.name || '').trim();
                            var value = String(x.desc || '').trim();
                            if (label || value) specParts.push((label + ' : ' + value).trim());
                        }
                        var base = specParts.join(' | ') || String(p.specificationattr_str || p.specificationattr_all_str || '').trim();
                        var companies = p.companies || [];
                        for (var ci = 0; ci < companies.length; ci++) {
                            var c = companies[ci] || {};
                            var price = c.market_price || c.engineering_price || c.market_price_te || '';
                            if (!name || price === '' || price == null) continue;
                            var supplier = String(c.company_name || c.name || '').trim();
                            var phone = String(c.company_phone || '').trim();
                            var contact = String(c.company_contact_person || '').trim();
                            var brand = String(c.brand_name || '').trim();
                            var unit = String(c.unit || '').trim();
                            out.push({name: name, base: base, supplier: supplier, phone: phone, contact: contact, brand: brand, unit: unit, price: String(price)});
                        }
                    }
                    return out.slice(0, 50);
                }""", {"kw": keyword})

                # P0-5: 规格数值化区间匹配（替换子串匹配）
                import re as _re

                def _parse_spec_ranges(spec_text: str) -> list:
                    """解析规格文本，返回 [(参数名, [最小, 最大]), ...]
                    如 '高度4.5-5.0m,胸径d=8-10cm,冠径3.5-4m' →
                    [('高度',[4.5,5.0]),('胸径',[8,10]),('冠径',[3.5,4])]
                    """
                    if not spec_text:
                        return []
                    results = []
                    # 匹配 参数名+数字+区间（如 胸径d=8-10cm / 高度4.5-5.0m）
                    for m in _re.finditer(r"([^\d,，\s]+?)\s*[=：:]?\s*(\d+(?:\.\d+)?)\s*[-~—至]\s*(\d+(?:\.\d+)?)", spec_text):
                        param = m.group(1).strip()
                        lo, hi = float(m.group(2)), float(m.group(3))
                        results.append((param, [min(lo, hi), max(lo, hi)]))
                    return results

                spec_ranges = _parse_spec_ranges(spec)

                def _spec_match_v2(row_base: str) -> bool:
                    """P0-5: 数值化区间比较——产品规格数值需落在用户区间内"""
                    if not spec_ranges:
                        return True  # 用户没填规格 → 不卡
                    base_str = str(row_base or "")
                    hit_count = 0
                    for param, (lo, hi) in spec_ranges:
                        # 从产品规格串中提取该参数附近的数字
                        # 如 base="高度 : 4.8m | 胸径 : 9cm" → 找"胸径"后的数字
                        param_key = param.replace("d", "").replace("D", "").strip()[:2]  # 去掉d等后缀，取前2字
                        # 在base里找param_key，然后提取其后的数字
                        for m in _re.finditer(param_key + r"[^\d]*?(\d+(?:\.\d+)?)", base_str):
                            val = float(m.group(1))
                            if lo <= val <= hi:
                                hit_count += 1
                                break
                        # 也检查不带参数名的区间（直接在base里找数值）
                        for m in _re.finditer(r"(?<![.\d])(\d+(?:\.\d+)?)(?!\d)", base_str):
                            val = float(m.group(1))
                            if lo <= val <= hi:
                                hit_count += 1
                                break
                    # P0-5②: 多参数需至少命中一半（最少命中1个）
                    need = max(1, len(spec_ranges) // 2)
                    return hit_count >= need

                # P0-11: 按供应商名去重（同名只保留第一条）
                seen_suppliers = set()
                suppliers = []
                for row in rows:
                    try:
                        price = float(row.get("price", 0))
                    except (TypeError, ValueError):
                        continue
                    if price <= 0:
                        continue

                    # P0-5: 规格数值化匹配
                    if not _spec_match_v2(row.get("base", "")):
                        continue

                    # P0-11: 供应商去重
                    supplier_name = row.get("supplier", "").strip()
                    if not supplier_name:
                        supplier_name = row.get("contact", "").split(":")[0] if row.get("contact") else ""
                    if supplier_name in seen_suppliers:
                        continue
                    seen_suppliers.add(supplier_name)

                    # 从 contact 提取手机号
                    phone = row.get("phone", "")
                    contact = row.get("contact", "")
                    if not phone and contact:
                        m = _re.search(r"(1[3-9]\d{9})", contact)
                        if m:
                            phone = m.group()

                    suppliers.append(SupplierInfo(
                        supplier=supplier_name,
                        price=price,
                        unit=row.get("unit", ""),
                        phone=phone,
                        product_title=row.get("name", ""),
                        confidence="high",
                        spec_detail=row.get("base", ""),  # P0-14: 存广材网产品规格串
                    ))

                    # 去重后达到 supplier_count×3 就停（给后面留余量）
                    if len(suppliers) >= supplier_count * 3:
                        break

                # 截断到 supplier_count
                suppliers = suppliers[:supplier_count]

                GldjcSSRPlugin._last_search_ts = time.time()
                GldjcSSRPlugin._daily_count += 1

                # P0-9: 数量不足 supplier_count → 弃用降级
                if len(suppliers) < supplier_count:
                    logger.info(f"[广材网] {keyword}: 匹配{len(suppliers)}家，不足{supplier_count}家，降级AI")
                    return SearchResult.failed(
                        f"广材网匹配结果不足{supplier_count}家（仅{len(suppliers)}家）", keyword=keyword
                    )

                # P0-8: 差异比检查（max/min > PRICE_RATIO_LIMIT → 整组弃用）
                PRICE_RATIO_LIMIT = 3.0
                prices = [s.price for s in suppliers]
                min_p, max_p = min(prices), max(prices)
                if min_p > 0 and max_p / min_p > PRICE_RATIO_LIMIT:
                    logger.info(
                        f"[广材网] {keyword}: 报价差异过大 "
                        f"(max={max_p}/min={min_p}={max_p/min_p:.1f}倍>{PRICE_RATIO_LIMIT})，降级AI"
                    )
                    return SearchResult.failed(
                        f"广材网报价差异过大({max_p}/{min_p}={max_p/min_p:.1f}倍)", keyword=keyword
                    )

                logger.info(f"[广材网] {keyword}: {len(suppliers)}家供应商")
                return SearchResult(
                    source="gldjc_ssr",
                    keyword=keyword,
                    spec=spec,
                    suppliers=suppliers,
                    search_url=url,
                )
            finally:
                await page.close()
        except Exception as e:
            logger.error(f"[广材网] 搜索失败: {e}")
            return SearchResult.failed(f"广材网请求异常: {e}", keyword=keyword)

    @classmethod
    async def close(cls):
        """关闭浏览器"""
        if cls._browser:
            try:
                await cls._browser.close()
            except Exception:
                pass
            cls._browser = None
        if cls._playwright:
            try:
                await cls._playwright.stop()
            except Exception:
                pass
            cls._playwright = None
