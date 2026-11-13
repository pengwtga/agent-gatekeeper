"""值班知识库生成器 — 历史工单（现象 → 根因 → 处置）

30 条合成历史工单，覆盖 5 类根因 × 6 种真实变体，作为 RAG 值班助手的知识库。
内容基于 SSRM 真实 oncall 处置经验编写。

用法：python scripts/gen_kb.py  →  data/ssrm/kb_tickets.jsonl
"""

import json
from pathlib import Path

OUT = Path(__file__).parent.parent / "data" / "ssrm" / "kb_tickets.jsonl"

# (根因类别, 现象标题, 症状描述, 处置动作, 处置过程/复盘)
TICKETS = [
    # ── xflow_callback_lost ──
    ("xflow_callback_lost", "注册卡在审批中", "供应商反馈注册提交后一直显示「审批中」，后台查审批单状态已是终态", "主动拉取工单状态补齐", "按工单号调 xFlow 查询接口确认审批通过，手动触发注册任务创建；事后排查为回调网络抖动，未再复现"),
    ("xflow_callback_lost", "审批通过但无任务", "对账脚本告警：xFlow 工单已 FINISHED 但 SSRM 无对应注册任务", "主动拉取工单状态补齐", "批量补建 3 笔任务；根因是服务发布窗口回调恰好在重启间隙，补加了回调重试"),
    ("xflow_callback_lost", "海外审批超时", "海外时区审批人在非工作时间审批，供应商等待超过 8 小时", "主动拉取工单状态补齐", "确认审批已通过但回调延迟；调整了海外审批人分组，增加时区覆盖"),
    ("xflow_callback_lost", "回调地址变更未同步", "xFlow 回调配置指向旧域名，回调全部失败", "更新回调配置", "协调 xFlow 管理员更新回调地址，历史 5 笔工单补齐"),
    ("xflow_callback_lost", "审批人转办后回调丢失", "审批人转办给他人后，原回调丢失", "主动拉取工单状态补齐", "转办不影响回调链路，实为偶发丢失；纳入夜间对账覆盖范围"),
    ("xflow_callback_lost", "审批被驳回但状态未同步", "审批单显示已驳回，SSRM 侧状态仍为审批中", "主动拉取工单状态同步", "补同步驳回状态并通知商务；后续对账任务覆盖了驳回态"),

    # ── ea_sync_timeout ──
    ("ea_sync_timeout", "EA 发布窗口写入失败", "EA 例行发布期间 createSupplier 批量超时", "异步任务退避重试", "重试 3 次后成功；后续与 EA 建立发布窗口同步机制，窗口期内自动延后非紧急同步"),
    ("ea_sync_timeout", "网关 504", "EA 接口网关超时，重试 4 次均失败", "异步任务退避重试", "EA 网络抖动，10 分钟后自愈，对账任务补推成功"),
    ("ea_sync_timeout", "EA 限流", "大批量变更触发 EA 接口限流（429）", "异步任务退避重试+限速", "接入限流自适应：遇 429 自动降速，改为错峰分批同步"),
    ("ea_sync_timeout", "连接重置", "调用主数据平台连接被重置（CONN_RESET）", "异步任务退避重试", "长连接被对端回收导致，改为每次新建连接后恢复"),
    ("ea_sync_timeout", "同步任务积压", "同步任务队列积压 200+，延迟 30 分钟", "扩容任务消费者", "临时增加消费者并行度，积压 20 分钟消化完毕；后续加了积压量告警"),
    ("ea_sync_timeout", "DNS 解析异常", "服务器 DNS 解析 EA 域名偶发失败", "异步任务退避重试", "运维更新 DNS 配置后恢复，期间 12 笔任务重试成功"),

    # ── duplicate_submit ──
    ("duplicate_submit", "用户双击提交", "同一供应商 10 秒内两次注册请求，第二笔报申请已存在", "幂等去重返回成功", "前端对第二笔做幂等提示；无需数据修复"),
    ("duplicate_submit", "MQ 重复消费", "同一注册事件被消费两次", "幂等去重返回成功", "消费端按 correlation_id 去重，幂等机制正常工作，无需处理"),
    ("duplicate_submit", "页面刷新重放", "用户提交后刷新页面导致表单重放", "幂等去重返回成功", "表单提交改为 POST + 幂等键，前端生成一次即可"),
    ("duplicate_submit", "并发双开页面", "同一商务双开两个页面同时提交不同供应商", "正常处理", "两笔供应商不同，非重复；确认幂等键设计按供应商粒度，互不影响"),
    ("duplicate_submit", "重试引发的重复", "网络超时后客户端自动重试，服务端实际已处理成功", "幂等去重返回成功", "重试命中幂等键返回原结果，符合预期"),
    ("duplicate_submit", "审批重复发起", "同一准入被两个商务先后发起", "后到者幂等拒绝", "第二笔被幂等拒绝并提示第一笔的审批进度；产品确认这是预期行为"),

    # ── data_inconsistency ──
    ("data_inconsistency", "门户缺联系人", "夜间对账：EA 已有供应商但 Dora 缺联系人", "对账补推", "对账任务自动补推 1 笔成功；根因是当日 Dora 接口抖动，实时同步失败后未重试"),
    ("data_inconsistency", "变更未同步全量下游", "供应商改名后 EA 更新成功，Dora 侧仍显示旧名", "对账补推", "补推成功；根因是变更同步部分成功，补加了变更场景的重试"),
    ("data_inconsistency", "对账任务未运行", "连续两天对账没有产生补推记录，积压差异 5 笔", "手动触发对账", "发现对账调度任务被误停，重启后全部补齐；加了调度存活告警"),
    ("data_inconsistency", "联系人字段为空", "快照对比发现 23 家供应商门户侧联系人字段为空", "对账补推", "历史上 Dora 接口字段名变更导致，历史数据批量补齐"),
    ("data_inconsistency", "跨域状态不一致", "SSRM 显示准入完成，Dora 显示待激活", "对账补推", "补推激活状态；根因是激活通知消息丢失，纳入补偿"),
    ("data_inconsistency", "ES 与库不一致", "ES 搜索结果与数据库查询结果差 2 家供应商", "重建 ES 索引片段", "夜间对账发现差异并修复；根因是当日实时同步失败且重试耗尽"),

    # ── frontend_param_error ──
    ("frontend_param_error", "字段格式错误", "营业执照编号提交的是图片路径而非编号", "拒绝并反馈前端", "前端改用 OCR 识别后的文本字段，问题消失"),
    ("frontend_param_error", "少传必填字段", "Dora 注册表单少传 site_code，后端校验失败", "拒绝并反馈前端", "前端补上隐藏字段；后端错误信息增加字段级提示"),
    ("frontend_param_error", "枚举值传中文名", "供应商类型传了「服务商」，后端枚举校验失败", "拒绝并反馈前端", "前端改为传枚举值；后端错误信息列出合法枚举"),
    ("frontend_param_error", "日期格式不一致", "资质有效期格式前端传 MM/DD/YYYY，后端期望 YYYY-MM-DD", "拒绝并反馈前端", "前后端统一为 ISO 格式；后端兼容解析了历史格式"),
    ("frontend_param_error", "金额字段精度", "前端把金额传成科学计数法字符串", "拒绝并反馈前端", "前端改为定长小数字符串；后端增加解析兼容"),
    ("frontend_param_error", "空字符串与空指针", "前端把空值传成空字符串，后端判空逻辑不一致", "拒绝并反馈前端", "前后端约定：空值统一传 null，后端兼容空字符串"),
]


def main():
    with OUT.open("w", encoding="utf-8") as f:
        for i, (label, title, symptom, action, resolution) in enumerate(TICKETS):
            f.write(json.dumps({
                "ticket_id": f"ONCALL-{i + 1:03d}",
                "title": title,
                "symptom": symptom,
                "root_cause_class": label,
                "standard_action": action,
                "resolution": resolution,
            }, ensure_ascii=False) + "\n")
    from collections import Counter
    c = Counter(t[0] for t in TICKETS)
    print(f"知识库已生成: {OUT} (共 {len(TICKETS)} 条)")
    for k, v in c.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
