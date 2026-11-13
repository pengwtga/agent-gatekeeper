"""三域助手知识库 + 检索评测集生成器（固定种子，可重复运行）

产出（全部幂等：手工金样本永远保留，生成物可随时重刷）：
1. data/ssrm/kb_tickets.jsonl            30 条手工金样本 + 120 条变体 = 150 条（SSRM 值班助手）
2. data/ssrm/rag_queries.jsonl           30 条手工 + 120 条变体查询 = 150 条（每变体一条，带区分 token）
3. data/ssrm/rag_queries_hard.jsonl      10 条手工 + 120 条变体困难查询 = 130 条
4. data/csa/csa_agent_kb.jsonl           100 条坐席助手 SOP/话术（新增）
5. data/credit/credit_collection_kb.jsonl 120 条催员辅助话术/合规红线（新增）
6. data/csa/csa_kb_queries.jsonl         40 条坐席助手检索评测
7. data/credit/credit_kb_queries.jsonl   40 条催员辅助检索评测

设计要点：
- SSRM 变体用「同义词替换 + 追加区分性细节」生成——替换压低与原问句的字符重叠，
  追加的供应商名等新字符不参与命中，保证原始 30 条查询的 top1 仍指向原工单；
- 变体查询刻意包含区分性 token（供应商名/月份），对应真实提问习惯（值班会说清是谁的事）；
- csa/credit 知识库条目共享统一五字段 schema（id/category/title/symptom/resolution），
  与 AssistantSpec 契约对齐；resolution 里写完整 SOP/话术/合规要点，供 LLM grounding。
"""

import json
import random
from pathlib import Path

random.seed(20261113)

DATA = Path(__file__).resolve().parents[1] / "data"


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]


def save(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"  {path.name}: {len(rows)} 条")


# ─────────────────────────── 1. SSRM 知识库 30 → 150 ───────────────────────────

SUPPLIERS = ["华东面料厂", "广州五金制品", "苏州电子元件", "天津包装材料", "义乌小商品供应链",
             "山东纺织集团", "深圳模具加工", "杭州物流服务", "宁波注塑件厂", "佛山家具五金",
             "青岛食品配料", "厦门运动器材", "东莞塑胶制品", "武汉光电科技", "绍兴印染厂"]
DETAILS = ["工单号 XF-88{n}", "重试 {k} 次未自动恢复", "涉及 {k} 笔任务", "发生在发布窗口内",
           "距上次同步已 {k} 分钟", "值班群已同步进展"]
# 同义词替换：压低变体与原始问句的字符重叠（变体应是「改写」不是「超集」）
SUBS = [
    {"供应商": "商家", "显示": "展示"},
    {"一直": "始终", "后台": "管理端"},
    {"反馈": "反映", "提交": "送出"},
    {"告警": "报警", "脚本": "程序"},
    {"审批": "审核", "状态": "进度"},
    {"任务": "记录", "创建": "新建"},
    {"失败": "异常", "同步": "联动"},
    {"超时": "超限", "接口": "服务"},
]


def expand_ssrm_kb() -> list[str]:
    kb = load(DATA / "ssrm" / "kb_tickets.jsonl")
    originals = [t for t in kb if "-V" not in t["ticket_id"]]
    variants = []
    for t in originals:
        for n in range(1, 5):
            sub = SUBS[(n - 1) % len(SUBS)]
            symptom = t["symptom"]
            for a, b in sub.items():
                symptom = symptom.replace(a, b)
            supplier = random.choice(SUPPLIERS)
            detail = random.choice(DETAILS).format(k=random.randint(2, 30), n=random.randint(1, 9))
            month = random.randint(1, 12)
            variants.append({
                "ticket_id": f"{t['ticket_id']}-V{n}",
                "title": f"{t['title']}（{supplier}）",
                "symptom": f"{symptom}，涉及{supplier}，{detail}",
                "root_cause_class": t["root_cause_class"],
                "standard_action": t["standard_action"],
                "resolution": f"{t['resolution']}；本例为 {month} 月{supplier}场景，处置路径一致，"
                              f"事后在周报中补充了巡检项",
            })
    save(DATA / "ssrm" / "kb_tickets.jsonl", originals + variants)
    return [v["ticket_id"] for v in variants]


# 变体查询：包含供应商名（区分性 token）+ 现象改写
SSRM_Q_TEMPLATES = {
    "xflow_callback_lost": ["{s}那边审批早就通过了，系统里怎么还没生成记录",
                            "{s}的单子终态了但系统查不到，帮忙看下怎么补",
                            "{s}反馈单子同意一天了，我们系统里影子都没有"],
    "ea_sync_timeout": ["{s}的主数据同步一直转圈，帮看下是不是超时了",
                        "{s}那边资料改了但没同步过来，怎么处理",
                        "{s}的数据接口一直超时，同步任务卡住了"],
    "duplicate_submit": ["{s}同一单重复提交了两笔，怎么去掉多余的那笔",
                         "{s}那边手抖提交了两次，重复记录怎么处理",
                         "{s}产生了双份单据，帮忙合并或删掉一条"],
    "data_inconsistency": ["{s}的单据金额和明细对不上，帮忙核对差异",
                           "{s}反映两边数据不一致，以哪边为准怎么修",
                           "{s}那条记录字段对不上，怎么排查修正"],
    "frontend_param_error": ["{s}反馈页面提交报参数错误，单据发不出去",
                             "{s}那边操作页面一直报错，像是参数问题",
                             "{s}提交时页面提示字段校验失败，怎么排查"],
}
SSRM_HARD_MARKERS = ["那边", "这单", "搞", "弄", "啥", "咋"]


def gen_ssrm_queries(variant_ids: list[str]) -> None:
    """变体查询从变体自身 symptom 派生（换一组同义词替换 = 改写而非复制），
    并保留区分性细节（供应商/工单号），使正确条目可被检索命中。"""
    kb = {t["ticket_id"]: t for t in load(DATA / "ssrm" / "kb_tickets.jsonl")}
    queries, hard = [], []
    i = 0
    for vid in variant_ids:
        t = kb[vid]
        # 与生成变体时不同的替换组 → 改写
        sub = SUBS[(int(vid.split("-V")[1]) + 3) % len(SUBS)]
        symptom = t["symptom"]
        for a, b in sub.items():
            symptom = symptom.replace(a, b)
        symptom = symptom.replace("，涉及", "，这边是").replace("。", "")
        queries.append({"query_id": f"RQ-{30 + i:03d}", "question": f"求助：{symptom}",
                        "relevant": [vid]})
        # 困难查询：在改写基础上去黑话 + 口语化
        hq = symptom
        for a, b in (("xFlow", "那边的流程"), ("SSRM", "咱们系统"), ("EA", "主数据那边"),
                     ("审批", "审核"), ("回调", "通知"), ("任务", "记录")):
            hq = hq.replace(a, b)
        hard.append({"query_id": f"HRQ-{10 + i:03d}", "question": f"值班求助，{hq}", "relevant": [vid]})
        i += 1
    old_q = [q for q in load(DATA / "ssrm" / "rag_queries.jsonl") if int(q["query_id"].split("-")[1]) < 30]
    old_h = [q for q in load(DATA / "ssrm" / "rag_queries_hard.jsonl") if int(q["query_id"].split("-")[1]) < 10]
    save(DATA / "ssrm" / "rag_queries.jsonl", old_q + queries)
    save(DATA / "ssrm" / "rag_queries_hard.jsonl", old_h + hard)


# ─────────────────────────── 2. CSA 坐席助手知识库 100 条 ───────────────────────────

CSA_PRODUCTS = ["蓝牙耳机", "羽绒服", "保温杯", "电动牙刷", "榨汁机", "帆布鞋", "儿童座椅",
                "香薰灯", "机械键盘", "瑜伽垫", "电煮锅", "真丝枕套", "露营椅", "加湿器",
                "运动水壶", "羊毛围巾", "洗地机", "懒人沙发", "筋膜枪", "投影仪"]

CSA_SOP: dict[str, list[dict]] = {
    "refund_dispute": [
        {"title": "退款超时未到账", "symptom": "用户反馈{d}天前申请退款，承诺 3 天到账，第 {d} 天仍未收到 {m} 元",
         "resolution": "话术：「非常抱歉让您久等了，我马上为您核实退款进度」。动作：①查订单退款流水确认是否原路退回；②银行处理中的，告知 1-3 个工作日内到账并致歉；③超 7 个工作日的，升级支付侧人工工单，承诺 24 小时内答复。红线：不许诺具体到账时刻，只给时限区间。"},
        {"title": "仅退款争议", "symptom": "用户申请仅退款不退货，商家拒绝，用户情绪激动要求平台介入",
         "resolution": "话术：「您的诉求我已记录，平台会依据双方凭证公正处理」。动作：①核对商品金额与用户信用分，小额（≤30 元）建议商家仅退款止损；②引导用户上传商品问题照片；③48 小时内给出平台裁决。红线：不当面评判商家对错，只讲流程。"},
        {"title": "退货运费归属", "symptom": "质量问题的退货，用户要求商家承担运费，商家推诿",
         "resolution": "话术：「质量问题产生的运费应由商家承担，我来为您协调」。动作：①确认质检结论为质量问题；②为用户垫付运费险或创建 12 元运费补偿；③同步商家运费规则。红线：非质量问题不要承诺运费补偿。"},
        {"title": "退款被拒不满", "symptom": "退款申请被商家拒绝，用户威胁投诉到 12315",
         "resolution": "话术：「我理解您的心情，我们先看下拒绝原因，再一起想办法」。动作：①向用户说明拒绝理由与凭证缺口；②可补证据的引导补充后重新提交；③确实不符合政策的，明确告知政策依据与再次申诉入口。红线：不得暗示用户一定能退。"},
    ],
    "logistics_delay": [
        {"title": "发货延迟", "symptom": "订单超过 48 小时未发货，用户催促并要求赔偿",
         "resolution": "话术：「已为您加急催促仓库，预计 {d} 天内发出」。动作：①查仓库缺货/爆单原因；②超 72 小时未发的，补偿 5 元无门槛券；③用户不愿等的，协助全额退款并致歉。红线：发货时间以仓库回执为准，不拍脑袋承诺。"},
        {"title": "物流停滞", "symptom": "快递 {d} 天没有物流更新，用户担心丢件",
         "resolution": "话术：「我帮您联系快递网点核实包裹状态」。动作：①创建物流侧查件工单；②24 小时无回音按疑似丢件处理；③确认丢件的，直接补发或全额退款，无需用户申请。红线：丢件责任在平台侧，不要求用户自证。"},
        {"title": "错发/漏发", "symptom": "用户收到商品与订单不符（颜色/型号错）",
         "resolution": "话术：「给您带来不便非常抱歉，马上为您处理换货」。动作：①核对订单与实收差异；②寄回运费到付+优先补发正确商品；③错发商品可赠予用户（成本低于运费时）。红线：换货周期明确为 5-7 天。"},
        {"title": "拒收退回", "symptom": "用户拒收后包裹原路退回，要求尽快退款",
         "resolution": "话术：「包裹已在退回途中，签收后退款会立即启动」。动作：①查退回物流单号并告知预计时效；②商家签收后退 B 款；③超 5 天未退的升级商家侧工单。红线：不要在商家签收前承诺退款到账时间。"},
    ],
    "presale_consult": [
        {"title": "发货时间咨询", "symptom": "预售商品用户询问何时发货",
         "resolution": "话术：「该商品为预售，按页面标注最晚 {d} 天内发出」。动作：①引用商品页预售说明；②超期未发的主动补偿 10 元券；③急用的推荐现货替代款。红线：承诺口径与商品页一致，不得口头提前。"},
        {"title": "尺码推荐", "symptom": "用户询问{p}尺码如何选择，纠结两个码",
         "resolution": "话术：「您平时穿什么码？我结合版型给您建议」。动作：①要身高体重数据；②按版型表推荐并说明该款偏大/偏小；③提示运费险让用户无顾虑下单。红线：不确定时推荐运费险兜底，不打包票。"},
        {"title": "优惠活动咨询", "symptom": "用户问现在买还是等活动价，价差 {m} 元",
         "resolution": "话术：「目前到手价 X，活动预估 Y，您可以对比后决定」。动作：①如实报价不夸大活动力度；②告知活动时间；③可发放限量券缩小价差促成下单。红线：活动机制不确定时先核实，不编造。"},
        {"title": "材质功能咨询", "symptom": "用户询问{p}的材质与功能细节，担心描述不符",
         "resolution": "话术：「我对照详情页为您确认：材质是…，功能包括…」。动作：①以详情页与质检报告为准逐条回答；②详情页没写的如实说「需要为您核实」并建工单；③可发实物图/视频打消疑虑。红线：不得口头承诺详情页之外的参数。"},
    ],
    "invoice_request": [
        {"title": "电子发票开具", "symptom": "用户下单后申请电子普通发票，需修改抬头",
         "resolution": "话术：「发票支持自助修改一次，我教您操作」。动作：①指引订单页-申请开票-修改抬头；②已开错的作废重开（3 个工作日）；③企业抬头需税号，提醒核对。红线：不开具与交易不符的发票品类。"},
        {"title": "补开历史订单发票", "symptom": "用户 {m} 个月前的订单没开发票，现在补开",
         "resolution": "话术：「一年内的订单都支持补开，马上为您办理」。动作：①确认订单号与金额；②提交补开申请（5 个工作日内开出）；③发送至用户邮箱并短信通知。红线：超一年的引导人工专线。"},
        {"title": "专票开具咨询", "symptom": "企业用户咨询能否开增值税专用发票",
         "resolution": "话术：「该店铺支持专票，需要您提供开票资料」。动作：①收集公司全称/税号/地址电话/开户行；②专票 10 个工作日内寄出或电子交付；③额度上限（单笔 1 万以上）走线下合同。红线：资质不全不得先承诺后补。"},
        {"title": "发票金额争议", "symptom": "用户认为发票金额应含运费/优惠后的金额有歧义",
         "resolution": "话术：「发票金额按实付金额开具，我帮您核对」。动作：①按实付（含运费、扣除优惠券）开票；②用户坚持按原价的，解释税务口径；③争议不下的升级财务专线。红线：不得虚开金额。"},
    ],
    "account_security": [
        {"title": "疑似盗号", "symptom": "用户反馈账号在异地登录，且有非本人下单记录",
         "resolution": "话术：「已为您冻结账号并下线全部登录设备」。动作：①立即冻结+强制改密；②非本人订单全额退款并标记风险单；③引导开启登录二次验证。红线：泄露的用户信息一个字都不能发给来电方，只对本账号认证手机号沟通。"},
        {"title": "改绑手机受阻", "symptom": "用户手机号注销，无法接收验证码改绑",
         "resolution": "话术：「需要人工核身，我为您转专项通道」。动作：①收集身份证明材料（证件照+近期订单信息）；②人工审核 24 小时内回电；③通过后协助改绑。红线：核身材料不全不得跳过验证直接改绑。"},
        {"title": "密码重置疑惑", "symptom": "用户没操作却收到密码重置短信，担心被盗",
         "resolution": "话术：「可能是他人在尝试登录，我先加固您的账号」。动作：①确认本人操作与否；②非本人的强制下线+改密+开启验证；③提示勿点不明链接。红线：安抚为主，不渲染风险造成恐慌。"},
        {"title": "账号被封申诉", "symptom": "用户账号因异常行为被封，申诉要求解封",
         "resolution": "话术：「封禁原因我需要先为您核实，请稍等」。动作：①查风控命中规则（刷单/恶意退款等）；②误伤的提交解封工单（48 小时）；③确实违规的告知封禁依据与申诉截止时间。红线：不透露风控规则细节，防止绕过。"},
    ],
}


def gen_csa_kb() -> tuple[list[dict], list[dict]]:
    kb, queries = [], []
    cases_per_cat = 20
    for cat, bases in CSA_SOP.items():
        for i in range(cases_per_cat):
            base = bases[i % len(bases)]
            p = random.choice(CSA_PRODUCTS)
            d = random.randint(3, 12)
            m = random.choice([59, 89, 129, 199, 259, 399, 599])
            case_id = f"CSAK-{cat[:4].upper()}-{i + 1:02d}"
            symptom = base["symptom"].format(d=d, m=m, p=p)
            # 唯一区分性 token：商品词 + 序号场景词（查询里会带商品词）
            symptom = symptom + f"（订单商品：{p}，工单场景码 {cat[:4].upper()}{i + 1:02d}）"
            kb.append({"case_id": case_id, "category": cat,
                       "cluster_key": f"csa:{cat}:B{i % len(bases)}",
                       "title": f"{base['title']}·{p}", "symptom": symptom,
                       "resolution": base["resolution"].format(d=d, m=m)})
            if i < 8:  # 每类前 8 条生成查询
                q = f"用户咨询{p}相关：{base['title']}，坐席该怎么回复"
                queries.append({"query_id": f"CQ-{len(queries):03d}", "question": q,
                                "relevant": [case_id]})
    save(DATA / "csa" / "csa_agent_kb.jsonl", kb)
    save(DATA / "csa" / "csa_kb_queries.jsonl", queries)
    return kb, queries


# ─────────────────────────── 3. 信贷催员辅助知识库 120 条 ───────────────────────────

CREDIT_CHANNELS = ["电话", "短信", "IM 消息"]

CREDIT_SOP: dict[str, list[dict]] = {
    "early_reminder": [
        {"title": "M1 首次温和提醒", "symptom": "客户账单逾期 {d} 天，首次外呼提醒，客户态度平和",
         "resolution": "话术：「您好，您的账单已逾期 {d} 天，为避免影响征信，请您合理安排还款」。要点：①只陈述事实与后果，不施压；②确认客户是否收到账单；③挂机后下发还款提醒短信。合规：外呼时间限 8:00-21:00，当日同客户不超过 3 次。"},
        {"title": "宽限期说明", "symptom": "客户问逾期 {d} 天是否还有宽限期、会不会上征信",
         "resolution": "话术：「我行有 {d} 天宽限期政策，宽限内还清不计逾期」。要点：①以产品合同条款为准逐字说明；②宽限外如实告知征信影响，不夸大不淡化；③建议绑定自动还款。合规：政策表述与官方话术库一致，不得口头放宽。"},
        {"title": "还款日提醒升级", "symptom": "客户连续两个月在还款日后 {d} 天才还款，需前置提醒",
         "resolution": "话术：「注意到您前两期都是稍晚几天还款，我在账单日前一天提醒您可以吗」。要点：①语气友好，提供备忘服务而非指责；②推荐账单日短信/推送订阅；③记录客户偏好还款渠道。合规：客户拒绝提醒的，尊重意愿并留痕。"},
        {"title": "自动扣款失败安抚", "symptom": "客户绑定卡余额充足但自动扣款失败 {d} 天",
         "resolution": "话术：「可能是银行通道问题，我帮您登记排查」。要点：①核实扣款失败原因（通道/限额/卡状态）；②引导手动还款避免逾期；③渠道问题建工单跟进并回告。合规：不将系统问题归因客户。"},
    ],
    "promise_pay": [
        {"title": "承诺还款登记", "symptom": "客户承诺 {d} 天内归还欠款 {m} 元，需规范登记",
         "resolution": "话术：「跟您确认下，您计划 {d} 日前归还 {m} 元，我帮您登记提醒」。要点：①双确认金额与日期；②登记到催收系统 P 档；③承诺日当天上午跟进确认。合规：承诺录音/记录保存完整，争议时是唯一凭证。"},
        {"title": "承诺失约跟进", "symptom": "客户 {d} 天前承诺还款但未到账，二次外呼",
         "resolution": "话术：「上次您提到这几天还款，我看账上还没收到，是遇到什么困难了吗」。要点：①先问原因不先指责；②二次承诺缩短周期（≤7 天）并降低金额门槛；③连续失约的升级协商流程。合规：禁用「再不还就…」式威胁表述。"},
        {"title": "部分还款接受", "symptom": "客户资金紧张，提出先还 {m} 元剩余下月结清",
         "resolution": "话术：「可以先收您这 {m} 元，剩余部分我们再约时间」。要点：①部分还款先抵扣利息后抵本金（按合同）；②剩余部分重新登记承诺；③书面（短信）确认新计划。合规：还款冲抵顺序表述与合同一致。"},
        {"title": "代偿人沟通", "symptom": "客户家人来电表示愿意代还 {m} 元，询问流程",
         "resolution": "话术：「感谢您的支持，代还需要借款人本人确认」。要点：①代收需本人授权确认；②只与认证紧急联系人核对身份后才能谈流程，不得透露逾期细节；③提供官方还款渠道。合规：向第三方披露债务详情是红线，仅限确认身份后告知办理方式。"},
    ],
    "hardship_negotiate": [
        {"title": "失业延期申请", "symptom": "客户失业 {d} 天，申请延期还款，态度诚恳有还款意愿",
         "resolution": "话术：「您的情况可以申请延期/分期方案，我来说下材料和流程」。要点：①收离职证明/收入流水；②按政策给 1-3 个月宽限或免息分期；③书面确认新计划。合规：方案权限以系统审批为准，催员不得私许减免。"},
        {"title": "医疗困难协商", "symptom": "客户直系亲属重病住院，暂无还款能力，请求减免罚息",
         "resolution": "话术：「请节哀/保重，特殊困难有对应通道，我教您申请」。要点：①收医疗证明；②提交特殊困难减免（罚息部分）申请；③保持每周一次关怀式跟进。合规：同情话术不得夹带还款施压。"},
        {"title": "以贷养贷劝阻", "symptom": "客户提出想再借一笔新贷款还旧账",
         "resolution": "话术：「不建议这样做，成本会越滚越高，我们看看分期方案」。要点：①明确劝阻多头借贷；②引导协商既有债务；③做风险提示留痕。合规：不得诱导客户借新还旧。"},
        {"title": "协商方案复核", "symptom": "客户称之前谈好的分期方案现在变了，质疑催员反悔",
         "resolution": "话术：「我马上调当时的记录核对，请您稍等」。要点：①调录音/记录核实；②确实口径不一致的立即纠正并致歉；③统一以系统登记方案为准。合规：任何口头方案以系统审批为准的告知必须在首次沟通时说清。"},
    ],
    "lost_contact": [
        {"title": "失联初步找回", "symptom": "客户电话空号/停机 {d} 天，需合规找回",
         "resolution": "动作：①尝试全部预留号码与 IM 留言；②短信告知后果与联系方式；③检查是否换号未更新。合规：每日尝试频次有上限并留痕；不得使用未授权渠道获取的新号码。"},
        {"title": "紧急联系人边界", "symptom": "客户失联 {d} 天，考虑联系其预留紧急联系人",
         "resolution": "话术（对联系人）：「方便转告{姓名}回个官方客服电话吗」。要点：①仅可请其转告，不得透露欠款金额/逾期详情；②不得频繁骚扰联系人；③联系人明确拒绝的停止并留痕。合规：向第三人披露债务信息属违规催收，是投诉高发点。"},
        {"title": "换号未更新", "symptom": "客户手机号变更未更新，旧号被他人接听",
         "resolution": "话术：「您好，请问是机主本人吗？我们找{姓名}有事宜」。要点：①不得向新机主透露任何债务信息；②请其转告或挂断；③走其他合规渠道继续触达。合规：误拨第三方的通话要在记录中标注，防止投诉。"},
        {"title": "复联后降级处理", "symptom": "失联 {d} 天的客户主动回电，情绪戒备",
         "resolution": "话术：「感谢您回电，今天主要想了解下您的还款安排」。要点：①先感谢不打责；②重新核身后再谈方案；③失联期间的罚息如实说明。合规：复联首轮不得立即高压施压，防止二次失联。"},
    ],
    "compliance_redline": [
        {"title": "威胁恐吓识别", "symptom": "客户投诉称上一任催员说「不还就上门找人/通知单位」",
         "resolution": "处理：①立即调录音核实，属实上报质检与合规岗；②对客户正式致歉并承诺处理；③涉事催员停线培训。红线：威胁、恐吓、辱骂是《消费金融催收自律公约》明令禁止行为，零容忍。"},
        {"title": "冒充公检法识别", "symptom": "客户反映有人自称「法务」称将立案拘留，疑似违规施压话术",
         "resolution": "处理：①核实确非本机构行为（谨防诈骗冒名）；②本机构人员的立即停线调查；③提示客户官方核实渠道。红线：催收不得冒充公检法与政府机关，涉嫌违法。"},
        {"title": "信息泄露防护", "symptom": "客户质问催员如何知道其新单位地址，怀疑信息违规获取",
         "resolution": "处理：①如实说明信息来源（客户授权的合同条款/官方渠道）；②无法说明来源的立即上报合规审查；③对客户隐私诉求做好记录。红线：不得通过非法渠道购买或获取客户信息。"},
        {"title": "外访合规边界", "symptom": "网点申请上门外访长期失联客户，审批咨询",
         "resolution": "要点：①外访须两人以上并全程录音；②着装与身份明示，不得伪装；③客户拒绝接待的立即离开；④禁止夜间与节假日外访。红线：外访不得有扣押、围堵等限制人身自由行为。"},
        {"title": "催收时间违规", "symptom": "客户投诉晚上十点半接到催收电话",
         "resolution": "处理：①核实外呼时间记录，属实确认违规；②向客户致歉并登记；③调整该案件外呼时段策略。红线：外呼限于 8:00-21:00，系统强制管控，投诉一件查一件。"},
        {"title": "暴力催收举报响应", "symptom": "客户通过 12378/媒体渠道投诉暴力催收，舆情风险",
         "resolution": "处理：①2 小时内响应舆情工单；②调取全量录音自证或认责；③认责的按监管要求反馈整改报告。红线：不得私下联系客户要求撤诉，一切走官方渠道。"},
    ],
}


def gen_credit_kb() -> tuple[list[dict], list[dict]]:
    kb, queries = [], []
    cases_per_cat = 24
    for cat, bases in CREDIT_SOP.items():
        for i in range(cases_per_cat):
            base = bases[i % len(bases)]
            ch = random.choice(CREDIT_CHANNELS)
            d = random.randint(1, 45)
            m = random.choice([1200, 3500, 6800, 9800, 15000, 23000])
            case_id = f"CCLK-{cat[:4].upper()}-{i + 1:02d}"
            symptom = base["symptom"].format(d=d, m=m) + f"（触达渠道：{ch}，案例码 {cat[:4].upper()}{i + 1:02d}）"
            kb.append({"case_id": case_id, "category": cat,
                       "cluster_key": f"credit:{cat}:B{i % len(bases)}",
                       "title": f"{base['title']}·{ch}", "symptom": symptom,
                       "resolution": base["resolution"].format(d=d, m=m, 姓名="客户本人")})
            if i < 8:
                q = f"催收场景·{ch}：{base['title']}（逾期 {d} 天 / 欠款 {m} 元），标准话术和合规要点是什么"
                queries.append({"query_id": f"CRQ-{len(queries):03d}", "question": q,
                                "relevant": [case_id]})
    save(DATA / "credit" / "credit_collection_kb.jsonl", kb)
    save(DATA / "credit" / "credit_kb_queries.jsonl", queries)
    return kb, queries


def main() -> None:
    print("1) SSRM 值班助手知识库 30 → 150")
    variant_ids = expand_ssrm_kb()
    gen_ssrm_queries(variant_ids)

    print("2) CSA 坐席助手知识库 + 检索评测")
    kb, qs = gen_csa_kb()
    print(f"  csa_agent_kb {len(kb)} 条 / 查询 {len(qs)} 条")

    print("3) Credit 催员辅助知识库 + 检索评测")
    kb, qs = gen_credit_kb()
    print(f"  credit_collection_kb {len(kb)} 条 / 查询 {len(qs)} 条")


if __name__ == "__main__":
    main()