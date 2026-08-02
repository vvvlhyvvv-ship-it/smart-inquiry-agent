"""
extract/extractor.py — LLM 调用封装（V5.2 精简版）

v5.0 仅保留 call_llm（OpenAI 兼容接口）。
v4.0 的 extract_prices / call_llm_vision 等爬虫相关函数已删除。

支持运行时动态重载配置（从 SQLite 读取，优先级高于 .env）。
"""

import json
import os
from typing import Optional
import httpx
from dotenv import load_dotenv
from utils.logger import get_logger

load_dotenv()
logger = get_logger(__name__)

# 默认配置（从 .env 读取，作为兜底）
_DEFAULT_PRIMARY = {
    "base_url": os.getenv("AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1"),
    "model": os.getenv("AGNES_MODEL", "agnes-2.0-flash"),
    "api_key": os.getenv("AGNES_API_KEY", ""),
}
_DEFAULT_FALLBACK = {
    "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
    "model": os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
    "api_key": os.getenv("DEEPSEEK_API_KEY", ""),
}
_DEFAULT_TEMPERATURE = 0.1
_DEFAULT_MAX_TOKENS = 4000  # P0-10①: 2000→4000，推理模型thinking+text总消耗更高

# 运行时配置（可被 reload_llm_config() 更新）
LLM_CONFIG = {
    "primary": dict(_DEFAULT_PRIMARY),
    "fallback": dict(_DEFAULT_FALLBACK),
}
LLM_TEMPERATURE = _DEFAULT_TEMPERATURE
LLM_MAX_TOKENS = _DEFAULT_MAX_TOKENS

_client: Optional[httpx.AsyncClient] = None


def reload_llm_config():
    """从 SQLite 数据库重新加载 LLM 配置。数据库有值 → 用数据库值；否则回退 .env。"""
    global LLM_CONFIG, LLM_TEMPERATURE, LLM_MAX_TOKENS
    try:
        from utils.db import get_all_llm_config
        db_config = get_all_llm_config()
    except Exception as e:
        logger.warning(f"无法读取数据库 LLM 配置: {e}，使用 .env 默认值")
        db_config = {}

    KEY_MAP = {
        "llm_primary_api_key":   ("primary", "api_key"),
        "llm_primary_base_url":  ("primary", "base_url"),
        "llm_primary_model":     ("primary", "model"),
        "llm_fallback_api_key":  ("fallback", "api_key"),
        "llm_fallback_base_url": ("fallback", "base_url"),
        "llm_fallback_model":    ("fallback", "model"),
    }
    LLM_CONFIG["primary"] = dict(_DEFAULT_PRIMARY)
    LLM_CONFIG["fallback"] = dict(_DEFAULT_FALLBACK)
    LLM_TEMPERATURE = _DEFAULT_TEMPERATURE
    LLM_MAX_TOKENS = _DEFAULT_MAX_TOKENS

    for db_key, (section, field) in KEY_MAP.items():
        if db_key in db_config and db_config[db_key]:
            LLM_CONFIG[section][field] = db_config[db_key]

    if "llm_temperature" in db_config and db_config["llm_temperature"]:
        try:
            LLM_TEMPERATURE = float(db_config["llm_temperature"])
        except (TypeError, ValueError):
            pass
    if "llm_max_tokens" in db_config and db_config["llm_max_tokens"]:
        try:
            LLM_MAX_TOKENS = int(db_config["llm_max_tokens"])
        except (TypeError, ValueError):
            pass

    logger.info("LLM 配置已重载（数据库优先，.env 兜底）")


# 启动时从数据库加载配置（数据库优先，.env 兜底）
try:
    reload_llm_config()
except Exception as e:
    logger.warning(f"启动时加载 LLM 配置失败，使用 .env 默认值: {e}")


async def _get_client() -> httpx.AsyncClient:
    """获取复用的 httpx AsyncClient"""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
    return _client


async def call_llm(provider: str, prompt: str, system_prompt: str = "") -> Optional[str]:
    """
    异步调用 LLM API（OpenAI 兼容接口）

    provider: "primary" (Agnes) 或 "fallback" (DeepSeek)
    返回: 模型回复文本，失败返回 None
    """
    config = LLM_CONFIG[provider]
    if not config["api_key"]:
        logger.warning(f"LLM ({provider}) API Key 未配置，请在 .env 文件中设置")
        return None

    client = await _get_client()
    try:
        resp = await client.post(
            f"{config['base_url']}/chat/completions",
            headers={
                "Authorization": f"Bearer {config['api_key']}",
                "Content-Type": "application/json",
            },
            json={
                "model": config["model"],
                "messages": [
                    {
                        "role": "system",
                        "content": system_prompt or "你是一个精确的数据提取助手。只返回JSON。",
                    },
                    {"role": "user", "content": prompt},
                ],
                "temperature": LLM_TEMPERATURE,
                "max_tokens": LLM_MAX_TOKENS,
            },
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        # P1-10: API成功但返回空字符串时也打日志
        if not content or not content.strip():
            logger.warning(f"LLM ({provider}) 返回了空字符串（HTTP {resp.status_code}）")
            return None
        return content
    except httpx.HTTPStatusError as e:
        # P0-2: 记录具体HTTP错误（限流/超时/key失效）
        status = e.response.status_code
        body = ""
        try:
            body = e.response.text[:200]
        except Exception:
            pass
        logger.error(f"LLM ({provider}) HTTP {status}: {body}")
        return None
    except httpx.TimeoutException:
        logger.error(f"LLM ({provider}) 请求超时")
        return None
    except Exception as e:
        logger.error(f"LLM ({provider}) 调用失败: {type(e).__name__}: {e}")
        return None
