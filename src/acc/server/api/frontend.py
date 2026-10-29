"""The built React dashboard (served by accd itself) and the security headers on every response."""
from __future__ import annotations

import mimetypes

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .context import Context

mimetypes.add_type("application/manifest+json", ".webmanifest")


def install(app: FastAPI, ctx: Context) -> None:
    """Registered last: the SPA route catches every path the API routers did not."""
    from ..preview import domain

    static = ctx.settings.static_dir
    if (static / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404)
        index = static / "index.html"
        if not index.exists():
            return HTMLResponse("<p>Rynhult UI not built. Run <code>npm run build</code> in /srv/acc/app/web.</p>", 503)
        candidate = (static / path).resolve()
        if path and candidate.is_file() and static.resolve() in candidate.parents:
            # The service worker and manifest must be re-checked on every load, or phones keep an old copy.
            fresh = {"Cache-Control": "no-cache"} if candidate.name in ("sw.js", "manifest.webmanifest") else None
            return FileResponse(candidate, headers=fresh)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    @app.api_route("/api/{path:path}", methods=["POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
    def no_such_api(path: str):
        # An API route that isn't registered (a preview switched off) is absent: 404, not the SPA's 405.
        raise HTTPException(404)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if ctx.settings.cookie_secure:  # served over HTTPS: browsers never try plain HTTP for this host again
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            f"frame-src {'https' if ctx.settings.cookie_secure else 'http'}://*.{domain(ctx.settings)}{':*' if not ctx.settings.cookie_secure else ''}; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        return response
