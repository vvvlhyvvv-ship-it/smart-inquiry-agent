# 智能询价 Agent v5.0

> AI知识推理 + 联网搜索 · 造价工程师的AI询价决策助手
> 输出含置信度标注的比价报告（草稿/正式双层输出）

## v5.0 核心变化

- **砍掉爬虫**：不再用 Playwright 爬 1688/钢铁网/广材网
- **AI询价**：Agnes 2.0 Flash（免费主力）+ DeepSeek（联网搜索备用）
- **双层输出**：草稿报告（行情参考价）/ 正式报告（含核实成交价）
- **数据飞轮**：人工核实回填 → 积累真实成交价 → 下次更准

## 快速开始

### 1. 环境准备

```bash
pip install -r requirements.txt
```

### 2. 配置 API Key

```bash
cp .env.example .env
# 编辑 .env，填入 Agnes 和 DeepSeek 的 API Key
```

### 3. 启动服务

```bash
python main.py
# 自动打开浏览器 → http://localhost:8888
```

### 4. 使用流程

1. 下载 Excel 模板，填写材料清单（材料名称、规格、单位）
2. 上传 Excel → 确认列映射 → 勾选「含联网实时价」
3. 点击「开始询价」→ 实时查看进度
4. 下载草稿报告（行情参考价）
5. 电话核实后回填 → 下载正式报告（成交价）

## 项目结构

```
inquire-agent/
├── main.py              # FastAPI 入口（v5.0 精简版）
├── requirements.txt     # 依赖（去 Playwright/Pillow）
├── .env.example         # API Key 模板
├── core/
│   ├── ai_engine.py     # 🆕 AI询价核心引擎（三档降级链）
│   ├── material_parser.py # Excel 解析 + 列映射
│   ├── report.py        # 报告生成（草稿/正式双层）
│   └── router.py        # 数据类（SearchResult/SupplierInfo）
├── extract/
│   └── extractor.py     # LLM 调用封装（OpenAI 兼容）
├── utils/
│   ├── config.py        # .env 配置
│   ├── db.py            # SQLite WAL 模式
│   └── logger.py        # 日志
├── web/
│   ├── index.html       # 单页应用（上传→询价→核实）
│   └── result.html      # 结果详情页
├── plugins/
│   └── archive/         # 广材网爬虫代码归档（MVP不暴露）
└── data/
    └── templates/       # Excel 模板
```

## AI 询价三档降级链

```
① Agnes 2.0 Flash 知识推理（免费主力）→ confidence=medium
   ↓ 失败/用户开启联网
② DeepSeek 联网搜索（付费）→ confidence=high
   ↓ 联网失败
③ DeepSeek 知识兜底（无联网）→ confidence=low
```

## 置信度评定

| 置信度 | 触发条件 | 报告标注 |
|--------|---------|---------|
| high | DeepSeek 联网搜索成功，有来源URL | 绿色 |
| medium | Agnes 知识推理成功（无联网） | 黄色 |
| low | DeepSeek 联网失败，知识兜底 | 红色 |

## 数据库

- 路径：`E:\inquire_db\price_records.db`（SQLite WAL 模式）
- 询价记录：每次询价自动入库
- 核实价格：人工核实后回填，积累真实成交价

## 与 v4.0 的差异

| 维度 | v4.0 | v5.0 |
|------|------|------|
| 价格来源 | 爬1688/钢铁网/广材网 | Agnes + DeepSeek 联网搜索 |
| 维护成本 | 高（反爬对抗） | 低（纯API调用） |
| 代码量 | 17个py + 6个html | 6个py + 2个html |
| 价格可信度 | 挂牌价（不可审计） | 草稿(参考价)/正式(成交价)双层 |
| 数据飞轮 | 无 | 人工核实回填积累 |

详见：`D:\obsidian文件\创新成果\项目变更-智能询价.md`
