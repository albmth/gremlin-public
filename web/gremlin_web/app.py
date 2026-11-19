"""HTTP: the public pages and the JSON API (`/api/v1`) behind them.

Anyone may use the site without signing in; every request belongs to an anonymous visitor
(see `visitors`), whose cookie the middleware issues on first contact. Requests that change
state must come from the site's own origin (checked on `Origin`), so another website open in
the same browser cannot act for its visitor, and the site answers only to its own host name, so
a DNS-rebinding page cannot read it. A custom domain reaches the Function URL through a proxy
(`infra/cloudflare/demo-proxy`) that proves itself with `PROXY_HEADER`; only then does the site
trust the forwarded client address and accept its own Function URL host.
Errors are `{"error": {"code": ..., "message": ...}}`.
"""

from __future__ import annotations

import hashlib
import hmac
from functools import cache
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, ConfigDict, Field

from gremlin_web.generations import RAW_LIMITS, RAW_TYPES, RESULTS, Rejected, closed_message
from gremlin_web.visitors import COOKIE, COOKIE_SECONDS, new_token, visitor_of

HERE = Path(__file__).parent
SHOWCASE_ASSETS = ("reference.jpg", "clip.jpg", "clip.mp4", "result.jpg", "result.mp4")
STATIC_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".mp4": "video/mp4",
    ".ico": "image/vnd.microsoft.icon",
}
# A trusted proxy sends PROXY_HEADER with proxy_key(token) and the visitor's address in
# CLIENT_IP_HEADER; anything else claiming to be the proxy is refused.
PROXY_HEADER = "x-gremlin-proxy"
CLIENT_IP_HEADER = "x-gremlin-client-ip"
# Page path -> (template, title)
PAGES = {
    "/": ("create.html", None),
    "/terms": ("terms.html", "Terms"),
}


def proxy_key(gremlin_token: str) -> str:
    # Derived from the Gremlin token the site already holds, so the proxy needs no new stored
    # secret on AWS; rotating that token means putting the new key into the proxy too.
    return hmac.new(gremlin_token.encode(), b"gremlin-web-proxy", hashlib.sha256).hexdigest()


def error(status, code, message, headers=None):
    return JSONResponse(
        {"error": {"code": code, "message": message}}, status_code=status, headers=headers
    )


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FileSpec(Strict):
    bytes: int
    type: str = Field(max_length=64)


class DraftRequest(Strict):
    reference: FileSpec
    driving: FileSpec


class Point(Strict):
    x: float = Field(strict=True, ge=0, le=1, allow_inf_nan=False)
    y: float = Field(strict=True, ge=0, le=1, allow_inf_nan=False)


class SelectRequest(Strict):
    revision: str = Field(max_length=32)
    reference_point: Point | None = None
    target_point: Point | None = None
    reference_candidate: int | None = Field(default=None, strict=True, ge=0, le=7)
    target_candidate: int | None = Field(default=None, strict=True, ge=0, le=7)
    prompt: str | None = Field(default=None, min_length=20, max_length=2000)
    tier: str | None = None


class ReviseRequest(Strict):
    revision: str = Field(max_length=32)
    start_ms: int | None = Field(default=None, strict=True, ge=0, le=60_000)
    duration_ms: int | None = Field(default=None, strict=True, ge=3000, le=5000)
    draft_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")


@cache
def _asset_version(name):
    path = HERE / "static" / name
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12] if path.is_file() else "0"


def create_app(service):
    settings = service.settings
    generations = service.generations
    app = FastAPI(title="Gremlin web", docs_url=None, redoc_url=None, openapi_url=None)
    templates = Environment(
        loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape()
    )
    # Deployed assets never change within a process; locally they follow edits.
    version = (
        _asset_version
        if settings.environment in {"staging", "prod"}
        else _asset_version.__wrapped__
    )
    templates.globals |= {
        "asset": lambda name: f"/static/{name}?v={version(name)}",
        "retention_hours": settings.retention_hours,
        "showcase_available": all(
            (HERE / "static" / "showcase" / name).is_file() for name in SHOWCASE_ASSETS
        ),
    }
    storage_origins = _storage_origins(settings)
    own_host = urlparse(settings.public_origin).netloc
    key = proxy_key(settings.gremlin_token.get_secret_value()) if settings.gremlin_token else None

    @app.middleware("http")
    async def guard(request: Request, call_next):
        claimed = request.headers.get(PROXY_HEADER)
        if claimed is not None:
            if not key or not hmac.compare_digest(claimed.encode(), key.encode()):
                return error(403, "UNTRUSTED_PROXY", "This request did not come through the site.")
            request.state.address = request.headers.get(CLIENT_IP_HEADER) or "unknown"
        elif request.headers.get("host", "") != own_host:
            if request.method == "GET" and not request.url.path.startswith("/api/"):
                return RedirectResponse(settings.public_origin + request.url.path, 307)
            return error(421, "WRONG_HOST", f"Use {settings.public_origin}.")
        else:
            # Directly at the Function URL this is the caller's own address, not a proxy's.
            request.state.address = request.client.host if request.client else "unknown"
        token = request.cookies.get(COOKIE)
        visitor = visitor_of(token)
        fresh = visitor is None and not request.url.path.startswith(("/static/", "/health"))
        if fresh:
            token = new_token()
            visitor = visitor_of(token)
        request.state.visitor = visitor
        response = await call_next(request)
        if fresh:
            response.set_cookie(
                COOKIE,
                token,
                max_age=COOKIE_SECONDS,
                httponly=True,
                secure=settings.secure_cookies,
                samesite="lax",
                path="/",
            )
        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("Referrer-Policy", "same-origin")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if settings.secure_cookies:
            headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if headers.get("content-type", "").startswith("text/html"):
            media = " ".join(storage_origins)
            headers.setdefault(
                "Content-Security-Policy",
                # Cloudflare injects its Web Analytics beacon on the demo domain.
                "default-src 'self'; script-src 'self' https://static.cloudflareinsights.com; "
                "style-src 'self'; "
                f"img-src 'self' data: blob: {media}; "
                f"media-src 'self' blob: {media}; "
                f"connect-src 'self' {media} https://cloudflareinsights.com; font-src 'self'; "
                "frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'self'",
            )
        if request.url.path.startswith("/api/"):
            headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(Rejected)
    async def rejected(_request, exc: Rejected):
        return error(exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def invalid(_request, exc: RequestValidationError):
        fields = {part for issue in exc.errors() for part in issue["loc"]}
        message = "The request is missing a field or has an invalid one."
        if fields & {"start_ms", "duration_ms"}:
            message = "Choose 3-5 seconds starting within the first minute."
        elif "prompt" in fields:
            message = "Use a description between 20 and 2000 characters."
        return error(422, "INVALID_REQUEST", message)

    def same_site(request: Request):
        if (
            request.method not in {"GET", "HEAD"}
            and request.headers.get("origin") != settings.public_origin
        ):
            raise Rejected(403, "CROSS_SITE", "This request must come from this site.")

    def visitor(request: Request):
        same_site(request)
        return request.state.visitor

    def address(request: Request):
        return request.state.address

    # Service

    @app.get("/health")
    def health():
        return {"status": "ok", "build": settings.build_commit or None}

    @app.get("/api/v1/config")
    def config(you: str = Depends(visitor), ip: str = Depends(address)):
        admission = generations.admission()
        unfinished = generations.unfinished(you)
        slot = generations.active()
        return {
            "gremlin": {
                "ready": bool(admission.get("accepting") and admission.get("dispatching")),
                "reason_code": admission.get("reason_code"),
                "message": closed_message(admission),
            },
            "active": unfinished and unfinished["gid"],
            "active_state": unfinished and unfinished["state"],
            # Someone else's video holds the site's one slot, so a new one can't start yet.
            "busy": bool(slot) and slot.get("visitor") != you,
            "left": service.quotas.left(),
            "total": settings.total_generations,
            "network": {
                "left": service.quotas.network_left(ip),
                "total": settings.address_daily_videos,
            },
            "preparing": generations.preparing(),
            "duration_ms": [3000, 5000],
            "limits": {
                "reference_bytes": RAW_LIMITS["reference"],
                "driving_bytes": RAW_LIMITS["driving"],
            },
            "types": {kind: sorted(types) for kind, types in RAW_TYPES.items()},
        }

    # Generations

    @app.post("/api/v1/generations")
    def create_draft(body: DraftRequest, you: str = Depends(visitor), ip: str = Depends(address)):
        return generations.create_draft(you, ip, body.model_dump())

    @app.post("/api/v1/generations/{gid}/submit")
    def submit(gid: str, you: str = Depends(visitor)):
        return generations.view(generations.submit(you, gid))

    @app.post("/api/v1/generations/{gid}/select")
    def select(gid: str, body: SelectRequest, you: str = Depends(visitor)):
        return generations.view(generations.select(you, gid, body.model_dump()))

    @app.post("/api/v1/generations/{gid}/prepare")
    def revise(gid: str, body: ReviseRequest, you: str = Depends(visitor)):
        return generations.view(generations.revise(you, gid, body.model_dump()))

    @app.get("/api/v1/generations/{gid}/preview/{side}")
    def preview(gid: str, side: str, revision: str | None = None, you: str = Depends(visitor)):
        url = generations.preview_url(you, gid, side, revision)
        if url is None:
            return error(404, "NOT_FOUND", "That picture isn't available.")
        return RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store"})

    @app.get("/api/v1/generations/{gid}")
    def generation(gid: str, you: str = Depends(visitor)):
        gen = generations.get(you, gid)
        if gen is None:
            return error(404, "NOT_FOUND", "That video doesn't exist.")
        return generations.view(generations.refresh(gen))

    @app.post("/api/v1/generations/{gid}/cancel")
    def cancel(gid: str, you: str = Depends(visitor)):
        return generations.view(generations.cancel(you, gid))

    @app.delete("/api/v1/generations/{gid}", status_code=204)
    def delete(gid: str, you: str = Depends(visitor)):
        generations.delete(you, gid)
        return Response(status_code=204)

    @app.get("/api/v1/generations/{gid}/files/{name}")
    def file(gid: str, name: str, download: bool = False, you: str = Depends(visitor)):
        url = generations.file_url(you, gid, name, download) if name in RESULTS else None
        if url is None:
            return error(404, "NOT_FOUND", "That file isn't available.")
        return RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store"})

    # Pages

    @app.get("/static/{name:path}", include_in_schema=False)
    def static(name: str):
        path = (HERE / "static" / name).resolve()
        if not path.is_file() or (HERE / "static") not in path.parents:
            return Response(status_code=404)
        return Response(
            path.read_bytes(),
            media_type=STATIC_TYPES.get(path.suffix, "application/octet-stream"),
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    def page(request: Request, template, title, path, **context):
        html = templates.get_template(template).render(title=title, path=path, **context)
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    for page_path, (template, title) in PAGES.items():

        def handler(request: Request, _path=page_path, _template=template, _title=title):
            return page(request, _template, _title, _path)

        app.add_api_route(page_path, handler, methods=["GET"], include_in_schema=False)

    @app.get("/videos/{gid}", include_in_schema=False)
    def video_page(gid: str, request: Request):
        return page(request, "video.html", "Video", f"/videos/{gid}", gid=gid)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return RedirectResponse(f"/static/favicon.ico?v={version('favicon.ico')}", status_code=302)

    return app


def _storage_origins(settings):
    if settings.s3_endpoint:
        parsed = urlparse(settings.s3_endpoint)
        return [f"{parsed.scheme}://{parsed.netloc}"]
    return [
        f"https://{settings.media_bucket}.s3.{settings.region}.amazonaws.com",
        f"https://{settings.media_bucket}.s3.amazonaws.com",
    ]
