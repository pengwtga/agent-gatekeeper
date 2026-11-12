"""旧版服务端渲染页面（legacy）

这两个页面是 React 前端（frontend/）之前的那一版，功能已被 React 全量覆盖：

- 线上走 nginx 反代时**根本到不了**这两个路由 —— `location /` 只服务静态资源，
  `/playground` 会被 `try_files` 兜给 index.html，交给前端路由；
- 但直连 `127.0.0.1:8002`（后端 Swagger 那个映射）时仍然可用，
  只构建了 api 镜像、没有前端产物时算一个兜底。

从 main.py 抽出来的理由：340 行 HTML/CSS/JS 字符串把路由淹没了。
打开 main.py 的人第一眼应该看到「有哪几个接口」，而不是三段 CSS。

要删的话：删掉本文件 + main.py 里 `include_router` 那一行即可，没有其它引用。
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

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


@router.get("/playground")
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
  const review = (!!d.needs_human_review)
    ? '<div class="warn">⚠️ 置信度未达自动处置门禁，建议人工确认后再执行处置</div>'
    : '<div class="okv">✅ 置信度达到自动处置门禁，可按建议执行处置</div>';
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


@router.get("/assistant")
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


