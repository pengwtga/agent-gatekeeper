"""困难测试集 — 换表述 / 未见错误码 / 混合信号 / 口语化描述

设计原则：每条样本的人工标注都清晰无歧义，但**故意绕开规则基线的关键字与特征**：
- 不出现规则依赖的关键字（如 approved / duplicate / EA_TIMEOUT）
- 使用未见过的错误码（GATEWAY_504、ENUM_INVALID、JOB_STUCK...）
- 混入干扰信号（downstream 字段指向非根因系统）
- 口语化/描述式 message（模拟真实工单里人写的话）

这是检验 LLM 泛化能力、暴露规则基线天花板的测试集。

用法：
    python scripts/gen_hard_dataset.py
输出：
    data/incidents_hard.jsonl
"""

import json
from pathlib import Path

OUT_FILE = Path(__file__).parent.parent / "data" / "ssrm" / "incidents_hard.jsonl"

# (label, incident) —— 全部人工编写
SAMPLES = [
    # ── xflow_callback_lost：不出现 approved/回调丢失等规则关键字 ──
    ("xflow_callback_lost", {
        "stage": "register", "source_system": "portal", "error_code": "JOB_STUCK",
        "error_message": "用户反馈提交后页面一直显示审批中，但后台查审批记录其实早已通过",
        "wait_minutes": 95, "retry_count": 0, "downstream": "ssrm", "http_status": None,
    }),
    ("xflow_callback_lost", {
        "stage": "register", "source_system": "ssrm", "error_code": "TASK_NOT_CREATED",
        "error_message": "对账脚本发现工单 W-88213 状态=FINISHED 但没有生成注册任务，疑似通知没打过来",
        "wait_minutes": 120, "retry_count": 0, "downstream": "ssrm", "http_status": None,
    }),
    ("xflow_callback_lost", {
        "stage": "change", "source_system": "xflow", "error_code": None,
        "error_message": "审批人在 xFlow 里点完同意了，系统这边却迟迟没有动静",
        "wait_minutes": 60, "retry_count": 0, "downstream": "ssrm", "http_status": None,
    }),
    ("xflow_callback_lost", {
        "stage": "register", "source_system": "ssrm", "error_code": None,
        "error_message": "夜里巡检发现 3 笔审批已终态但 SSRM 无任务记录，事件通知通道告警为空",
        "wait_minutes": 600, "retry_count": 0, "downstream": "ssrm", "http_status": None,
    }),
    ("xflow_callback_lost", {
        "stage": "register", "source_system": "ssrm", "error_code": None,
        "error_message": "审批已经同意，但供应商状态一直是待审核，查 SSRM 日志没有任何回调到达的记录",
        "wait_minutes": 80, "retry_count": 0, "downstream": "ssrm", "http_status": None,
    }),
    # ── ea_sync_timeout：换词（上游无响应/连接重置/发布窗口），downstream 不总写 ea ──
    ("ea_sync_timeout", {
        "stage": "change", "source_system": "ssrm", "error_code": "SYNC_FAIL_99",
        "error_message": "供应商主数据推送失败，上游服务无响应", "wait_minutes": 5,
        "retry_count": 3, "downstream": "master-data", "http_status": None,
    }),
    ("ea_sync_timeout", {
        "stage": "register", "source_system": "ssrm", "error_code": "CONN_RESET",
        "error_message": "调用主数据平台创建供应商时连接被重置", "wait_minutes": 2,
        "retry_count": 2, "downstream": "mdm-platform", "http_status": None,
    }),
    ("ea_sync_timeout", {
        "stage": "register", "source_system": "ssrm", "error_code": "GATEWAY_504",
        "error_message": "主数据平台接口网关超时，第 4 次重试仍失败", "wait_minutes": 8,
        "retry_count": 4, "downstream": "ea", "http_status": 504,
    }),
    ("ea_sync_timeout", {
        "stage": "change", "source_system": "ssrm", "error_code": None,
        "error_message": "EA 发布窗口期间所有写入请求被拒，返回 503", "wait_minutes": 12,
        "retry_count": 3, "downstream": "ea", "http_status": 503,
    }),
    ("ea_sync_timeout", {
        "stage": "register", "source_system": "ssrm", "error_code": "E50402",
        "error_message": "sync to vendor master failed: i/o timeout after 5000ms", "wait_minutes": 3,
        "retry_count": 3, "downstream": "ea", "http_status": None,
    }),
    # ── duplicate_submit：换词（提交了两次/重复消息/already processed）──
    ("duplicate_submit", {
        "stage": "register", "source_system": "dora", "error_code": None,
        "error_message": "同一供应商 10 秒内提交了两次注册，第二次提示申请已存在", "wait_minutes": 0,
        "retry_count": 0, "downstream": "ssrm", "http_status": 409,
    }),
    ("duplicate_submit", {
        "stage": "register", "source_system": "mq", "error_code": None,
        "error_message": "消费到重复消息，correlation_id 相同的注册事件已在处理中", "wait_minutes": 0,
        "retry_count": 1, "downstream": "ssrm", "http_status": None,
    }),
    ("duplicate_submit", {
        "stage": "register", "source_system": "kafka", "error_code": None,
        "error_message": "repeated kafka delivery: this register event was already processed", "wait_minutes": 0,
        "retry_count": 2, "downstream": "ssrm", "http_status": None,
    }),
    ("duplicate_submit", {
        "stage": "change", "source_system": "dora", "error_code": "REQ_REPLAYED",
        "error_message": "检测到变更请求重放：request_id 与 5 分钟前的成功请求一致", "wait_minutes": 0,
        "retry_count": 0, "downstream": "ssrm", "http_status": 409,
    }),
    # ── data_inconsistency：描述缺口而非关键字 ──
    ("data_inconsistency", {
        "stage": "register", "source_system": "reconcile", "error_code": None,
        "error_message": "SSRM 显示准入完成，但 Dora 里查不到该公司联系人，EA 数据正常", "wait_minutes": 720,
        "retry_count": 0, "downstream": "dora", "http_status": None,
    }),
    ("data_inconsistency", {
        "stage": "change", "source_system": "reconcile", "error_code": None,
        "error_message": "EA 侧已有该供应商最新信息，供应商门户 Dora 却搜不到", "wait_minutes": 800,
        "retry_count": 0, "downstream": "dora", "http_status": None,
    }),
    ("data_inconsistency", {
        "stage": "register", "source_system": "reconcile", "error_code": "FIELD_EMPTY",
        "error_message": "对比快照：23 家供应商的联系人字段在门户侧为空", "wait_minutes": 1000,
        "retry_count": 0, "downstream": "dora", "http_status": None,
    }),
    ("data_inconsistency", {
        "stage": "change", "source_system": "warehouse", "error_code": None,
        "error_message": "变更后仓库侧显示旧数据，疑似同步中断导致部分下游未更新", "wait_minutes": 300,
        "retry_count": 0, "downstream": "ea", "http_status": None,
    }),
    ("data_inconsistency", {
        "stage": "register", "source_system": "reconcile", "error_code": None,
        "error_message": "reconciliation report: 5 vendors have empty contact info on portal, master data OK",
        "wait_minutes": 900, "retry_count": 0, "downstream": "dora", "http_status": None,
    }),
    # ── frontend_param_error：不一定带 PARAM 错误码、downstream 不总写 frontend ──
    ("frontend_param_error", {
        "stage": "register", "source_system": "dora", "error_code": None,
        "error_message": "注册页提交的营业执照编号是图片路径而不是数字，后端无法解析", "wait_minutes": 0,
        "retry_count": 0, "downstream": "frontend", "http_status": 400,
    }),
    ("frontend_param_error", {
        "stage": "register", "source_system": "dora", "error_code": None,
        "error_message": "Dora 注册表单少传了 site_code，SSRM 校验不通过", "wait_minutes": 0,
        "retry_count": 0, "downstream": "frontend", "http_status": 400,
    }),
    ("frontend_param_error", {
        "stage": "change", "source_system": "dora", "error_code": "ENUM_INVALID",
        "error_message": "调用方把供应商类型传成了中文名，枚举校验失败", "wait_minutes": 0,
        "retry_count": 0, "downstream": "ssrm", "http_status": 422,
    }),
    ("frontend_param_error", {
        "stage": "register", "source_system": "ssrm", "error_code": None,
        "error_message": "400 Bad Request: supplier_type value '服务商' not in enum [AGENT,VENDOR]",
        "wait_minutes": 0, "retry_count": 0, "downstream": "ssrm", "http_status": 400,
    }),
    # ── 混合干扰样本（label 仍明确，但表面特征误导）──
    ("ea_sync_timeout", {
        "stage": "register", "source_system": "ssrm", "error_code": "WRITE_REJECTED",
        "error_message": "向主数据平台写入供应商时被对端拒绝，连接可以建立但请求无响应", "wait_minutes": 6,
        "retry_count": 3, "downstream": "ea", "http_status": None,
    }),
    ("duplicate_submit", {
        "stage": "register", "source_system": "ssrm", "error_code": "CONCURRENT_OP",
        "error_message": "同一注册单被两个操作员同时处理，后到的提交被拒绝", "wait_minutes": 0,
        "retry_count": 0, "downstream": "ssrm", "http_status": 409,
    }),
]


def main():
    with OUT_FILE.open("w", encoding="utf-8") as f:
        for idx, (label, incident) in enumerate(SAMPLES):
            incident = dict(incident)
            incident["incident_id"] = f"HARD-{idx:03d}"
            f.write(json.dumps({
                "incident": incident,
                "label": label,
                "expected_action": "见分类体系",
            }, ensure_ascii=False) + "\n")
    from collections import Counter
    c = Counter(lbl for lbl, _ in SAMPLES)
    print(f"困难测试集已生成: {OUT_FILE} (共 {len(SAMPLES)} 条)")
    for k, v in c.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
