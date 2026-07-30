"""
utils/db.py — SQLite 数据库操作（WAL 模式 + 写入队列）
"""

import sqlite3
import threading
import queue
import os
from utils.logger import get_logger

logger = get_logger(__name__)

DB_PATH = "E:/inquire_db/price_records.db"

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS price_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    material_name TEXT NOT NULL,
    material_spec TEXT DEFAULT '',
    material_unit TEXT DEFAULT '',
    supplier TEXT NOT NULL,
    supplier_phone TEXT DEFAULT '',
    price REAL NOT NULL,
    price_unit TEXT DEFAULT '',
    source_platform TEXT NOT NULL,
    search_url TEXT DEFAULT '',
    screenshot_path TEXT DEFAULT '',
    confidence TEXT DEFAULT 'medium',
    is_anomaly INTEGER DEFAULT 0,
    task_id TEXT NOT NULL,
    inquiry_date DATE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(material_name, supplier, inquiry_date)
);

CREATE TABLE IF NOT EXISTS inquiry_tasks (
    task_id TEXT PRIMARY KEY,
    user_id INTEGER DEFAULT 1,
    material_count INTEGER,
    completed_count INTEGER DEFAULT 0,
    total_beans INTEGER,
    status TEXT DEFAULT 'running',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS material_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    standard_name TEXT NOT NULL,
    alias TEXT NOT NULL,
    UNIQUE(standard_name, alias)
);

CREATE TABLE IF NOT EXISTS llm_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    nickname TEXT DEFAULT '微信用户',
    avatar TEXT DEFAULT '',
    beans INTEGER DEFAULT 100,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS beans_pricing (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_name TEXT NOT NULL,
    bean_count INTEGER NOT NULL,
    price_cents INTEGER NOT NULL,
    is_active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS bean_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    amount INTEGER NOT NULL,
    task_id TEXT DEFAULT '',
    description TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

_write_queue = queue.Queue()
_conn_lock = threading.Lock()
_initialized = False


def _ensure_db_dir():
    """确保数据库目录存在"""
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)


def _init_db():
    """初始化数据库连接"""
    _ensure_db_dir()
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


def _db_writer_worker():
    """后台线程：串行执行所有写操作"""
    conn = _init_db()
    while True:
        item = _write_queue.get()
        if item is None:
            break
        sql, params = item
        try:
            with _conn_lock:
                conn.execute(sql, params)
                conn.commit()
        except Exception as e:
            logger.error(f"数据库写入失败: {e}")
    conn.close()


_writer_thread = threading.Thread(target=_db_writer_worker, daemon=True)
_writer_thread.start()


def save_price_record(**kwargs):
    """将价格记录加入写入队列（非阻塞）"""
    sql = """INSERT OR REPLACE INTO price_records
        (material_name, material_spec, material_unit, supplier, supplier_phone,
         price, price_unit, source_platform, search_url, screenshot_path,
         confidence, is_anomaly, task_id, inquiry_date)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
    params = (
        kwargs["material_name"],
        kwargs.get("material_spec", ""),
        kwargs.get("material_unit", ""),
        kwargs["supplier"],
        kwargs.get("supplier_phone", ""),
        kwargs["price"],
        kwargs.get("price_unit", ""),
        kwargs["source_platform"],
        kwargs.get("search_url", ""),
        kwargs.get("screenshot_path", ""),
        kwargs.get("confidence", "medium"),
        kwargs.get("is_anomaly", 0),
        kwargs["task_id"],
        kwargs["inquiry_date"],
    )
    _write_queue.put((sql, params))


def save_task(task_id: str, material_count: int, total_beans: int = 0):
    """创建询价任务记录"""
    sql = """INSERT OR REPLACE INTO inquiry_tasks
        (task_id, material_count, total_beans, status)
        VALUES (?, ?, ?, 'running')"""
    _write_queue.put((sql, (task_id, material_count, total_beans)))


def update_task_status(task_id: str, status: str, completed_count: int = None):
    """更新任务状态"""
    if completed_count is not None:
        sql = """UPDATE inquiry_tasks
            SET status = ?, completed_count = ?, completed_at = CURRENT_TIMESTAMP
            WHERE task_id = ?"""
        _write_queue.put((sql, (status, completed_count, task_id)))
    else:
        sql = """UPDATE inquiry_tasks SET status = ? WHERE task_id = ?"""
        _write_queue.put((sql, (status, task_id)))


def get_all_llm_config() -> dict:
    """直接读取所有 LLM 配置（不走写入队列，确保即时读取）"""
    _ensure_db_dir()
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        rows = conn.execute("SELECT key, value FROM llm_config").fetchall()
        return {row[0]: row[1] for row in rows}
    finally:
        conn.close()


def save_llm_config(key: str, value: str):
    """保存单个 LLM 配置项（通过写入队列异步执行）"""
    sql = "INSERT OR REPLACE INTO llm_config (key, value) VALUES (?, ?)"
    _write_queue.put((sql, (key, value)))


def stop_db_writer():
    """优雅关闭写入线程"""
    _write_queue.put(None)
    _writer_thread.join(timeout=5)


# ==================== 用户操作 ====================

def get_user(user_id: str) -> dict:
    """获取用户信息，不存在则创建默认用户"""
    _ensure_db_dir()
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        row = conn.execute("SELECT id, nickname, avatar, beans FROM users WHERE id = ?", (user_id,)).fetchone()
        if row:
            return {"id": row[0], "nickname": row[1], "avatar": row[2], "beans": row[3]}
        # 新用户：创建并赠送100豆
        conn.execute("INSERT INTO users (id, nickname, beans) VALUES (?, '微信用户', 100)", (user_id,))
        conn.commit()
        return {"id": user_id, "nickname": "微信用户", "avatar": "", "beans": 100}
    finally:
        conn.close()


def update_user(user_id: str, nickname: str = None, avatar: str = None):
    """更新用户昵称/头像"""
    _ensure_db_dir()
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        if nickname:
            conn.execute("UPDATE users SET nickname = ? WHERE id = ?", (nickname, user_id))
        if avatar:
            conn.execute("UPDATE users SET avatar = ? WHERE id = ?", (avatar, user_id))
        conn.commit()
    finally:
        conn.close()


def deduct_beans(user_id: str, amount: int, task_id: str = "", description: str = "") -> bool:
    """扣减询价豆，返回是否成功"""
    _ensure_db_dir()
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        cur = conn.execute("SELECT beans FROM users WHERE id = ?", (user_id,))
        row = cur.fetchone()
        if not row or row[0] < amount:
            return False
        conn.execute("UPDATE users SET beans = beans - ? WHERE id = ?", (amount, user_id))
        conn.execute(
            "INSERT INTO bean_transactions (user_id, amount, task_id, description) VALUES (?, ?, ?, ?)",
            (user_id, -amount, task_id, description),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def add_beans(user_id: str, amount: int, description: str = "") -> int:
    """充值询价豆，返回新余额"""
    _ensure_db_dir()
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.execute("UPDATE users SET beans = beans + ? WHERE id = ?", (amount, user_id))
        conn.execute(
            "INSERT INTO bean_transactions (user_id, amount, description) VALUES (?, ?, ?)",
            (user_id, amount, description),
        )
        conn.commit()
        row = conn.execute("SELECT beans FROM users WHERE id = ?", (user_id,)).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


# ==================== 询价豆定价 ====================

def get_beans_pricing() -> list:
    """获取所有激活的定价方案"""
    _ensure_db_dir()
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        rows = conn.execute(
            "SELECT id, package_name, bean_count, price_cents FROM beans_pricing WHERE is_active = 1 ORDER BY bean_count"
        ).fetchall()
        return [{"id": r[0], "name": r[1], "beans": r[2], "price_cents": r[3]} for r in rows]
    finally:
        conn.close()


def save_beans_pricing(packages: list):
    """全量替换定价方案（管理员操作）"""
    _ensure_db_dir()
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.execute("DELETE FROM beans_pricing")
        for pkg in packages:
            conn.execute(
                "INSERT INTO beans_pricing (package_name, bean_count, price_cents) VALUES (?, ?, ?)",
                (pkg["name"], pkg["beans"], pkg["price_cents"]),
            )
        conn.commit()
    finally:
        conn.close()


def init_default_pricing():
    """初始化默认定价方案"""
    existing = get_beans_pricing()
    if not existing:
        save_beans_pricing([
            {"name": "100询价豆", "beans": 100, "price_cents": 990},
            {"name": "500询价豆", "beans": 500, "price_cents": 3990},
            {"name": "1000询价豆", "beans": 1000, "price_cents": 6990},
        ])
        logger.info("💰 默认询价豆定价已初始化")
