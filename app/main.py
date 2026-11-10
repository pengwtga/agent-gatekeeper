"""SSRM Ops Agent — FastAPI 服务（端口 8001）

端点：
- GET  /health          健康检查（含 LLM 配置状态）
- POST /classify        单条事件根因分类
- POST /eval            数据集评测（LLM vs 规则基线）
- GET  /stats           数据集统计
- POST /assist          值班助手问答
- GET  /playground      根因分类页面（后端定位问题用）
- GET  /assistant       值班助手页面（自然语言检索历史工单）
"""

import ipaddress
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.classifier import ROOT_CAUSE_CLASSES, RootCauseClassifier
from app.config import settings
from app.dataset import load_dataset, stats
from app.llm_client import llm_client
from app.rag_assistant import RagAssistant

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
logger = logging.getLogger(__name__)

classifier = RootCauseClassifier(llm_client)
rag = RagAssistant(llm_client)


@asynccontextmanager
async def lifespan(app: FastAPI):
    providers = ",".join(llm_client.configured_providers) if llm_client.is_configured else "未配置(规则基线降级)"
    logger.info("SSRM Ops Agent started | LLM 容灾链: %s", providers)
    await rag.warmup()
    logger.info("RAG 值班助手就绪 | 模式: %s", rag.mode)
    yield


app = FastAPI(title="SSRM Ops Agent", version="0.1.0", lifespan=lifespan)


# ── 公网防护：服务会消耗 LLM API 配额，加最小成本的防刷措施 ──
RATE_LIMIT_PER_MIN = 20  # 单 IP 每分钟上限（classify/assist）
_RATE_WINDOW_S = 60
_SWEEP_INTERVAL_S = 300  # 定期清理过期 IP，避免字典随扫描器无限增长

_rate_bucket: dict[str, list[float]] = {}
_last_sweep = 0.0


def _is_trusted_proxy(host: str) -> bool:
    """只有对端是本机/内网时才相信它带来的转发头（防公网直连伪造 X-Real-IP 绕过限速）"""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host in ("localhost", "")
    return ip.is_loopback or ip.is_private


def _client_ip(request: Request) -> str:
    """取真实客户端 IP。

    部署形态是 nginx → 后端（后端只绑 127.0.0.1），nginx 已注入 X-Real-IP。
    直接用 request.client.host 拿到的是 nginx 容器 IP，会把「单 IP 20/min」
    退化成「全站 20/min」——一个人刷就把所有人限死。
    """
    peer = request.client.host if request.client else ""
    if _is_trusted_proxy(peer):
        xri = (request.headers.get("x-real-ip") or "").strip()
        if xri:
            return xri
        xff = (request.headers.get("x-forwarded-for") or "").strip()
        if xff:
            return xff.split(",")[0].strip()
    return peer or "unknown"


def _check_rate(request: Request) -> None:
    """按真实客户端 IP 的内存滑动窗口限速（单实例够用；多实例需换 Redis）"""
    global _last_sweep
    now = time.monotonic()

    if now - _last_sweep > _SWEEP_INTERVAL_S:
        for k in [k for k, v in _rate_bucket.items() if not v or now - v[-1] > _RATE_WINDOW_S]:
            _rate_bucket.pop(k, None)
        _last_sweep = now

    ip = _client_ip(request)
    window = [t for t in _rate_bucket.get(ip, []) if now - t < _RATE_WINDOW_S]
    if len(window) >= RATE_LIMIT_PER_MIN:
        _rate_bucket[ip] = window
        logger.warning("[RateLimit] ip=%s 触发限速", ip)
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    window.append(now)
    _rate_bucket[ip] = window


# ════════════════════════════════════════════════════════════════
# 页面共享设计系统：两个页面（根因分类 / 值班助手）统一风格
# ════════════════════════════════════════════════════════════════
PAGE_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f2f4f8;color:#1f2329;min-height:100vh}
.wrap{max-width:920px;margin:0 auto;padding:24px 16px 48px}
header{background:linear-gradient(135deg,#2456e6,#4c8bf5);color:#fff;border-radius:16px;padding:20px 24px;display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;box-shadow:0 4px 16px rgba(36,86,230,.22)}
header h1{font-size:20px;font-weight:600}
header p{font-size:13px;opacity:.92;margin-top:5px;line-height:1.5}
.nav{display:flex;gap:8px;flex-shrink:0}
.nav a{color:#fff;text-decoration:none;font-size:13px;border:1px solid rgba(255,255,255,.55);border-radius:999px;padding:7px 16px;background:rgba(255,255,255,.14)}
.nav a.active{background:#fff;color:#2456e6;border-color:#fff;font-weight:600}
.card{background:#fff;border:1px solid #e8ebf0;border-radius:14px;padding:18px 20px;margin-top:16px;box-shadow:0 1px 4px rgba(31,35,41,.04)}
.tag{display:inline-flex;align-items:center;white-space:nowrap;font-size:12px;border-radius:999px;padding:4px 12px;border:1px solid;line-height:1.4}
.t-mode{color:#0b7285;border-color:#9edbe4;background:#e7f5f7}
.t-llm{color:#5f3dc4;border-color:#cabffa;background:#f3f0ff}
.t-hi{color:#2b8a3e;border-color:#96d7a7;background:#ebfbee}
.t-mid{color:#e8590c;border-color:#ffc9a3;background:#fff4e6}
.t-lo{color:#c92a2a;border-color:#f1a0a0;background:#fff5f5}
.t-cls{color:#2456e6;border-color:#b3ccf7;background:#eef3fe;font-family:Menlo,monospace}
.meta{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.sec{font-size:12.5px;color:#8a929e;font-weight:600;margin:14px 0 8px;letter-spacing:.3px}
.advice{background:#f0f5ff;border-left:3px solid #2456e6;padding:10px 14px;border-radius:0 8px 8px 0;white-space:pre-wrap;font-size:14px;line-height:1.7}
.warn{margin-top:12px;font-size:12.5px;color:#e8590c;background:#fff4e6;border-radius:8px;padding:8px 12px}
.okv{margin-top:12px;font-size:12.5px;color:#2b8a3e;background:#ebfbee;border-radius:8px;padding:8px 12px}
.hint{font-size:12.5px;color:#8a929e}
.typing{color:#8a929e;font-size:13px}
.chips{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:10px;margin-top:16px}
.chips button{display:flex;align-items:center;gap:10px;text-align:left;background:#fff;border:1px solid #dfe3ea;border-radius:12px;padding:14px 16px;font-size:14px;color:#1f2329;cursor:pointer;box-shadow:0 1px 3px rgba(31,35,41,.05);transition:.15s;font-family:inherit}
.chips button:hover{border-color:#2456e6;transform:translateY(-1px);box-shadow:0 4px 10px rgba(36,86,230,.12)}
.chips .ico{font-size:22px;flex-shrink:0}
"""

PLAYGROUND_CSS = """
textarea{width:100%;height:170px;font-family:Menlo,monospace;font-size:13px;padding:12px;border:1px solid #dfe3ea;border-radius:10px;outline:none;resize:vertical;background:#fff;color:#1f2329}
textarea:focus{border-color:#2456e6}
.actions{display:flex;align-items:center;gap:14px;margin-top:12px}
button.primary{background:#2456e6;color:#fff;border:none;border-radius:10px;padding:11px 28px;font-size:14px;font-weight:600;cursor:pointer;font-family:inherit}
button.primary:disabled{opacity:.55;cursor:not-allowed}
.clsname{font-size:17px;font-weight:700;color:#2456e6;margin:4px 0 10px}
.crow{display:flex;align-items:center;gap:10px;margin-bottom:4px}
.rbar{flex:1;height:8px;background:#e8ebf0;border-radius:999px;overflow:hidden}
.rbar i{display:block;height:100%;background:linear-gradient(90deg,#4c8bf5,#2456e6);border-radius:999px}
.rpct{font-weight:700;font-size:15px;color:#2456e6;font-variant-numeric:tabular-nums}
details{margin-top:12px}
summary{cursor:pointer;font-size:12.5px;color:#8a929e}
pre{background:#f8f9fb;border:1px solid #e8ebf0;border-radius:8px;padding:12px;font-size:12px;white-space:pre-wrap;margin-top:8px}
"""

ASSISTANT_CSS = """
.chat{display:flex;flex-direction:column;gap:14px;margin-top:8px}
.msg{display:flex;gap:10px;align-items:flex-start}
.msg.user{flex-direction:row-reverse}
.avatar{width:36px;height:36px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:18px;flex-shrink:0;background:#dfe3e8}
.msg.user .avatar{background:#2456e6}
.bubble{border-radius:14px;padding:14px 18px;font-size:14px;line-height:1.7}
.msg.user .bubble{background:#2456e6;color:#fff;border-top-right-radius:4px;max-width:82%}
.msg.bot .bubble{background:#fff;border:1px solid #e8ebf0;border-top-left-radius:4px;box-shadow:0 1px 4px rgba(31,35,41,.04);flex:1;min-width:0}
.src{display:flex;align-items:center;gap:12px;background:#f8f9fb;border:1px solid #e8ebf0;border-radius:10px;padding:10px 14px;margin:8px 0;font-size:13px;flex-wrap:wrap}
.src .tid{font-weight:600;color:#2456e6;font-family:Menlo,monospace;font-size:12px}
.src .title{flex:1;min-width:120px}
.bar{width:90px;height:6px;background:#e8ebf0;border-radius:999px;overflow:hidden;flex-shrink:0}
.bar i{display:block;height:100%;background:linear-gradient(90deg,#4c8bf5,#2456e6);border-radius:999px}
.pct{font-weight:700;color:#2456e6;width:44px;text-align:right;font-variant-numeric:tabular-nums;flex-shrink:0}
.case{background:#f8f9fb;border:1px solid #e8ebf0;border-radius:10px;padding:12px 14px;margin:8px 0;font-size:13px;line-height:1.8}
.case .tid2{font-family:Menlo,monospace;color:#2456e6}
.case .k{color:#8a929e}
.inputbar{display:flex;gap:10px;margin-top:18px;position:sticky;bottom:12px}
.inputbar input{flex:1;border:1px solid #dfe3ea;border-radius:12px;padding:14px 16px;font-size:14px;outline:none;background:#fff;box-shadow:0 2px 10px rgba(31,35,41,.06);color:#1f2329}
.inputbar input:focus{border-color:#2456e6}
.inputbar button{background:#2456e6;color:#fff;border:none;border-radius:12px;padding:0 28px;font-size:14px;font-weight:600;cursor:pointer;font-family:inherit}
.inputbar button:disabled{opacity:.55;cursor:not-allowed}
"""


def page_shell(title: str, desc: str, active: str, extra_css: str, body: str, script: str) -> str:
    """统一页面骨架：渐变头部 + 互相跳转的导航 + 共享样式"""
    nav = ""
    for key, href, label in (("classify", "/playground", "🔍 根因分类"), ("assistant", "/assistant", "🛎️ 值班助手")):
        cls = ' class="active"' if key == active else ""
        nav += f'<a{cls} href="{href}">{label}</a>'
    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · SSRM 智能运维</title>
<style>{PAGE_CSS}{extra_css}</style>
</head>
<body>
<div class="wrap">
  <header>
    <div><h1>{title}</h1><p>{desc}</p></div>
    <nav class="nav">{nav}</nav>
  </header>
  {body}
</div>
<script>{script}</script>
</body>
</html>"""


# ── 两个页面共用的 JS 工具函数 ──
COMMON_JS = """
const CLS_NAME = {xflow_callback_lost:'审批回调丢失', ea_sync_timeout:'EA 同步超时', duplicate_submit:'重复提交',
  data_inconsistency:'数据不一致', frontend_param_error:'前端参数错误', unknown:'未知（转人工）'};
function esc(s){const d=document.createElement('div');d.textContent=s==null?'':String(s);return d.innerHTML;}
function confTag(c){
  if(c==null) return '';
  const cls=c>=0.85?'t-hi':(c>=0.7?'t-mid':'t-lo');
  return `<span class="tag ${cls}">置信度 ${(c*100).toFixed(0)}%</span>`;
}
"""


class ClassifyRequest(BaseModel):
    incident: dict[str, Any]


class EvalRequest(BaseModel):
    limit: Optional[int] = 30   # 每次评测条数（控制 LLM 成本）


@app.get("/")
async def root():
    return {
        "service": "SSRM Ops Agent",
        "docs": "/docs",
        "endpoints": {
            "POST /classify": "单条事件根因分类（body: {\"incident\": {...}}）",
            "POST /eval": "数据集评测",
            "GET /stats": "数据集统计",
            "POST /assist": "值班助手问答（body: {\"question\": \"...\"}）",
            "GET /kb/stats": "知识库统计",
        },
        "pages": {
            "playground": "/playground",
            "assistant": "/assistant",
        },
    }


@app.get("/playground")
async def playground():
    """根因分类页：给后端值班定位问题用——结构化上下文进，根因+置信度+处置建议出"""
    body = """
<div class="chips">
  <button onclick="fill('callback')"><span class="ico">📮</span><span>案例：审批通过但没生成任务</span></button>
  <button onclick="fill('ea')"><span class="ico">⏱️</span><span>案例：EA 同步一直超时</span></button>
  <button onclick="fill('dup')"><span class="ico">🔁</span><span>案例：重复提交冲突</span></button>
  <button onclick="fill('hard')"><span class="ico">🧩</span><span>案例：口语化困难样本</span></button>
</div>
<div class="card">
  <div class="sec">事件上下文 JSON（可编辑）</div>
  <textarea id="payload" placeholder='{"incident": {...}}'></textarea>
  <div class="actions">
    <button class="primary" id="go" onclick="run()">🔍 开始分类</button>
    <span class="hint">LLM 通道约 5~10 秒 · 字段含义：stage 阶段 / source_system 来源 / wait_minutes 已等待分钟数 / retry_count 重试次数</span>
  </div>
</div>
<div class="card" id="result"><span class="hint">点击上方案例卡片，或粘贴自己的 incident JSON，然后点「开始分类」</span></div>
"""
    script = COMMON_JS + """
const CASES = {
  callback: {"stage":"register","source_system":"xflow","error_code":null,"error_message":"审批单 FT20261025088 状态为 APPROVED 但 SSRM 无对应注册任务","wait_minutes":55,"retry_count":0,"downstream":"ssrm","http_status":null},
  ea: {"stage":"change","source_system":"ssrm","error_code":"EA_TIMEOUT","error_message":"调用 EA updateSupplier 超时 5s","wait_minutes":3,"retry_count":4,"downstream":"ea","http_status":504},
  dup: {"stage":"register","source_system":"dora","error_code":null,"error_message":"同一供应商 10 秒内提交了两次注册，第二次提示申请已存在","wait_minutes":0,"retry_count":0,"downstream":"ssrm","http_status":409},
  hard: {"stage":"register","source_system":"portal","error_code":"JOB_STUCK","error_message":"用户反馈提交后页面一直显示审批中，但后台查审批记录其实早已通过","wait_minutes":95,"retry_count":0,"downstream":"ssrm","http_status":null}
};
function fill(k){ document.getElementById('payload').value = JSON.stringify({"incident": CASES[k]}, null, 2); }

async function run(){
  const btn = document.getElementById('go'), box = document.getElementById('result');
  if(btn.disabled) return;
  let payload;
  try { payload = JSON.parse(document.getElementById('payload').value); }
  catch(e){ box.innerHTML = '<span style="color:#c92a2a">JSON 解析失败：'+esc(e.message)+'</span>'; return; }
  btn.disabled = true;
  box.innerHTML = '<span class="typing">分类中…（LLM 通道约 5~10 秒）</span>';
  try {
    const resp = await fetch('/classify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const d = await resp.json();
    render(d);
  } catch(e){ box.innerHTML = '<span style="color:#c92a2a">请求失败：'+esc(e.message||e)+'</span>'; }
  finally { btn.disabled = false; }
}

function render(d){
  const box = document.getElementById('result');
  if(d.detail){ box.innerHTML = '<span style="color:#c92a2a">参数错误：'+esc(JSON.stringify(d.detail))+'</span>'; return; }
  const c = d.confidence ?? 0;
  const cls = d.root_cause_class || 'unknown';
  const srcTag = d.source === 'llm'
    ? `<span class="tag t-llm">通道: LLM · ${esc(d.llm_provider||'?')}</span>`
    : '<span class="tag t-mode">通道: 规则基线</span>';
  const review = (d.needs_human_review ?? (c < 0.85))
    ? '<div class="warn">⚠️ 置信度低于自动处置门禁（0.85），建议人工确认后再执行处置</div>'
    : '<div class="okv">✅ 置信度达到自动处置门禁（0.85），可按建议执行处置</div>';
  box.innerHTML = `
    <div class="meta">
      <span class="tag t-cls">${esc(cls)}</span>
      ${srcTag}
      ${confTag(c)}
    </div>
    <div class="clsname">${CLS_NAME[cls]||esc(cls)}</div>
    <div class="crow"><div class="rbar"><i style="width:${(c*100).toFixed(0)}%"></i></div><span class="rpct">${(c*100).toFixed(0)}%</span></div>
    <div class="sec">🔍 判断依据</div>
    <div class="advice">${esc(d.evidence||'（无）')}</div>
    <div class="sec">💡 处置建议</div>
    <div class="advice">${esc(d.suggested_action||'（无）')}</div>
    ${review}
    <details><summary>查看原始 JSON</summary><pre>${esc(JSON.stringify(d,null,2))}</pre></details>`;
}

// ?demo=1 自动跑一个预设案例（README 截图 / 面试演示用）
if (new URLSearchParams(location.search).has('demo')){ fill('callback'); run(); }
"""
    return HTMLResponse(page_shell(
        "🔍 根因分类",
        "给后端值班定位问题用的：输入卡单/失败事件的结构化上下文，输出根因类别、置信度、判断依据与处置建议",
        "classify", PLAYGROUND_CSS, body, script))


@app.get("/assistant")
async def assistant_page():
    """值班助手页：自然语言描述问题，检索相似历史工单并给出处置建议"""
    body = """
<div class="chips">
  <button onclick="askPreset('审批已经通过了，但 SSRM 里一直没生成注册任务，等了一个小时了')"><span class="ico">📮</span><span>审批通过但没生成任务</span></button>
  <button onclick="askPreset('调用 EA 的 updateSupplier 接口一直超时，重试了 4 次还是失败')"><span class="ico">⏱️</span><span>EA 同步一直失败</span></button>
  <button onclick="askPreset('供应商说提交注册时报错申请已存在，怀疑是重复提交')"><span class="ico">🔁</span><span>怀疑重复提交</span></button>
  <button onclick="askPreset('在 Dora 上查不到这个供应商的联系人信息')"><span class="ico">🔎</span><span>Dora 查不到联系人</span></button>
</div>

<div class="chat" id="chat">
  <div class="msg bot">
    <div class="avatar">🤖</div>
    <div class="bubble">你好！我是 SSRM 值班助手。遇到线上问题直接用一句话描述（也可以点上面的案例卡片），我会：<br>1️⃣ 检索最相似的历史工单<br>2️⃣ 结合 LLM 给出处置建议<br><span class="hint">置信度低于 55% 时会明确提示转人工，不硬答。</span></div>
  </div>
</div>

<div class="inputbar">
  <input id="q" placeholder="输入你遇到的问题，回车发送…" onkeydown="if(event.key==='Enter')send()">
  <button id="sendBtn" onclick="send()">发送</button>
</div>
"""
    script = COMMON_JS + """
const chat = document.getElementById('chat');
const input = document.getElementById('q');
const sendBtn = document.getElementById('sendBtn');

fetch('/kb/stats').then(r=>r.json()).then(d=>{
  const b = document.createElement('span');
  b.className = 'tag t-mode';
  const modeName = d.mode === 'vector' ? '向量检索' : (d.mode === 'keyword+rerank' ? '关键词+LLM重排' : d.mode);
  b.textContent = `📚 知识库 ${d.tickets} 条 · ${modeName}`;
  document.querySelector('header > div').appendChild(b);
  b.style.marginLeft = '10px';
  b.style.marginTop = '8px';
}).catch(()=>{});

function addUser(text){
  const div = document.createElement('div');
  div.className = 'msg user';
  div.innerHTML = `<div class="avatar">🧑‍💻</div><div class="bubble">${esc(text)}</div>`;
  chat.appendChild(div);
}
function addBotHTML(html){
  const div = document.createElement('div');
  div.className = 'msg bot';
  div.innerHTML = `<div class="avatar">🤖</div><div class="bubble">${html}</div>`;
  chat.appendChild(div);
  div.scrollIntoView({behavior:'smooth', block:'end'});
  return div.querySelector('.bubble');
}

async function askPreset(q){ input.value = q; send(); }

async function send(){
  const q = input.value.trim();
  if (!q || sendBtn.disabled) return;
  input.value = '';
  sendBtn.disabled = true;
  addUser(q);
  const t = addBotHTML('<span class="typing">正在检索历史工单并生成建议…（约 5~10 秒）</span>');
  try {
    const resp = await fetch('/assist', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({question: q})});
    const d = await resp.json();
    render(t, d);
  } catch(e){
    t.innerHTML = `<span style="color:#c92a2a">请求失败：${esc(e.message || e)}</span>`;
  } finally {
    sendBtn.disabled = false;
  }
}

function render(t, d){
  if (d.error){ t.innerHTML = `<span style="color:#c92a2a">${esc(d.error)}</span>`; return; }

  // /assist 返回：answer(字符串) / confidence(顶层) / mode / llm_provider / sources / cases(LLM降级)
  const modeName = d.mode === 'vector' ? '向量检索' : (d.mode === 'keyword+rerank' ? '关键词+LLM重排' : (d.mode||'-'));
  const meta = `<div class="meta">
    <span class="tag t-mode">检索: ${esc(modeName)}</span>
    ${d.llm_provider ? `<span class="tag t-llm">LLM: ${esc(d.llm_provider)}</span>` : ''}
    ${confTag(d.confidence)}
  </div>`;

  const answerText = typeof d.answer === 'string' ? d.answer : '（无建议）';
  let html = meta
    + '<div class="sec">💡 处置建议</div>'
    + `<div class="advice">${esc(answerText)}</div>`;
  if (d.confidence != null && d.confidence < 0.55){
    html += '<div class="warn">⚠️ 相似度低于采信阈值（0.55），以上建议仅供参考，请人工判断。</div>';
  }

  if (d.cases && d.cases.length){
    // LLM 降级模式：返回完整 case（含症状与处置过程）
    html += `<div class="sec">📋 相似历史工单（LLM 暂不可用，返回原始 case）</div>` + d.cases.map(c =>
      `<div class="case"><b class="tid2">${esc(c.ticket_id)}</b> · ${esc(c.title)}<br>` +
      `<span class="k">现象：</span>${esc(c.symptom)}<br>` +
      `<span class="k">处置：</span>${esc(c.resolution)}</div>`).join('');
  } else if (d.sources && d.sources.length){
    const hasDetail = !!d.sources[0].title;
    html += `<div class="sec">📋 相似历史工单 Top ${d.sources.length}</div>` + d.sources.map(s=>{
      const pct = Math.round((s.score||0)*100);
      return `<div class="src"><span class="tid">${esc(s.ticket_id)}</span>` +
        (hasDetail ? `<span class="title">${esc(s.title)}</span><span class="tag t-cls">${esc(s.root_cause_class)}</span>` : '') +
        `<div class="bar"><i style="width:${pct}%"></i></div><span class="pct">${pct}%</span></div>`;
    }).join('');
  } else {
    html += '<div class="hint" style="margin-top:10px">（未检索到相似工单，建议升级人工并把 case 沉淀进知识库）</div>';
  }

  t.innerHTML = html;
}

// ?demo=1 自动跑一个预设问题（README 截图 / 面试演示用）
if (new URLSearchParams(location.search).has('demo')) askPreset('审批已经通过了，但 SSRM 里一直没生成注册任务，等了一个小时了');
"""
    return HTMLResponse(page_shell(
        "🛎️ SSRM 值班助手",
        "值班 oncall 时用一句话描述问题，自动检索相似历史工单并给出处置建议",
        "assistant", ASSISTANT_CSS, body, script))


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "llm_configured": llm_client.is_configured,
        "llm_providers": llm_client.configured_providers,
        "llm_usage": llm_client.usage,   # 进程累计 token 用量，成本可见的最小实现
        "classes": list(ROOT_CAUSE_CLASSES.keys()),
    }


@app.post("/classify")
async def classify(req: ClassifyRequest, request: Request):
    _check_rate(request)
    result = await classifier.classify(req.incident)
    return result


@app.post("/eval")
async def eval_endpoint(req: EvalRequest, request: Request):
    """数据集评测：逐条分类并与标注对比，输出总体准确率、分类别准确率、混淆对。

    防刷：一次评测会循环调用 LLM 上限条数次，公网环境要求 token
    （用法：POST /eval?token=... 或请求头 X-Eval-Token；token 配置于 .env 的 SSRM_AIOPS_EVAL_TOKEN）。
    """
    token = request.query_params.get("token") or request.headers.get("x-eval-token")
    if not settings.eval_token or token != settings.eval_token:
        raise HTTPException(status_code=403, detail="评测接口需要有效 token（防公网刷量消耗 LLM 配额）")
    _check_rate(request)
    rows = load_dataset(limit=req.limit, shuffle=True)
    correct = 0
    per_class: dict[str, dict[str, int]] = {}
    confusions: list[dict[str, str]] = []
    latencies: list[float] = []
    sources: dict[str, int] = {}

    for row in rows:
        t0 = time.monotonic()
        result = await classifier.classify(row["incident"])
        latencies.append((time.monotonic() - t0) * 1000)
        sources[result.get("source", "?")] = sources.get(result.get("source", "?"), 0) + 1

        pred = result.get("root_cause_class")
        label = row["label"]
        per_class.setdefault(label, {"total": 0, "correct": 0})
        per_class[label]["total"] += 1
        if pred == label:
            correct += 1
            per_class[label]["correct"] += 1
        else:
            confusions.append({"label": label, "predicted": pred, "incident_id": row["incident"]["incident_id"]})

    total = len(rows)
    return {
        "total": total,
        "accuracy": round(correct / total, 4) if total else 0,
        "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0,
        "source_channels": sources,
        "per_class": {
            k: {"accuracy": round(v["correct"] / v["total"], 3), **v}
            for k, v in per_class.items()
        },
        "confusions": confusions[:10],
    }


@app.get("/stats")
async def dataset_stats():
    return stats()


class AssistRequest(BaseModel):
    question: str


@app.post("/assist")
async def assist(req: AssistRequest, request: Request):
    """值班助手：描述你遇到的问题，返回相似历史工单和处置建议"""
    _check_rate(request)
    return await rag.assist(req.question)


@app.get("/kb/stats")
async def kb_stats():
    from collections import Counter
    c = Counter(d["root_cause_class"] for d in rag.docs)
    return {"tickets": len(rag.docs), "by_class": dict(c), "mode": rag.mode, "ready": rag.ready}
