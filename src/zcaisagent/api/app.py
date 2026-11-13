from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from zcaisagent.api.deps import get_container
from zcaisagent.api.routes import admin_router, chat_router, n2sql_router, runs_router
from zcaisagent.domain.errors import ZCAISAgentError
from zcaisagent.ports.health import HealthStatus

_CHAT_PAGE = Path(__file__).parent / "static" / "chat.html"
_STUDIO_PAGE = Path(__file__).parent / "static" / "studio.html"

# 状态 → 徽章配色与中文标签
_HEALTH_BADGE = {
    HealthStatus.OK: ("ok", "正常"),
    HealthStatus.DEGRADED: ("warn", "降级"),
    HealthStatus.DOWN: ("bad", "故障"),
    HealthStatus.UNKNOWN: ("muted", "未知"),
}

# 与 chat.html / studio.html 一致的 🤖 favicon（内联 SVG，无需额外请求）。
_FAVICON = (
    "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' "
    "viewBox='0 0 100 100'><text y='.9em' font-size='90'>🤖</text></svg>"
)

_HEALTH_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="5">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="__FAVICON__">
<title>zCAISAgent · 健康检查</title>
<style>
  :root {
    --paper: #0f110e; --panel: #151812; --panel-2: #1b1f18; --line: #2a2e26;
    --ink: #e9e7dc; --muted: #97998c; --gold: #d8a657;
    --ok: #8fbf6a; --warn: #d8a657; --bad: #d8735f;
    --mono: "Cascadia Mono", Consolas, monospace;
    --sans: "PingFang SC", "Microsoft YaHei", system-ui, sans-serif;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; background: var(--paper); color: var(--ink);
    font-family: var(--sans); -webkit-font-smoothing: antialiased;
    display: flex; align-items: center; justify-content: center; padding: 32px 16px;
  }
  .card {
    width: min(760px, 100%); background: var(--panel);
    border: 1px solid var(--line); border-radius: 6px; padding: 28px 30px 24px;
  }
  .head { display: flex; align-items: baseline; justify-content: space-between; gap: 16px; }
  .head h1 { margin: 0; font-size: 19px; letter-spacing: .01em; }
  .head .sub { color: var(--muted); font-size: 12px; font-family: var(--mono); }
  .banner {
    margin: 18px 0 20px; padding: 12px 16px; border-radius: 4px;
    border: 1px solid var(--line); font-size: 14px;
    display: flex; align-items: center; justify-content: space-between;
  }
  .banner.ok { border-color: rgba(143,191,106,.45); background: rgba(143,191,106,.08);
               color: var(--ok); }
  .banner.bad { border-color: rgba(216,115,95,.45); background: rgba(216,115,95,.08);
                color: var(--bad); }
  .banner small { color: var(--muted); font-weight: normal; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; color: var(--muted); font-weight: 500; font-size: 11px;
       letter-spacing: .08em; text-transform: uppercase;
       padding: 8px 10px; border-bottom: 1px solid var(--line); }
  td { padding: 9px 10px; border-bottom: 1px solid var(--line); }
  tr:last-child td { border-bottom: 0; }
  td.mono { font-family: var(--mono); font-size: 12px; }
  td.msg { color: var(--muted); font-size: 12px; max-width: 260px; word-break: break-all; }
  .badge { display: inline-block; padding: 2px 10px; border-radius: 999px;
           border: 1px solid var(--line); font-size: 11.5px; }
  .badge.ok { color: var(--ok); border-color: rgba(143,191,106,.45); }
  .badge.warn { color: var(--warn); border-color: rgba(216,166,87,.45); }
  .badge.bad { color: var(--bad); border-color: rgba(216,115,95,.45); }
  .badge.muted { color: var(--muted); }
  .foot { margin-top: 18px; color: var(--muted); font-size: 11.5px;
          display: flex; justify-content: space-between; gap: 12px; }
  .foot a { color: var(--gold); text-decoration: none; }
  .foot a:hover { text-decoration: underline; }
</style>
</head>
<body>
  <div class="card">
    <div class="head">
      <h1>zCAISAgent · 组件健康</h1>
      <span class="sub">刷新于 __TIME__</span>
    </div>
    <div class="banner __OVERALL__">
      <span>__STATUS_TEXT__</span>
      <small>每 5 秒自动刷新 · curl /health 可获取 JSON</small>
    </div>
    <table>
      <thead>
        <tr><th>组件</th><th>状态</th><th>消息</th><th>更新时间</th></tr>
      </thead>
      <tbody>__ROWS__</tbody>
    </table>
    <div class="foot">
      <a href="/studio">Studio 运营台 ↗</a>
      <a href="/">对话控制台 ↗</a>
    </div>
  </div>
</body>
</html>
"""


def _health_payload() -> dict:
    container = get_container()
    entries = container.health.snapshot()
    cache = container.cache
    if cache is not None and getattr(cache, "is_degraded", False):
        # Redis 故障已回退内存缓存：健康板同步标记 DOWN。
        message = getattr(cache, "last_error", "") or "cache degraded"
        for entry in entries:
            if entry.name == "cache":
                entry.status = HealthStatus.DOWN
                entry.message = message
    healthy = all(entry.status == HealthStatus.OK for entry in entries)
    return {
        "status": "ok" if healthy else "degraded",
        "service": "zcaisagent",
        "entries": [entry.model_dump() for entry in entries],
    }


def _render_health_page(payload: dict) -> str:
    def badge(status: str) -> str:
        css, label = _HEALTH_BADGE.get(status, ("muted", status))
        return f'<span class="badge {css}">{label}</span>'

    def stamp(ts: float) -> str:
        if not ts:
            return "—"
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

    rows = []
    for entry in payload["entries"]:
        rows.append(
            "<tr>"
            f'<td class="mono">{entry["name"]}</td>'
            f"<td>{badge(entry['status'])}</td>"
            f'<td class="msg">{entry["message"] or "—"}</td>'
            f'<td class="mono">{stamp(entry["updated_at"])}</td>'
            "</tr>"
        )
    degraded = payload["status"] != "ok"
    return (
        _HEALTH_PAGE.replace("__FAVICON__", _FAVICON)
        .replace("__TIME__", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        .replace("__ROWS__", "\n".join(rows))
        .replace("__OVERALL__", "bad" if degraded else "ok")
        .replace(
            "__STATUS_TEXT__",
            "服务降级 · 存在故障组件" if degraded else "服务正常 · 全部组件健康",
        )
    )


def create_app() -> FastAPI:
    app = FastAPI(title="zCAISAgent", version="0.1.0")
    app.include_router(chat_router, prefix="/v1", tags=["chat"])
    app.include_router(runs_router, prefix="/v1", tags=["runs"])
    app.include_router(n2sql_router, prefix="/v1", tags=["n2sql"])
    app.include_router(admin_router, prefix="/v1", tags=["admin"])

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(_CHAT_PAGE, media_type="text/html")

    @app.get("/studio", include_in_schema=False)
    async def studio() -> FileResponse:
        return FileResponse(_STUDIO_PAGE, media_type="text/html")

    @app.exception_handler(ZCAISAgentError)
    async def zcaisagent_error_handler(request: Request, exc: ZCAISAgentError):
        status_code = 401 if exc.code == "UNAUTHORIZED" else 400
        return JSONResponse(
            status_code=status_code,
            content={"code": exc.code, "message": exc.message},
        )

    @app.get("/health", include_in_schema=False)
    async def health(request: Request):
        payload = _health_payload()
        # 浏览器（Accept 含 text/html）返回可视化页面；curl / Docker
        # healthcheck / 监控等机器客户端仍拿 JSON 契约。
        if "text/html" in request.headers.get("accept", ""):
            return HTMLResponse(_render_health_page(payload))
        return payload

    return app


app = create_app()
