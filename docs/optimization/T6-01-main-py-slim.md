# T6-1 main.py 瘦身：340 行页面代码抽出去

## 1. 问题

`app/main.py` 593 行，其中 **340 行是 HTML / CSS / JS 字符串**：

| 块 | 行数 | 内容 |
|---|---|---|
| `PAGE_CSS` / `PLAYGROUND_CSS` / `ASSISTANT_CSS` | ~70 | 三段 CSS 字符串 |
| `page_shell()` | ~25 | 拼整页 HTML |
| `COMMON_JS` | ~10 | 两个页面共用的 JS 工具函数 |
| `/playground` handler | ~80 | 页面 body + 190 行 script |
| `/assistant` handler | ~120 | 同上 |

后果是打开 `main.py` 的人，翻两屏还没看到第二个接口。
路由、限速、预算、门禁这些**真正的后端逻辑被埋在 CSS 字符串下面**。

## 2. 判断

这些页面是 React 前端（`frontend/`）之前的那一版，功能已被 React 全量覆盖。
所以有两个选项：**抽走** 或 **删掉**。

先确认一个事实：**线上它们根本不可达**。

```nginx
location / { try_files $uri $uri/ /index.html; }   # frontend/nginx.conf
```

`/playground` 请求进 nginx 后命中 `try_files` 的最后一个分支，拿到的是
React 的 `index.html`，交给前端路由处理 —— 请求根本不会到后端。
只有直连 `127.0.0.1:8002`（docker-compose 里后端那个只绑本机的映射）时才能访问。

**选择抽走而不是删掉**，理由是：只构建 api 镜像、没有前端产物时，这两个页面是唯一能用的
UI。删掉是不可逆的功能删除，抽走只花 10 分钟且保留了这条路。

> 但这属于「保守选择」：如果确认不需要兜底，删掉更干净。
> 删除成本是 **1 个文件 + 1 行 `include_router`**，没有其它引用。

## 3. 做法

新增 `app/pages_legacy.py`：

```python
router = APIRouter()

PAGE_CSS = """..."""
def page_shell(...): ...
COMMON_JS = """..."""

@router.get("/playground")
async def playground(): ...
```

`main.py` 里只剩两行：

```python
from app.pages_legacy import router as legacy_pages
app.include_router(legacy_pages)
```

**结果：593 → 273 行**，`main.py` 现在从上往下依次是
导入 → lifespan → 中间件 → 限速 → 数据模型 → 6 个业务端点，一屏能看完结构。

## 4. 搬运时容易漏的两处

1. **装饰器要换**：`@app.get("/playground")` → `@router.get("/playground")`。
   漏了会直接 `NameError: name 'app' is not defined`（新模块里没有 `app`）。
2. **`HTMLResponse` 的导入要跟着走**：`main.py` 搬完不再用它，留着就是未使用导入；
   而新模块必须导入它，否则 handler 返回时 NameError。

搬运后立刻 `ast.parse` 全部改动文件，比启动服务看 traceback 快得多。

## 5. 验收

```bash
curl -o /dev/null -w "%{http_code}\n" localhost:8010/playground   # 200
curl -o /dev/null -w "%{http_code}\n" localhost:8010/assistant    # 200
curl -s localhost:8010/ | jq .pages                               # 两个页面仍在 pages 里
```

页面内容与搬运前一致（同一份 CSS 字符串，逐字节未改）。

## 6. 边界

- 这两个页面**不再演进**：导航里只有两项，没有「可观测」入口。改 UI 一律改 React；
- 它们和 React 版同路径（`/playground`），在 nginx 后面永远打不开，容易让人误以为线上用的是它 ——
  所以文件头注释写明了这层关系；
- `COMMON_JS` 里的 `CLS_NAME` 和 React 的 `components/Tags.jsx` 是两份重复的词表，
  改分类名要同时改两处（又一个该删的理由）。
