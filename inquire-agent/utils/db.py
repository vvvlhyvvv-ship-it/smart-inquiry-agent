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

-- v5.1 大平台接入改造：项目表
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT DEFAULT '',
    region TEXT DEFAULT '',
    budget REAL DEFAULT 0,
    description TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

# v5.1 大平台接入改造：price_records 表新增字段（ALTER TABLE，旧数据自动补空值）
_ALTER_SQL = [
    "ALTER TABLE price_records ADD COLUMN project_id TEXT REFERENCES projects(id)",
    "ALTER TABLE price_records ADD COLUMN project_type TEXT DEFAULT ''",
    "ALTER TABLE price_records ADD COLUMN region TEXT DEFAULT ''",
    "ALTER TABLE price_records ADD COLUMN procurement_type TEXT DEFAULT ''",
]

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
    # v5.1 大平台接入改造：为 price_records 表追加字段（已存在则忽略）
    for sql in _ALTER_SQL:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass  # 列已存在，跳过
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
         confidence, is_anomaly, task_id, inquiry_date,
         project_id, project_type, region, procurement_type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
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
        kwargs.get("project_id", ""),
        kwargs.get("project_type", ""),
        kwargs.get("region", ""),
        kwargs.get("procurement_type", ""),
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


# ==================== v5.1 大平台接入：项目 + 数据导出 ====================

def save_project(project_id: str, name: str, ptype: str = "", region: str = "",
                 budget: float = 0, description: str = ""):
    """保存项目信息（通过写入队列）"""
    sql = """INSERT OR REPLACE INTO projects (id, name, type, region, budget, description, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)"""
    _write_queue.put((sql, (project_id, name, ptype, region, budget, description)))


def get_project(project_id: str) -> dict:
    """获取项目信息"""
    _ensure_db_dir()
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        row = conn.execute(
            "SELECT id, name, type, region, budget, description FROM projects WHERE id = ?",
            (project_id,)
        ).fetchone()
        if row:
            return {"id": row[0], "name": row[1], "type": row[2], "region": row[3],
                    "budget": row[4], "description": row[5]}
        return None
    finally:
        conn.close()


def query_price_records(material_name: str = None, region: str = None,
                        project_type: str = None, start_date: str = None,
                        end_date: str = None, limit: int = 100) -> list:
    """
    查询价格记录（供大平台价格审查模块用）
    支持按材料名/地区/项目类型/日期范围筛选
    """
    _ensure_db_dir()
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        sql = """SELECT material_name, material_spec, material_unit, supplier, supplier_phone,
                 price, price_unit, source_platform, confidence, inquiry_date,
                 project_id, project_type, region, procurement_type
                 FROM price_records WHERE 1=1"""
        params = []
        if material_name:
            sql += " AND material_name LIKE ?"
            params.append(f"%{material_name}%")
        if region:
            sql += " AND region = ?"
            params.append(region)
        if project_type:
            sql += " AND project_type = ?"
            params.append(project_type)
        if start_date:
            sql += " AND inquiry_date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND inquiry_date <= ?"
            params.append(end_date)
        sql += " ORDER BY inquiry_date DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [
            {"material_name": r[0], "material_spec": r[1], "material_unit": r[2],
             "supplier": r[3], "supplier_phone": r[4], "price": r[5], "price_unit": r[6],
             "source_platform": r[7], "confidence": r[8], "inquiry_date": r[9],
             "project_id": r[10], "project_type": r[11], "region": r[12], "procurement_type": r[13]}
            for r in rows
        ]
    finally:
        conn.close()


def list_projects() -> list:
    """列出所有项目"""
    _ensure_db_dir()
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        rows = conn.execute(
            "SELECT id, name, type, region, budget FROM projects ORDER BY updated_at DESC"
        ).fetchall()
        return [{"id": r[0], "name": r[1], "type": r[2], "region": r[3], "budget": r[4]} for r in rows]
    finally:
        conn.close()


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
