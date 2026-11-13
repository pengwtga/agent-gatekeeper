"""信贷进件评测集生成器 — 5 类 × 参数化合成

在 30 条手工"金样本"（CRA-*，含刻意设计的灰区/边界案例）基础上，
参数化生成至 300 条（CRG-*），固定随机种子可复现。

每类的生成约束严格对齐规则基线的判定逻辑（金样本负责边界，生成集负责量与分布）：
- pass:           全指标在阈值内（查询≤5 / dti≤0.45 / 月供≤收入50% / 设备≤60 / 用途具体）
- multi_debt:     查询≥8（最强信号）或 dti≥0.70；另含灰区形态样本（dti 0.60~0.70 且查询<8，
                  规则会落 unknown 升级 LLM 仲裁——评测集保留这个漏斗的真实形态）
- repayment_risk: dti 0.50~0.59 或 月供>收入50%（避开共债与灰区带）
- fraud_suspect:  设备风险≥85 / 查询≥12且现单位≤3月 / 中介话术，任一硬信号
- material_issue: 用途说明空白或<6字，或月收入缺失（其余指标刻意给"看起来很好"）

用法：
    .venv/bin/python scripts/gen_credit_dataset.py [total]   # 默认 300，可重跑
输出：
    data/credit/credit_applications.jsonl（金样本 + 生成样本，按 app_id 排序）
"""

import json
import random
import sys
from pathlib import Path

random.seed(20261113)  # 固定种子保证可复现

DATA_FILE = Path(__file__).parent.parent / "data" / "credit" / "credit_applications.jsonl"

PURPOSES_PASS = [
    "装修{},已和装修公司签合同", "购买{},已和卖家谈好价格", "{}学费,学校缴费单在手上",
    "{}医疗费用,医院方案已确认", "婚宴{},酒店已下订金", "{}置换,旧的去抵一部分",
    "家里{}坏了要换新", "{}进货周转,生意流水稳定", "{}研学费用,机构合同已签",
    "置换{},差价部分需要贷款",
]
FILLERS = ["厨房", "老家房子", "女儿的钢琴课", "父亲的手术", "酒席", "代步车", "冰箱",
           "门店的货", "孩子的夏令营", "电动车"]
PURPOSES_MULTI = [
    "别的平台借的先还上,这个利息低点", "整合一下之前的分期,压力大", "信用卡账单快到期了先周转",
    "几个口子都借遍了,这边再借点", "借新还旧,倒一下手头",
]
PURPOSES_FRAUD_AGENT = [
    "朋友介绍的中介说这个口子包过", "找了代办说有内部渠道", "说好无视征信黑户也能下",
    "中介帮忙包装资料,说肯定能过", "中介说走特殊通道不用看征信",
]
PURPOSES_SHORT = ["", " ", "急用", "周转", "要用钱"]

ACTIONS = {
    "pass": "进入正常授信流程，按标准额度模型定价",
    "multi_debt": "进信审决策，降额或拒绝，附征信查询证据",
    "repayment_risk": "进信审决策，降额授信或拒绝，附收入负债测算依据",
    "fraud_suspect": "冻结授信转欺诈调查，2 小时内人工核查",
    "material_issue": "退回补件队列，列明需补齐的材料清单",
}


def _row(i: int, label: str, inc: dict) -> dict:
    return {
        "incident": {"app_id": f"CRG-{i:06d}", **inc},
        "label": label,
        "expected_action": ACTIONS[label],
    }


def _base_inc() -> dict:
    return {
        "amount": round(random.uniform(5000, 80000), 0),
        "term_months": random.choice([12, 24, 36]),
        "monthly_income": round(random.uniform(6000, 45000), 0),
        "dti": round(random.uniform(0.10, 0.45), 2),
        "credit_queries_3m": random.randint(0, 5),
        "overdue_24m": random.choices([0, 0, 0, 1], k=1)[0],
        "age": random.randint(22, 55),
        "tenure_months": random.randint(12, 96),
        "device_risk": random.randint(5, 55),
        "application_text": random.choice(PURPOSES_PASS).format(random.choice(FILLERS)),
        "hour": random.randint(8, 22),
    }


def gen_pass(i: int) -> dict:
    inc = _base_inc()
    # 月供 ≤ 收入 45%，确保落在放行区
    inc["amount"] = round(inc["monthly_income"] * random.uniform(0.5, 4.0), 0)
    return _row(i, "pass", inc)


def gen_material(i: int) -> dict:
    inc = _base_inc()
    mode = random.random()
    if mode < 0.45:                      # 用途缺失/过短（<6 字）
        inc["application_text"] = random.choice(PURPOSES_SHORT)
    elif mode < 0.80:                    # 收入缺失，其余看起来很好
        inc["monthly_income"] = 0
    else:                                # 双缺
        inc["application_text"] = random.choice(PURPOSES_SHORT)
        inc["monthly_income"] = 0
    inc["credit_queries_3m"] = random.randint(0, 5)
    inc["device_risk"] = random.randint(5, 50)
    # 金额钳制：月供 ≤ 收入 40%，避免"月供超收入"规则先于"资料不全"触发——
    # 资料类进件的标签语义是"先补件再谈额度"，不该被还款能力规则截胡
    if inc["monthly_income"] > 0:
        inc["amount"] = round(min(inc["amount"], inc["monthly_income"] * 0.4 * inc["term_months"]), 0)
    return _row(i, "material_issue", inc)


def gen_multi(i: int) -> dict:
    inc = _base_inc()
    inc["overdue_24m"] = random.choices([0, 1, 2], k=1)[0]
    mode = random.random()
    if mode < 0.55:                      # 查询 8~11 为主信号，dti 中等
        inc["credit_queries_3m"] = random.randint(8, 11)
        inc["dti"] = round(random.uniform(0.30, 0.58), 2)   # 避开灰区带
        inc["device_risk"] = random.randint(5, 75)
        inc["application_text"] = random.choice(PURPOSES_MULTI)
    elif mode < 0.85:                    # dti ≥ 0.70，查询不密集
        inc["credit_queries_3m"] = random.randint(0, 7)
        inc["dti"] = round(random.uniform(0.70, 0.88), 2)
        inc["device_risk"] = random.randint(5, 70)
    else:                                # 双高
        inc["credit_queries_3m"] = random.randint(8, 11)
        inc["dti"] = round(random.uniform(0.70, 0.88), 2)
        inc["device_risk"] = random.randint(5, 70)
        inc["application_text"] = random.choice(PURPOSES_MULTI)
    return _row(i, "multi_debt", inc)


def gen_gray(i: int) -> dict:
    """灰区形态：dti 0.60~0.70 且查询 <8 —— 规则落 unknown、升级 LLM 仲裁的漏斗入口。
    label 取 multi_debt（"借新还旧"用途更像共债整合），这是一道有争议空间的真实边界题。"""
    inc = _base_inc()
    inc["dti"] = round(random.uniform(0.60, 0.69), 2)
    inc["credit_queries_3m"] = random.randint(0, 7)
    inc["device_risk"] = random.randint(5, 70)
    inc["application_text"] = random.choice(PURPOSES_MULTI + ["生意周转需要一笔资金"])
    return _row(i, "multi_debt", inc)


def gen_repayment(i: int) -> dict:
    inc = _base_inc()
    inc["credit_queries_3m"] = random.randint(0, 7)      # 避开共债信号
    if random.random() < 0.5:
        inc["dti"] = round(random.uniform(0.50, 0.59), 2)  # 避开灰区带（0.60~0.70）
    else:                                                # 月供 > 收入 50%
        inc["monthly_income"] = round(random.uniform(4000, 9000), 0)
        # 月供 = 额度/期数 ∈ 收入的 55%~90%，确保触发"月供>收入50%"规则
        inc["amount"] = round(inc["monthly_income"] * 0.5 * inc["term_months"]
                              * random.uniform(1.1, 1.8), 0)
        inc["dti"] = round(random.uniform(0.30, 0.55), 2)
    return _row(i, "repayment_risk", inc)


def gen_fraud(i: int) -> dict:
    inc = _base_inc()
    mode = random.random()
    if mode < 0.40:                      # 设备风险异常
        inc["device_risk"] = random.randint(85, 99)
    elif mode < 0.75:                    # 短期高频申请（凌晨、小额多头、现单位极短）
        inc["credit_queries_3m"] = random.randint(12, 20)
        inc["tenure_months"] = random.randint(1, 3)
        inc["age"] = random.randint(20, 30)
        inc["hour"] = random.choice([0, 1, 2, 3, 23])
        inc["amount"] = round(random.uniform(40000, 120000), 0)
    else:                                # 中介话术
        inc["application_text"] = random.choice(PURPOSES_FRAUD_AGENT)
        inc["hour"] = random.choice([1, 2, 3, 22, 23])
    return _row(i, "fraud_suspect", inc)


def main() -> None:
    total = int(sys.argv[1]) if len(sys.argv) > 1 else 300

    # 金样本 = 现有文件里的手工样本（CRA-*）；生成器可重跑，先剥离旧生成样本再重造
    golden = []
    if DATA_FILE.exists():
        for line in DATA_FILE.open(encoding="utf-8"):
            if not line.strip():
                continue
            row = json.loads(line)
            if row["incident"]["app_id"].startswith("CRA-"):
                golden.append(row)
    print(f"金样本: {len(golden)} 条（CRA-*）")

    n_gen = total - len(golden)
    per_class = n_gen // 5
    n_gray = max(1, int(per_class * 0.15))             # multi_debt 中 15% 用灰区形态
    rows = []
    for gen in [gen_pass] * per_class + [gen_material] * per_class + \
               [gen_fraud] * per_class + [gen_repayment] * per_class + \
               [gen_multi] * (per_class - n_gray) + [gen_gray] * n_gray:
        rows.append(gen(len(rows)))

    all_rows = sorted(golden + rows, key=lambda r: r["incident"]["app_id"])
    with DATA_FILE.open("w", encoding="utf-8") as f:
        for r in all_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    from collections import Counter
    dist = Counter(r["label"] for r in all_rows)
    print(f"生成: {len(rows)} 条 → 合计 {len(all_rows)} 条")
    print("类别分布:", dict(dist))


if __name__ == "__main__":
    main()
