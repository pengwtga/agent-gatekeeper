# T6-6 · app/ 目录分包：15 个平铺文件 → 4 个子包

日期：2026-11-13 ｜ 状态：✅ 已完成

## 动机

T0~T3-0 一路加下来，`app/` 平铺了 15 个业务无关文件混在一起：入口、配置、LLM 客户端、两个 Agent、
可观测三件套、评测脚本全部同级，新人（和面试官）打开目录看不到结构。

## 分包方案

原则：**只动目录，不改模块名**——import 路径一眼可推，diff 里 rename 干净，历史文档里的
"改哪个文件"仍然可读。

```
app/
├── main.py            # 入口（路由组装 + lifespan）
├── config.py          # 全局配置（被所有模块依赖，保持顶层）
├── pages_legacy.py    # 旧页面兜底（与 main 同级）
├── core/              # 基础设施：llm_client / budget / embeddings / vector_store
├── agents/            # 业务 Agent：classifier / rag_assistant / evidence
├── observability/     # 可观测：obs(中间件) / metrics / tracing
└── evaluation/        # 评测：dataset / eval / rag_eval
```

每个 `__init__.py` 带包级 docstring（成员清单 + 一句话职责），目录即文档。

依赖方向（无循环）：

```
main → agents → core / observability → config
main → evaluation → agents / core
observability → config（不反向依赖 core）
```

## 迁移时的两个坑（记录给下次）

1. **相对数据路径**：`Path(__file__).parent.parent / "data"` 依赖文件位置，搬进子包后要多一级
   `parent`（dataset.py / eval.py / rag_eval.py / rag_assistant.py 共 5 处）；
2. **三类 import 写法**：模块顶部 import、函数内延迟 import（`llm_client.py` 里 3 处
   `from app.metrics import ...`）、`from app.obs import` 这种带花括号的——批量替换要覆盖全部
   形态，改完用 `grep -vE "app\.(core|agents|observability|evaluation|config)\."` 验证残留为零。

## 验收（全部通过）

```bash
.venv/bin/python -m app.evaluation.eval 30 --rule    # 30/30，unknown 判定行正常输出
.venv/bin/python -m app.evaluation.rag_eval          # 常规 100% / 困难 90%（与分包前一致）
.venv/bin/python -c "from app.main import app"       # 15 条路由
uvicorn app.main:app --port 8012                     # 启动 <1s
POST /classify                                       # 200，LLM 命中，
GET  /metrics                                        # classification 计数正确记录
```

命令行入口变化：`python -m app.eval` → `python -m app.evaluation.eval`，
`python -m app.rag_eval` → `python -m app.evaluation.rag_eval`（README 已同步）。
