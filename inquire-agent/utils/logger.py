"""
utils/logger.py — 结构化日志配置
"""

import logging
import os
from logging.handlers import TimedRotatingFileHandler

LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-20s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def get_logger(name: str = "inquire_agent") -> logging.Logger:
    """获取配置好的 logger 实例"""
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    # 控制台输出（INFO 及以上）
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    logger.addHandler(console)

    # 文件输出（DEBUG 及以上，按日轮转，保留 30 天）
    file_handler = TimedRotatingFileHandler(
        filename=os.path.join(LOG_DIR, "app.log"),
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
        delay=True,
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    file_handler.suffix = "%Y-%m-%d"
    logger.addHandler(file_handler)

    return logger
