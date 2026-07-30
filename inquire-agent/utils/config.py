"""
utils/config.py — 配置管理（读取 .env）
"""

import os
from dotenv import load_dotenv

load_dotenv()


def get_env(key: str, default: str = "") -> str:
    """获取环境变量"""
    return os.getenv(key, default)


# 数据库路径
DB_PATH = os.getenv("INQUIRE_DB_PATH", "E:/inquire_db/price_records.db")

# 截图目录
SCREENSHOT_DIR = os.getenv("INQUIRE_SCREENSHOT_DIR", "E:/inquire_db/screenshots")

# 截图保留天数
RETENTION_DAYS = int(os.getenv("INQUIRE_RETENTION_DAYS", "90"))

# 服务端口
PORT = int(os.getenv("INQUIRE_PORT", "8888"))

# 每项材料默认询价供应商数
DEFAULT_SUPPLIER_COUNT = int(os.getenv("INQUIRE_SUPPLIER_COUNT", "3"))

# 最大并发搜索数
MAX_CONCURRENT = int(os.getenv("INQUIRE_MAX_CONCURRENT", "2"))
