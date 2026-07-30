"""
main.py — 智能询价 Agent v5.0 入口

v5.0 核心变化：
- 砍掉 Playwright 爬虫、微信登录、询价豆、管理员后台
- 核心：上传Excel → AI询价（Agnes+DeepSeek）→ 下载报告 → 回填核实
- 新增 /api/status/{id} 轮询接口（替代 SSE）
- 新增 /api/verify 回填核实接口（数据飞轮起点）
"""

import os
import json
import asyncio
import uuid
import webbrowser
from datetime import datetime

from fastapi import FastAPI, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from core.ai_engine import AIEngine
from core.material_parser import (
    get_column_names, generate_template,
    auto_map_columns, ai_guess_columns, get_preview_data,
    TEMPLATE_COLUMNS,
)
from core.report import generate_excel_report
from core.router import SearchResult
from utils.logger import get_logger
from utils.config import PORT
from utils.db import save_task, update_task_status, stop_db_writer

logger = get_logger(__name__)

app = FastAPI(title="智能询价 Agent v5.0")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 静态文件挂载
web_dir = os.path.join(os.path.dirname(__file__), "web")
if os.path.exists(web_dir):
    app.mount("/web", StaticFiles(directory=web_dir), name="web")

# ==================== 内存状态（MVP 简化） ====================
# 上传的文件信息
_uploaded_files: dict = {}
# 询价任务状态：task_id -> {status, total, completed, current, results, cancel_event, config}
_tasks: dict = {}


# ==================== 工具函数 ====================

def _nocache_file(path: str, **kwargs) -> FileResponse:
    """返回带禁用缓存头的 FileResponse"""
    headers = kwargs.pop("headers", {})
    headers.update({
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    })
    return FileResponse(path, headers=headers, **kwargs)


def _parse_mapping(column_mapping: dict) -> dict:
    """列映射转 int"""
    return {
        k: int(v) if v not in (None, "", "null") else None
        for k, v in column_mapping.items()
    }


# ============================================================
# 页面路由
# ============================================================

@app.get("/")
async def index():
    """首页（单页应用）"""
    index_path = os.path.join(web_dir, "index.html")
    if os.path.exists(index_path):
        return _nocache_file(index_path)
    return {"message": "智能询价 Agent v5.0 API"}


@app.get("/result.html")
async def result_page():
    """结果详情页"""
    return _nocache_file(os.path.join(web_dir, "result.html"))


# ============================================================
# 1. 文件上传 + 列映射
# ============================================================

@app.post("/api/upload")
async def upload_excel(file: UploadFile = File(...)):
    """上传 Excel，返回文件 ID、列名、AI自动映射、预览行数"""
    file_id = uuid.uuid4().hex[:12]
    file_path = f"data/temp_{file_id}.xlsx"

    content = await file.read()
    with open(file_path, "wb") as f:
        f.write(content)

    columns = get_column_names(file_path)

    import openpyxl
    wb = openpyxl.load_workbook(file_path)
    row_count = wb.active.max_row - 1
    wb.close()

    # 自动映射
    auto_mapping = auto_map_columns(columns)

    # 如果关键列（name）没匹配到，尝试 AI 识别
    if auto_mapping.get("name") is None:
        try:
            ai_mapping = await ai_guess_columns(columns)
            for k, v in ai_mapping.items():
                if v is not None and auto_mapping.get(k) is None:
                    auto_mapping[k] = v
        except Exception:
            pass

    _uploaded_files[file_id] = {
        "path": file_path,
        "filename": file.filename,
        "columns": columns,
        "row_count": row_count,
    }

    logger.info(f"📁 文件上传: {file.filename} → {file_id} ({row_count} 行)")

    return {
        "file_id": file_id,
        "filename": file.filename,
        "columns": columns,
        "row_count": row_count,
        "auto_mapping": auto_mapping,
        "template_columns": [{"key": t["key"], "label": t["label"]} for t in TEMPLATE_COLUMNS],
    }


@app.post("/api/preview")
async def preview_data(data: dict):
    """根据用户确认的列映射返回预览数据"""
    file_id = data.get("file_id")
    column_mapping = data.get("column_mapping", {})

    if file_id not in _uploaded_files:
        return JSONResponse({"error": "文件未找到"}, status_code=404)

    file_info = _uploaded_files[file_id]
    mapping = _parse_mapping(column_mapping)
    preview_rows = get_preview_data(file_info["path"], mapping)

    return {
        "file_id": file_id,
        "mapping": mapping,
        "total_rows": file_info["row_count"],
        "preview_rows": preview_rows,
    }


# ============================================================
# 2. 开始询价
# ============================================================

@app.post("/api/start")
async def start_inquiry(data: dict):
    """开始询价，返回 task_id

    请求参数：
      file_id, column_mapping, enable_web_search(默认true)
    """
    file_id = data.get("file_id")
    column_mapping = data.get("column_mapping", {})
    enable_web_search = data.get("enable_web_search", True)
    suppliers_per_item = int(data.get("suppliers_per_item", 3))

    if file_id not in _uploaded_files:
        return JSONResponse({"error": "文件未找到"}, status_code=404)

    file_info = _uploaded_files[file_id]
    mapping = _parse_mapping(column_mapping)
    materials = get_preview_data(file_info["path"], mapping, max_rows=9999)

    if not materials:
        return JSONResponse({"error": "未解析到材料数据，请检查列映射"}, status_code=400)

    task_id = uuid.uuid4().hex[:12]
    cancel_event = asyncio.Event()

    # 预估 API 调用次数
    est_agnes = len(materials)
    est_deepseek = len(materials) if enable_web_search else 0

    _tasks[task_id] = {
        "status": "running",
        "total": len(materials),
        "completed": 0,
        "current": "",
        "results": [],
        "cancel_event": cancel_event,
        "enable_web_search": enable_web_search,
        "suppliers_per_item": suppliers_per_item,
        "started_at": datetime.now().isoformat(),
        "logs": [],
    }

    save_task(task_id, len(materials))
    logger.info(
        f"🚀 开始询价: task_id={task_id}, 材料={len(materials)}项, "
        f"每项{suppliers_per_item}家, 联网={enable_web_search}, "
        f"预估调用 Agnes~{est_agnes} DeepSeek~{est_deepseek}"
    )

    # 后台执行
    engine = AIEngine(enable_web_search=enable_web_search, supplier_count=suppliers_per_item)
    asyncio.create_task(_run_inquiry(task_id, engine, materials))

    return {
        "task_id": task_id,
        "material_count": len(materials),
        "suppliers_per_item": suppliers_per_item,
        "estimated_calls": {
            "agnes": est_agnes,
            "deepseek": est_deepseek,
        },
    }


async def _run_inquiry(task_id: str, engine: AIEngine, materials: list):
    """后台执行询价任务"""
    task = _tasks[task_id]
    cancel_event = task["cancel_event"]

    async def progress_cb(event_type: str, data: dict):
        if event_type == "item_start":
            task["current"] = data.get("material", "")
            task["logs"].append({
                "time": datetime.now().strftime("%H:%M:%S"),
                "type": "start",
                "msg": f"🔄 正在询价：{data.get('material', '')} ({data['index']}/{data['total']})",
            })
        elif event_type == "item_done":
            task["completed"] = data["index"]
            success = data.get("success", False)
            source = data.get("source", "")
            cnt = data.get("supplier_count", 0)
            icon = "✅" if success else "⚠️"
            task["logs"].append({
                "time": datetime.now().strftime("%H:%M:%S"),
                "type": "done" if success else "warn",
                "msg": f"{icon} {data.get('material', '')} → {source} {cnt}家" if success
                       else f"{icon} {data.get('material', '')} → 询价失败",
            })
        elif event_type == "all_done":
            task["logs"].append({
                "time": datetime.now().strftime("%H:%M:%S"),
                "type": "done",
                "msg": f"🎉 全部完成：{data.get('completed', 0)}/{data.get('total', 0)} 项",
            })

    try:
        results = await engine.inquiry_batch(
            materials, progress_callback=progress_cb, cancel_event=cancel_event
        )
        task["results"] = results
        task["status"] = "cancelled" if cancel_event.is_set() else "completed"
        task["completed_at"] = datetime.now().isoformat()
        update_task_status(task_id, task["status"], len(results))
        logger.info(f"✅ 询价任务完成: {task_id}, 成功{sum(1 for r in results if r.success)}/{len(results)}")
    except Exception as e:
        logger.error(f"询价任务失败: {task_id} - {e}", exc_info=True)
        task["status"] = "failed"
        task["error"] = str(e)
        task["completed_at"] = datetime.now().isoformat()
        update_task_status(task_id, "failed")


# ============================================================
# 3. 进度查询（前端轮询，替代 SSE）
# ============================================================

@app.get("/api/status/{task_id}")
async def get_status(task_id: str):
    """轮询查询任务进度"""
    if task_id not in _tasks:
        return JSONResponse({"error": "任务未找到"}, status_code=404)

    task = _tasks[task_id]
    return {
        "task_id": task_id,
        "status": task["status"],  # running / completed / cancelled / failed
        "total": task["total"],
        "completed": task["completed"],
        "current": task["current"],
        "logs": task["logs"][-50:],  # 最近50条日志
        "started_at": task.get("started_at"),
        "completed_at": task.get("completed_at"),
    }


# ============================================================
# 4. 取消任务
# ============================================================

@app.post("/api/cancel/{task_id}")
async def cancel_task(task_id: str):
    """取消正在执行的任务，保留已完成结果"""
    if task_id not in _tasks:
        return JSONResponse({"error": "任务未找到"}, status_code=404)

    task = _tasks[task_id]
    if task["status"] != "running":
        return {"status": task["status"], "message": "任务已结束"}

    task["cancel_event"].set()
    task["logs"].append({
        "time": datetime.now().strftime("%H:%M:%S"),
        "type": "warn",
        "msg": "⏹ 用户取消任务，正在保存已完成结果...",
    })
    return {"status": "cancelling", "task_id": task_id}


# ============================================================
# 5. 结果统计
# ============================================================

@app.get("/api/result/{task_id}")
async def get_result(task_id: str):
    """获取询价结果统计"""
    if task_id not in _tasks:
        return JSONResponse({"error": "结果未找到"}, status_code=404)

    task = _tasks[task_id]
    results = task["results"]
    total = len(results)
    success_count = sum(1 for r in results if r.success)

    # 置信度统计
    conf_counts = {"high": 0, "medium": 0, "low": 0}
    source_counts = {}
    source_label = {
        "ai_knowledge": "AI知识推理",
        "ai_websearch": "AI联网搜索",
        "ai_fallback":  "AI知识兜底",
    }
    # 构建明细列表（供前端展示）
    items = []
    for r in results:
        item = {
            "name": r.keyword,
            "spec": r.spec,
            "unit": r.material_unit,
            "source": source_label.get(r.source, r.source),
            "success": r.success,
            "error": r.error_message,
            "suppliers": [],
        }
        for s in (r.suppliers or []):
            sd = s.to_dict() if hasattr(s, "to_dict") else s
            conf = sd.get("confidence", "medium")
            conf_counts[conf] = conf_counts.get(conf, 0) + 1
            item["suppliers"].append({
                "name": sd.get("supplier", ""),
                "price": sd.get("price", 0),
                "unit": sd.get("unit", ""),
                "confidence": conf,
                "phone": sd.get("phone", ""),
            })
        source_counts[r.source] = source_counts.get(r.source, 0) + 1
        items.append(item)

    return {
        "task_id": task_id,
        "status": task["status"],
        "total": total,
        "success_count": success_count,
        "confidence_stats": conf_counts,
        "source_stats": source_counts,
        "items": items,
        "started_at": task.get("started_at"),
        "completed_at": task.get("completed_at"),
    }


# ============================================================
# 6. 下载报告（草稿/正式）
# ============================================================

@app.get("/api/download/{task_id}")
async def download_report(task_id: str, report_type: str = "draft"):
    """下载询价报告

    report_type:
      draft  = AI建议草稿（行情参考价）
      formal = 正式报告（含核实成交价）
    """
    if task_id not in _tasks:
        return JSONResponse({"error": "结果未找到"}, status_code=404)

    task = _tasks[task_id]
    results = task["results"]
    if not results:
        return JSONResponse({"error": "暂无结果，请先完成询价"}, status_code=400)

    # 生成 AI 询价说明
    ai_summary = await _generate_ai_summary(results, task_id)

    output_path = f"data/report_{task_id}_{report_type}.xlsx"
    generate_excel_report(
        results, output_path,
        project_name=f"AI询价建议-{task_id}",
        report_type=report_type,
        ai_summary=ai_summary,
    )

    filename = "AI询价建议草稿.xlsx" if report_type == "draft" else "正式询价报告.xlsx"
    return FileResponse(
        output_path,
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


async def _generate_ai_summary(results: list, task_id: str) -> str:
    """调用 LLM 生成 200 字专业询价说明"""
    from extract.extractor import call_llm

    # 汇总结果
    success_count = sum(1 for r in results if r.success)
    total = len(results)
    conf_counts = {"high": 0, "medium": 0, "low": 0}
    for r in results:
        for s in (r.suppliers or []):
            sd = s.to_dict() if hasattr(s, "to_dict") else s
            conf = sd.get("confidence", "medium")
            conf_counts[conf] = conf_counts.get(conf, 0) + 1

    # 取前5个材料的简况
    sample = []
    for r in results[:5]:
        if r.success and r.suppliers:
            s = r.suppliers[0]
            sd = s.to_dict() if hasattr(s, "to_dict") else s
            sample.append(f"{r.keyword}: ¥{sd.get('price', 0)}/{sd.get('unit', '')}")

    prompt = f"""以下是询价任务 {task_id} 的结果汇总，请以造价工程师的专业视角，写一段200字以内的询价说明。

材料总数：{total} 项
成功询价：{success_count} 项
置信度分布：高 {conf_counts['high']} / 中 {conf_counts['medium']} / 低 {conf_counts['low']}
部分材料价格样例：{', '.join(sample) if sample else '无'}

要求：
1. 说明价格来源（AI知识推理/联网搜索）
2. 提醒价格异常或置信度低的项需人工核实
3. 数据可信度说明
4. 简短建议
只返回说明文字，不要标题和格式标记。"""

    try:
        summary = await call_llm("primary", prompt)
        return (summary or "").strip()[:500]
    except Exception as e:
        logger.warning(f"AI说明生成失败: {e}")
        return f"本次询价共{total}项，成功{success_count}项。置信度：高{conf_counts['high']}/中{conf_counts['medium']}/低{conf_counts['low']}。低置信度项请务必人工核实。"


# ============================================================
# 7. 回填核实结果（数据飞轮起点）
# ============================================================

@app.post("/api/verify")
async def verify_prices(data: dict):
    """回填人工核实的价格

    请求参数：
      task_id, verifications: [
        {material_name, spec, verified_price, verified_note, supplier, phone}
      ]
    """
    task_id = data.get("task_id")
    verifications = data.get("verifications", [])

    if task_id not in _tasks:
        return JSONResponse({"error": "任务未找到"}, status_code=404)

    task = _tasks[task_id]
    results = task["results"]
    updated = 0

    # 构建 核实信息 查找表（按材料名匹配）
    verify_map = {v.get("material_name", "").strip(): v for v in verifications}

    for r in results:
        name = r.keyword.strip()
        if name in verify_map:
            v = verify_map[name]
            r.verified = True
            r.verified_price = v.get("verified_price")
            r.verified_note = v.get("verified_note", "")
            # 更新供应商电话（如提供）
            phone = v.get("phone", "")
            supplier_name = v.get("supplier", "")
            if phone and r.suppliers:
                for s in r.suppliers:
                    if not supplier_name or s.supplier == supplier_name:
                        s.phone = phone
            updated += 1

            # 存入数据库（积累真实成交价）
            try:
                from utils.db import save_price_record
                save_price_record(
                    material_name=name,
                    material_spec=r.spec,
                    material_unit=r.material_unit,
                    supplier=supplier_name or (r.suppliers[0].supplier if r.suppliers else ""),
                    supplier_phone=phone,
                    price=float(v.get("verified_price", 0) or 0),
                    price_unit=r.material_unit,
                    source_platform="verified",
                    confidence="high",
                    task_id=task_id,
                    inquiry_date=datetime.now().strftime("%Y-%m-%d"),
                )
            except Exception as e:
                logger.warning(f"核实价入库失败: {name} - {e}")

    logger.info(f"📝 回填核实: task={task_id}, 更新{updated}项")
    return {
        "success": True,
        "updated": updated,
        "message": f"已回填 {updated} 项核实价格，可下载正式报告",
    }


# ============================================================
# 8. 模板下载
# ============================================================

@app.get("/api/template")
async def download_template():
    """下载材料清单 Excel 模板"""
    template_path = "data/templates/材料清单模板.xlsx"
    os.makedirs(os.path.dirname(template_path), exist_ok=True)
    generate_template(template_path)
    return FileResponse(template_path, filename="材料清单模板.xlsx")


# ============================================================
# 9. 模型配置（本地单机，无需密码）
# ============================================================

@app.get("/api/llm-config")
async def get_llm_config():
    """获取当前 LLM 配置（API Key 脱敏）"""
    from extract.extractor import LLM_CONFIG, LLM_TEMPERATURE, LLM_MAX_TOKENS

    def mask(key):
        if not key:
            return ""
        if len(key) <= 8:
            return "*" * len(key)
        return key[:4] + "*" * (len(key) - 8) + key[-4:]

    return {
        "primary": {
            "api_key": mask(LLM_CONFIG["primary"]["api_key"]),
            "base_url": LLM_CONFIG["primary"]["base_url"],
            "model": LLM_CONFIG["primary"]["model"],
            "has_api_key": bool(LLM_CONFIG["primary"]["api_key"]),
        },
        "fallback": {
            "api_key": mask(LLM_CONFIG["fallback"]["api_key"]),
            "base_url": LLM_CONFIG["fallback"]["base_url"],
            "model": LLM_CONFIG["fallback"]["model"],
            "has_api_key": bool(LLM_CONFIG["fallback"]["api_key"]),
        },
        "temperature": LLM_TEMPERATURE,
        "max_tokens": LLM_MAX_TOKENS,
    }


@app.post("/api/llm-config")
async def save_llm_config(data: dict):
    """保存 LLM 配置到数据库并即时生效

    只有非空字段才更新（空字符串=不修改）
    """
    from utils.db import save_llm_config as db_save
    from extract.extractor import reload_llm_config

    # 字段映射：请求字段 → 数据库 key
    field_map = {
        "primary_api_key":   "llm_primary_api_key",
        "primary_base_url":  "llm_primary_base_url",
        "primary_model":     "llm_primary_model",
        "fallback_api_key":  "llm_fallback_api_key",
        "fallback_base_url": "llm_fallback_base_url",
        "fallback_model":    "llm_fallback_model",
        "temperature":       "llm_temperature",
        "max_tokens":        "llm_max_tokens",
    }

    saved = 0
    skipped_masked = 0
    for field, db_key in field_map.items():
        if field in data and data[field] != "":
            val = str(data[field]).strip()
            # 过滤脱敏值：包含 * 的说明是前端显示的脱敏 Key，不是真实值，跳过
            if "*" in val:
                skipped_masked += 1
                continue
            db_save(db_key, val)
            saved += 1

    # 即时重载
    reload_llm_config()
    logger.info(f"🔧 LLM 配置已更新（{saved} 项，跳过 {skipped_masked} 个脱敏值），即时生效")
    return {
        "success": True, "saved": saved,
        "message": f"配置已保存（{saved} 项）" + (f"，跳过 {skipped_masked} 个未修改项" if skipped_masked else ""),
    }


@app.post("/api/llm-config/test")
async def test_llm_connection(data: dict):
    """测试 LLM 模型连通性

    data: {type: "primary"|"fallback", api_key?, base_url?, model?}
    不传 api_key/base_url/model 时用当前配置
    """
    import time
    import httpx
    from extract.extractor import LLM_CONFIG

    model_type = data.get("type", "primary")
    if model_type not in ("primary", "fallback"):
        return JSONResponse({"error": "type 必须为 primary 或 fallback"}, status_code=400)

    config = LLM_CONFIG[model_type]
    api_key = data.get("api_key") or config["api_key"]
    base_url = (data.get("base_url") or config["base_url"]).rstrip("/")
    model = data.get("model") or config["model"]

    if not api_key:
        return {"success": False, "error": "API Key 未配置"}

    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 5,
                    "temperature": 0,
                },
            )
            elapsed = round((time.time() - start) * 1000)
            if resp.status_code == 200:
                content = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
                logger.info(f"🔗 LLM 连通成功: {model_type}/{model} ({elapsed}ms)")
                return {"success": True, "model": model, "latency_ms": elapsed, "response": content[:50]}
            else:
                err = ""
                try:
                    err = resp.json().get("error", {}).get("message", resp.text[:200])
                except Exception:
                    err = resp.text[:200]
                return {"success": False, "error": f"HTTP {resp.status_code}: {err}", "latency_ms": elapsed}
    except httpx.TimeoutException:
        return {"success": False, "error": "连接超时（15秒）"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/config.html")
async def config_page():
    """模型配置页"""
    return _nocache_file(os.path.join(web_dir, "config.html"))


@app.get("/admin.html")
async def admin_page():
    """管理员入口页"""
    return _nocache_file(os.path.join(web_dir, "admin.html"))


# ============================================================
# 生命周期 + 启动
# ============================================================

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    logger.info("=" * 50)
    logger.info("🚀 智能询价 Agent v5.0 启动中...")

    os.makedirs("data/images", exist_ok=True)
    os.makedirs("data/templates", exist_ok=True)

    logger.info(f"✅ 服务已启动: http://localhost:{PORT}")
    logger.info("=" * 50)

    try:
        webbrowser.open(f"http://localhost:{PORT}")
    except Exception:
        pass

    yield

    logger.info("🛑 正在关闭服务...")
    stop_db_writer()
    logger.info("✅ 服务已关闭")


app.router.lifespan_context = lifespan


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
