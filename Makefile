# 常用任务入口（T3-3：评测不该靠背命令）
# 用法：make eval / make eval-rag / make calibrate / make prompts
PY ?= .venv/bin/python

.PHONY: eval eval-rag calibrate prompts

# 回归 + 困难集，--record 把分数写入 prompts/scores.json（按 prompt 版本归档）
eval:
	$(PY) -m app.evaluation.eval --regression --record
	$(PY) -m app.evaluation.eval 30 --hard --record

# 三域检索评测（每域都会调 LLM 重排，注意额度）
eval-rag:
	$(PY) -m app.evaluation.rag_eval
	$(PY) -m app.evaluation.rag_eval --domain csa
	$(PY) -m app.evaluation.rag_eval --domain credit

# 证据判据标定（T3-5）：纯本地，不调 LLM，秒出
calibrate:
	$(PY) scripts/calibrate_evidence_refs.py

# prompts/ 自检：active 指针、文件、占位符声明是否自洽
prompts:
	$(PY) -c "from app.prompts import validate; print(validate())"
