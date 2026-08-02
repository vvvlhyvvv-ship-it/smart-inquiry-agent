"""
utils/login_check.py — 登录态双保险校验（v5.2，审查修订版）
借鉴：material-price-audit/login_gate.py
修订：P1-4 改 httpx 异步（避免阻塞事件循环）；调用时机=启动时 + 每次询价任务开始前
"""

import httpx

# 页面正向文案（登录后才会出现的字样）
POSITIVE_HINTS = {
    "gldjc": ("退出登录", "退出", "会员中心", "我的广材", "个人中心", "账户中心", "充值"),
    "yzw": ("退出登录", "工作台", "会员权益", "我的消息"),
}

# 硬登录路径（跳到这里 = 未登录）
HARD_LOGIN_URLS = ("/login", "/signin", "/passport", "/sso", "ucenter")

# 各平台的 token cookie 名（用于 Cookie 证据判断）
PLATFORM_TOKEN_KEYS = {
    "gldjc": ("token", "tokensx", "session", "sid", "sso_token"),
    "yzw": ("yzw-auac-token", "web.auth.yzw", "token", "session", "sid"),
}

# 落地页（用于登录态检测）
# 广材网：搜索页，未登录会302到/login，登录后返回200
# 云筑网：AI助手首页
CHECK_URLS = {
    "gldjc": "https://www.gldjc.com/scj/so.html?l=1&keyword=%E9%98%80%E9%97%A8",
    "yzw": "https://ai.yzw.cn/",
}


async def check_login_dual(platform: str, cookies: dict, check_url: str) -> bool:
    """
    双保险登录态判断：
    1. 有 Cookie 证据（token/session 存在）
    2. 访问落地页（不是登录页），页面含正向文案
    调用时机：服务启动时 + 每次询价任务开始前（不在逐条询价循环内）
    """
    # ① Cookie 证据
    if not cookies:
        return False
    token_keys = PLATFORM_TOKEN_KEYS.get(platform, ("token", "session", "sid"))
    has_token = any(k.lower() in token_keys for k in cookies)
    if not has_token:
        return False

    # ② 页面证据：请求落地页，检查是否被重定向到登录页 + 是否有正向文案
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
        }
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(check_url, headers=headers)
        if resp.status_code != 200:
            return False
        # 被重定向到登录页 → 未登录
        if any(h in str(resp.url).lower() for h in HARD_LOGIN_URLS):
            return False
        # 页面正向文案（SSR页面可能没有，作为加分项）
        hints = POSITIVE_HINTS.get(platform, ())
        if any(h in resp.text for h in hints):
            return True
        # 文案缺失时，Cookie 证据 + 未跳登录页 + 有token值 → 判定已登录
        # （广材网SSR页面文案在JS渲染后才有，HTML源码里可能没有）
        token_val = ""
        for tk in token_keys:
            v = cookies.get(tk, "")
            if v and v.strip():
                token_val = v
                break
        if token_val:
            return True
        return False
    except Exception:
        return False
