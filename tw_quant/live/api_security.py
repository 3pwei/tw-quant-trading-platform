from __future__ import annotations

from fastapi import Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from ..auth import AccessTokenError, AuthorizationError
from .api_context import ApiDependencies
from .rate_limit import RateLimitDecision


def required_permission(method: str, path: str) -> str | None:
    """Map HTTP resources to permissions; unknown API routes fail closed."""
    if path in {"/api/demo/cases", "/api/demo/backtests"}:
        return "demo.run"
    if path in {"/api/me", "/api/access-requests"}:
        return None
    if path == "/api/admin/health":
        return "admin.providers.read"
    if path == "/api/admin/audit":
        return "audit.read"
    if path.startswith(("/api/admin/users", "/api/admin/access-requests")):
        return "admin.users.manage"
    if path in {"/api/health", "/api/kbars", "/api/strategy-signals"}:
        return "market.read"
    if path.startswith("/api/replay"):
        return "backtest.run"
    if path.startswith("/api/backtest-runs"):
        if method == "DELETE":
            return "backtest_history.delete.own"
        if method == "POST":
            return "backtest.run"
        return "backtest_history.read.own"
    if path.startswith("/api/backtest") or path == "/api/composite-backtest":
        return "backtest.run"
    if path.startswith("/api/live-shadow"):
        return "strategy.read.own" if method == "GET" else "__deny_unknown_api__"
    if path == "/api/live-auto" and method == "GET":
        return "orders.live.auto.read"
    if path == "/api/live/canary" and method == "GET":
        return "orders.live.read"
    if path == "/api/live/canary/arm":
        return "orders.live.arm"
    if path == "/api/live/canary/kill-switch" and method == "POST":
        # This endpoint includes emergency FLATTEN, so the stronger close
        # permission is required for every action on the shared route.
        return "orders.live.close"
    if path == "/api/live/orders" and method == "POST":
        return "orders.live.manual"
    if path.startswith("/api/live/orders/") and path.endswith("/cancel"):
        return "orders.live.cancel" if method == "POST" else "__deny_unknown_api__"
    if path == "/api/live/position/close" and method == "POST":
        return "orders.live.close"
    if path.startswith("/api/trading-runtimes"):
        if path.endswith("/live-auto/arm"):
            return "orders.live.auto.arm"
        if path.endswith("/live-auto/disarm"):
            return "orders.live.auto.pause"
        if path.endswith("/live-auto/stop"):
            return "orders.live.auto.stop"
        if "/live-auto" in path:
            return "orders.live.auto.read"
        if method == "POST" and path.endswith("/arm"):
            return "orders.paper"
        return "strategy.read.own" if method == "GET" else "strategy.write.own"
    if path.startswith(("/api/strategies", "/api/composite-strateg")):
        return "strategy.read.own" if method == "GET" else "strategy.write.own"
    if path.startswith("/api/paper/orders"):
        return "orders.paper" if method == "POST" else "positions.read.own"
    if path.startswith("/api/paper/kill-switch"):
        return "orders.paper"
    if path.startswith("/api/paper"):
        return "positions.read.own"
    if path.startswith("/api/"):
        return "__deny_unknown_api__"
    return None


def page_permission(path: str) -> str | None:
    if path.startswith("/trade"):
        return "market.read"
    if path.startswith("/paper"):
        return "positions.read.own"
    if path.startswith("/settings"):
        return "admin.settings.read"
    if path.startswith("/admin"):
        return "admin.users.manage"
    if path.startswith("/docs") or path == "/openapi.json":
        return "admin.settings.read"
    return None


def rate_limit_scope(method: str, path: str) -> str | None:
    if method == "POST" and path == "/api/access-requests":
        return "access_requests"
    if (
        (method == "GET" and path in {"/api/backtest", "/api/composite-backtest"})
        or (method == "POST" and path == "/api/backtest-runs")
    ):
        return "backtests"
    if method == "POST" and path == "/api/replay/prepare":
        return "replay_prepares"
    if method == "POST" and (
        path == "/api/paper/orders"
        or (path.startswith("/api/replay/sessions/") and path.endswith("/orders"))
    ):
        return "orders"
    if path.startswith("/api/live/") and method in {"POST", "DELETE"}:
        return "live_orders"
    return None


def rate_limit_headers(decision: RateLimitDecision) -> dict[str, str]:
    headers = {
        "X-RateLimit-Limit": str(decision.limit),
        "X-RateLimit-Remaining": str(decision.remaining),
        "X-RateLimit-Reset": str(decision.reset_after),
    }
    if not decision.allowed:
        headers["Retry-After"] = str(decision.retry_after)
    return headers


def authorization_denied_response(
    original_uri: str, error: AuthorizationError
) -> Response:
    detail = str(error)
    if original_uri.startswith(("/api/", "/ws/")):
        return JSONResponse(
            {"detail": detail}, status_code=403,
            headers={"Cache-Control": "no-store"},
        )
    request_action = ""
    if "not registered" in detail:
        title = "帳號尚未開通"
        message = "Email 已完成驗證，但尚未列入平台使用者名單。"
        request_action = """
    <button id="request-access" type="button">申請開通</button>
    <p id="request-status" role="status"></p>
    <script>
      const button = document.getElementById("request-access");
      const status = document.getElementById("request-status");
      button.addEventListener("click", async () => {
        button.disabled = true;
        status.textContent = "正在送出申請…";
        try {
          const response = await fetch("/api/access-requests", {
            method: "POST", headers: {"Content-Type": "application/json"}
          });
          const body = await response.json();
          if (!response.ok) throw new Error(body.detail || "申請送出失敗");
          button.hidden = true;
          status.className = "success";
          status.textContent = "申請已送出，請等待平台管理員審核。";
        } catch (error) {
          button.disabled = false;
          status.className = "error";
          status.textContent = error instanceof Error
            ? error.message : "申請送出失敗，請稍後再試。";
        }
      });
    </script>
"""
    elif "suspended" in detail or "revoked" in detail:
        title = "帳號目前無法使用"
        message = "此帳號已被暫停或撤銷，請聯絡平台管理員。"
    else:
        title = "你沒有此頁面的權限"
        message = "帳號已登入，但目前角色不允許使用這項管理功能。"
    return HTMLResponse(
        f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>{title}｜Wade Quant Lab</title>
<style>:root{{color-scheme:dark;font-family:Inter,"Noto Sans TC",sans-serif}}*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;color:#edf7f3;background:#07110f}}main{{width:min(520px,100%);padding:36px;border:1px solid rgba(157,197,184,.25);background:#0c1815;box-shadow:0 24px 80px rgba(0,0,0,.35)}}small{{color:#42d6a4;font:700 10px ui-monospace,monospace;letter-spacing:.18em}}h1{{margin:14px 0 0;font-size:28px}}p{{margin:16px 0;color:#9bb0aa;line-height:1.7}}button,a{{display:inline-block;margin-top:12px;padding:11px 15px;border:0;color:#07110f;background:#42d6a4;text-decoration:none;font-weight:800;cursor:pointer}}button:disabled{{opacity:.55;cursor:wait}}a{{margin-left:8px;background:transparent;color:#9bb0aa;border:1px solid rgba(157,197,184,.3)}}#request-status{{min-height:20px;margin:14px 0 0;font-size:13px}}#request-status.success{{color:#42d6a4}}#request-status.error{{color:#ff6b72}}</style></head>
<body><main><small>ACCESS CONTROL</small><h1>{title}</h1><p>{message}</p>
{request_action}<a href="/cdn-cgi/access/logout">登出並改用其他 Email</a></main></body></html>""",
        status_code=403, headers={"Cache-Control": "no-store"},
    )


def install_authorization_middleware(app, deps: ApiDependencies) -> None:
    @app.middleware("http")
    async def authorize_api_requests(request: Request, call_next):
        permission = required_permission(request.method, request.url.path)
        if not request.url.path.startswith("/api/") or request.method == "OPTIONS":
            return await call_next(request)
        if permission == "demo.run":
            if (request.method, request.url.path) not in {
                ("GET", "/api/demo/cases"), ("POST", "/api/demo/backtests")
            }:
                return JSONResponse({"detail": "unsupported demo route"}, status_code=403)
            return await call_next(request)
        try:
            if request.url.path == "/api/access-requests" and request.method == "POST":
                identity = deps.identity_from_headers(request.headers)
                if identity is None:
                    raise AccessTokenError("missing authenticated request identity")
                request.state.access_identity = identity
                actor = identity.subject
            else:
                user = deps.user_from_headers(request.headers)
                if permission:
                    deps.auth_service.require_permission(user, permission)
                request.state.auth_user = user
                actor = user.user_id
        except AccessTokenError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=401, headers={"Cache-Control": "no-store"})
        except AuthorizationError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=403, headers={"Cache-Control": "no-store"})
        scope = rate_limit_scope(request.method, request.url.path)
        if scope is None:
            return await call_next(request)
        decision = deps.limiter.check(scope, actor)
        headers = rate_limit_headers(decision)
        if not decision.allowed:
            return JSONResponse(
                {"detail": "rate limit exceeded", "scope": scope},
                status_code=429, headers={**headers, "Cache-Control": "no-store"},
            )
        response = await call_next(request)
        response.headers.update(headers)
        return response
