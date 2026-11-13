"""CSA 电商客服分类评测集扩充：30 条手工金样本 + 120 条参数化合成 = 150 条（固定种子）

约束与 csa/rules.py 的分支顺序严格对齐（这是生成器最容易被忽略的正确性来源）：
1. account_security：文本含账号关键字（优先级最高，order_status 任意）；
2. presale_consult 一部分：order_status=none → 无论文本是什么都是售前（无售后客体）；
3. refund_dispute：状态非 none + 退款词（可在物流词之前胜出，如"退货的物流"）；
4. invoice_request：非 none + 发票词，且不带退款/账号词；
5. logistics_delay：非 none + 物流词或 shipped；
6. presale_consult 其余：非 none 也无上述词 + 疑问句特征。

幂等：CSG-* 生成物重刷，CSA-* 手工样本保留。
"""

import json
import random
from pathlib import Path

random.seed(20261113)
DATA = Path(__file__).resolve().parents[1] / "data" / "csa"
FILE = DATA / "csa_tickets.jsonl"

CHANNELS = ["chat", "phone", "app"]
PRODUCTS = ["蓝牙耳机", "羽绒服", "保温杯", "电动牙刷", "榨汁机", "帆布鞋", "加湿器", "筋膜枪"]

POOLS: dict[str, list[str]] = {
    "account_security": [
        "我的账号好像被盗了，有人在异地登录还下了单",
        "收不到验证码，登录不上去了，里面还有余额",
        "账号被冻结了，说我有敏感操作，可我什么都没干",
        "想改绑手机号，旧号码早就不用了",
        "有人用我的账号下单还改了收货地址",
        "莫名其妙收到密码重置短信，我根本没操作",
        "我的账号被人改了密码，现在登录不上",
    ],
    "refund_dispute": [
        "申请退款{d}天了钱还没到账，{m}块呢",
        "买的{p}有质量问题，我要退货退款",
        "退款说三天到账，现在第七天了还没到",
        "要求仅退款，{p}用了两次就坏了",
        "退货的物流显示商家签收了，退款还没处理",
        "退钱太慢了，再不处理就投诉",
        "退款一直没到账，银行那边说没收到这笔",
    ],
    "invoice_request": [
        "订单{m}块，帮我开一张电子发票，抬头写公司名",
        "上次买的{p}忘了开票，现在能补开吗",
        "发票抬头写错了，能作废重开吗",
        "需要增值税专用发票，公司报销用",
        "开票金额是不是按优惠后的价格算",
        "补寄发票多久能收到",
    ],
    "logistics_delay": [
        "买的{p}物流{d}天没动了，是不是丢件了",
        "快递显示停滞在一个地方好几天，催一下",
        "订单显示已发货但一直没有送达记录",
        "收到的{p}发错颜色了，要换货怎么寄回",
        "快递到了我拒收了，现在包裹退回到哪了",
        "物流信息三天没更新，客服帮我查查",
    ],
    "presale_consult": [
        "{p}什么时候发货啊，急用",
        "这个{p}支持七天无理由退货吗",
        "有没有优惠活动，现在买还是等一等",
        "{p}的尺码怎么选，我{h}cm 应该穿哪个码",
        "还没下单想问下，{p}的材质是什么",
        "能开发票吗，报销用的",
        "这个{p}质保多久，坏了怎么办",
    ],
}
# presale 专用：order_status=none 的样本（文案可与上面复用，状态决定分支）
PRESALE_NONE = [
    "还没买，问下{p}支持退货吗",
    "什么时候发货？我想尽快收到",
    "{p}有没有活动价，多少能拿下",
]

Q_WORDS = ["吗", "什么时候", "有没有", "支持", "多少", "能不能", "可以"]


def _row(i: int, cls: str, text: str, status: str) -> dict:
    return {
        "incident": {
            "ticket_id": f"CSG-{i:03d}",
            "channel": random.choice(CHANNELS),
            "text": text,
            "order_status": status,
            "order_amount": round(random.uniform(29, 899), 1),
            "user_tenure_days": random.randint(1, 900),
            "hour": random.randint(8, 23),
        },
        "label": cls,
        "expected_action": "按客服 SOP 处理并留痕",
    }


def main() -> None:
    rows = [r for r in (json.loads(l) for l in FILE.open(encoding="utf-8") if l.strip())
            if r["incident"]["ticket_id"].startswith("CSA-")]
    generated = []
    i = 100
    per_class = 24
    classes = ["account_security", "refund_dispute", "invoice_request", "logistics_delay", "presale_consult"]
    for cls in classes:
        for k in range(per_class):
            p = random.choice(PRODUCTS)
            d, m, h = random.randint(3, 12), random.choice([59, 129, 259, 399]), random.randint(150, 185)
            if cls == "presale_consult":
                # 售前咨询一律 order_status=none：还没有订单就没有售后客体，
                # 这与规则第二分支（none → presale）的业务语义严格一致
                text = random.choice(POOLS[cls]).format(p=p, d=d, m=m, h=h)
                status = "none"
            else:
                text = random.choice(POOLS[cls]).format(p=p, d=d, m=m, h=h)
                status = random.choice(["paid", "shipped", "delivered", "refunding"]) if cls != "account_security" \
                    else random.choice(["paid", "shipped", "none", "delivered"])
            generated.append(_row(i, cls, text, status))
            i += 1
    with FILE.open("w", encoding="utf-8") as f:
        for r in rows + generated:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"csa_tickets.jsonl: {len(rows)} 手工 + {len(generated)} 生成 = {len(rows) + len(generated)}")


if __name__ == "__main__":
    main()