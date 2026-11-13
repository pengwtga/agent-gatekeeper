"""合成数据集生成器 — SSRM 卡单/同步失败场景

生成 5 类根因 × 20 条 = 100 条事件样本，字段模拟真实 oncall 工单上下文：
stage / source_system / error_code / error_message / wait_minutes / retry_count / downstream / http_status

用法：
    ~/.workbuddy/binaries/python/envs/aiops/bin/python scripts/gen_dataset.py
输出：
    data/ssrm/incidents.jsonl（每行一个样本：{"incident": {...}, "label": "...", "expected_action": "..."})
"""

import json
import random
from pathlib import Path

random.seed(20261023)  # 固定种子保证可复现

DATASET_DIR = Path(__file__).parent.parent / "data" / "ssrm"
OUT_FILE = DATASET_DIR / "incidents.jsonl"

STAGES = ["register", "register", "register", "change", "change"]  # 注册为主
HOURS = list(range(9, 23))


def jitter(v, lo, hi):
    return round(random.uniform(lo, hi), 2) if isinstance(v, float) else v


def gen_xflow_callback_lost(i: int) -> dict:
    ticket = f"FT202610{random.randint(10, 28)}{random.randint(100, 999)}"
    variants = [
        {
            "stage": random.choice(STAGES), "source_system": "xflow",
            "error_code": None, "http_status": None, "retry_count": 0,
            "downstream": "ssrm", "wait_minutes": random.randint(25, 240),
            "error_message": f"审批单 {ticket} 状态为 APPROVED，但 SSRM 无对应任务",
        },
        {
            "stage": random.choice(STAGES), "source_system": "xflow",
            "error_code": None, "http_status": None, "retry_count": 0,
            "downstream": "ssrm", "wait_minutes": random.randint(30, 600),
            "error_message": f"工单 {ticket} 审批流已结束(APPROVED)，SSRM 注册任务缺失，疑似回调未送达",
        },
        {
            "stage": random.choice(STAGES), "source_system": "ssrm",
            "error_code": "CALLBACK_MISSING", "http_status": None, "retry_count": 0,
            "downstream": "ssrm", "wait_minutes": random.randint(35, 480),
            "error_message": f"对账发现 xFlow 工单 {ticket} 已终态但本地任务未创建",
        },
    ]
    return random.choice(variants)


def gen_ea_sync_timeout(i: int) -> dict:
    variants = [
        {
            "stage": random.choice(STAGES), "source_system": "ssrm",
            "error_code": "EA_TIMEOUT", "http_status": 504, "retry_count": random.randint(2, 5),
            "downstream": "ea", "wait_minutes": random.randint(1, 15),
            "error_message": "调用 EA createSupplier 接口超时(5s)",
        },
        {
            "stage": random.choice(STAGES), "source_system": "ssrm",
            "error_code": "EA_5XX", "http_status": random.choice([502, 503, 500]),
            "retry_count": random.randint(1, 4), "downstream": "ea",
            "wait_minutes": random.randint(1, 20),
            "error_message": f"EA updateSupplier 返回 {random.choice([502, 503])}，疑似 EA 发布窗口",
        },
        {
            "stage": random.choice(STAGES), "source_system": "ssrm",
            "error_code": None, "http_status": 504, "retry_count": random.randint(2, 5),
            "downstream": "ea", "wait_minutes": random.randint(1, 10),
            "error_message": "同步供应商到 EA timed out after 5000 ms",
        },
    ]
    return random.choice(variants)


def gen_duplicate_submit(i: int) -> dict:
    variants = [
        {
            "stage": random.choice(STAGES), "source_system": "dora",
            "error_code": "DUPLICATE_KEY", "http_status": 409, "retry_count": 0,
            "downstream": "ssrm", "wait_minutes": random.randint(0, 2),
            "error_message": "重复提交：供应商注册申请 SUP{0} 已存在（幂等冲突）".format(random.randint(100000, 999999)),
        },
        {
            "stage": random.choice(STAGES), "source_system": "mq",
            "error_code": "IDEMPOTENT_CONFLICT", "http_status": None, "retry_count": random.randint(1, 3),
            "downstream": "ssrm", "wait_minutes": 0,
            "error_message": "MQ 重复消费：同一 correlation_id 的注册事件已处理",
        },
        {
            "stage": random.choice(STAGES), "source_system": "dora",
            "error_code": "DUPLICATE_KEY", "http_status": 409, "retry_count": 0,
            "downstream": "ssrm", "wait_minutes": 0,
            "error_message": "用户 3 秒内连续点击提交按钮，产生 duplicate 注册请求",
        },
    ]
    return random.choice(variants)


def gen_data_inconsistency(i: int) -> dict:
    variants = [
        {
            "stage": "register", "source_system": "reconcile",
            "error_code": "RECONCILE_DIFF", "http_status": None, "retry_count": 0,
            "downstream": random.choice(["dora", "ea"]), "wait_minutes": random.randint(600, 1440),
            "error_message": "夜间对账发现：SSRM 注册完成但 Dora 侧联系人缺失（EA 同步成功）",
        },
        {
            "stage": "change", "source_system": "reconcile",
            "error_code": "RECONCILE_DIFF", "http_status": None, "retry_count": 0,
            "downstream": random.choice(["dora", "ea"]), "wait_minutes": random.randint(700, 1400),
            "error_message": "部分成功：EA 已更新但 Dora 公司信息缺失，疑似同步中断",
        },
        {
            "stage": random.choice(STAGES), "source_system": "ssrm",
            "error_code": "PARTIAL_SUCCESS", "http_status": 200, "retry_count": 0,
            "downstream": random.choice(["dora", "ea"]), "wait_minutes": random.randint(60, 720),
            "error_message": "变更后双写部分成功，缺失下游数据，等待对账确认",
        },
    ]
    return random.choice(variants)


def gen_frontend_param_error(i: int) -> dict:
    missing = random.choice(["legal_entity_id", "contact_phone", "business_license_no", "site_code"])
    variants = [
        {
            "stage": random.choice(STAGES), "source_system": "dora",
            "error_code": "PARAM_MISSING", "http_status": 400, "retry_count": 0,
            "downstream": "frontend", "wait_minutes": 0,
            "error_message": f"请求缺少必填字段 {missing}",
        },
        {
            "stage": random.choice(STAGES), "source_system": "dora",
            "error_code": "PARAM_INVALID", "http_status": 422, "retry_count": 0,
            "downstream": "frontend", "wait_minutes": 0,
            "error_message": f"字段 {missing} 格式非法（期望数字，收到字符串）",
        },
        {
            "stage": random.choice(STAGES), "source_system": "ssrm",
            "error_code": "PARAM_MISSING", "http_status": 400, "retry_count": 0,
            "downstream": "frontend", "wait_minutes": 0,
            "error_message": "前端传参错误：missing field " + missing + "，调用方为 Dora 注册页",
        },
    ]
    return random.choice(variants)


GENERATORS = {
    "xflow_callback_lost": gen_xflow_callback_lost,
    "ea_sync_timeout": gen_ea_sync_timeout,
    "duplicate_submit": gen_duplicate_submit,
    "data_inconsistency": gen_data_inconsistency,
    "frontend_param_error": gen_frontend_param_error,
}

EXPECTED_ACTION = {
    "xflow_callback_lost": "按工单号主动拉取 xFlow 状态补齐注册任务",
    "ea_sync_timeout": "异步任务退避重试，超阈值转人工，对账兜底",
    "duplicate_submit": "幂等去重直接返回成功，无需人工",
    "data_inconsistency": "确认对账任务运行，自动补推缺失下游",
    "frontend_param_error": "拒绝并反馈具体字段问题，推动前端修复",
}

ACTIONS = [
    {
        "stage": s, "source_system": src, "error_code": ec, "http_status": hs,
        "retry_count": rc, "downstream": ds, "wait_minutes": wm, "error_message": em,
    }
    for label, gen in GENERATORS.items()
    for s, src, ec, hs, rc, ds, wm, em in
    [(x["stage"], x["source_system"], x["error_code"], x["http_status"],
      x["retry_count"], x["downstream"], x["wait_minutes"], x["error_message"])
     for x in [gen(i) for i in range(20)]]
]

LABELS = [lbl for lbl, gen in GENERATORS.items() for _ in range(20)]


def main():
    DATASET_DIR.mkdir(parents=True, exist_ok=True)
    with OUT_FILE.open("w", encoding="utf-8") as f:
        for idx, (incident, label) in enumerate(zip(ACTIONS, LABELS)):
            incident = dict(incident)
            incident["incident_id"] = f"SIM-{idx:04d}"
            incident["hour"] = random.choice(HOURS)
            row = {
                "incident": incident,
                "label": label,
                "expected_action": EXPECTED_ACTION[label],
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # 统计
    from collections import Counter
    c = Counter(LABELS)
    print(f"数据集已生成: {OUT_FILE} (共 {len(LABELS)} 条)")
    for k, v in c.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
