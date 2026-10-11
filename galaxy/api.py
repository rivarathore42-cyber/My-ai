"""HTTP layer (Phase 9). Access is decided by galaxy/auth.py and galaxy/identity.py; answers come from the orchestrator.

Every /api answer is JSON. Chat replies always carry `text`, `ok` and `status`, and every refusal (401, 403, 404, 405,
413, 429, 500) has the same shape, so the page never has to guess what came back. The page itself is web/app.html.
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from typing import Optional

from . import auth
from . import web_assets
from .core import DEFAULT_DB, ROOT, owner_id
from .gateway import GuardPolicy, resolve_token
from .identity import Identity, IdentityMismatch, credential_of, identity_from_bearer, is_admin, is_user
from .identity import token_id as _token_id
from .memory import MemoryStore
from .orchestrator import Orchestrator
from .security import DAY_S, DEFAULT_TOKEN_TTL_S, clamp_ttl

log = logging.getLogger("galaxy.v6.api")
OWNER = owner_id(os.environ.get("GALAXY_OWNER_ID", "owner")) or "owner"
FRIENDLY = {
    401: "Is device ko pehle pair karna hoga. Server log me pairing code dekhkar page par daalein.",
    403: "Is session ko ye kaam karne ki ijazat nahi hai.",
    404: "Ye route maujood nahi hai.",
    405: "Is route par ye method allowed nahi hai.",
    413: "Message bahut bada hai.",
    429: "Bahut zyada requests aa gayi hain. Thodi der baad try karein.",
}


def configure_logging() -> None:
    logdir = ROOT / "logs"
    logdir.mkdir(exist_ok=True)
    root = logging.getLogger("galaxy")
    root.setLevel(logging.INFO)
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        handler = RotatingFileHandler(logdir / "galaxy.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)


def create_service(db_path: Optional[str] = None) -> Orchestrator:
    store = MemoryStore(db_path or DEFAULT_DB)
    store.prune_calls(keep=20_000)
    service = Orchestrator(store)
    service.llm.warm_in_background()   # the model loads in the background; requests never wait for it
    return service


def build_app(service: Optional[Orchestrator] = None):
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

    configure_logging()
    mode = auth.mode_from_env(os.environ.get("GALAXY_AUTH"))
    host = os.environ.get("HOST", "127.0.0.1")
    auth.check_startup(mode, host)
    service = service or create_service()
    operator_token, generated = resolve_token(os.environ.get("GALAXY_API_TOKEN"))
    if generated:
        os.environ["GALAXY_API_TOKEN"] = operator_token
        print(f"operator token (admin routes only; never shown on a page): {operator_token}", file=sys.stderr, flush=True)
    pairing = auth.PairingBook()
    service.pairing = pairing
    if mode == "paired":
        code = pairing.issue()
        print(f"PAIRING CODE: {code[:4]} {code[4:]}  (valid 10 minutes, one use, five tries)", file=sys.stderr, flush=True)
    policy = GuardPolicy(operator_token, open_access=(mode == "local"),
                         accept=lambda credential: service.store.token_resolve(credential) is not None)
    app = FastAPI(title="Galaxy AI Engine v7.0", version="7.0.0")

    def error_response(status: int, text: str, code: Optional[str] = None):
        return JSONResponse({"ok": False, "status": status, "error": code or "refused", "text": text}, status_code=status)

    def who(request: Request) -> Optional[Identity]:
        """The identity for this request. An explicit Authorization header wins; otherwise the session cookie; in local
        mode, with no header, the owner. Nothing in a body is consulted."""
        header = request.headers.get("authorization")
        if header:
            return identity_from_bearer(header, operator_token, service.store.token_resolve)
        if mode == "local":
            return Identity("user", OWNER, "local")
        cookie = auth.cookie_value(request.headers.get("cookie"))
        return identity_from_bearer(f"Bearer {cookie}", operator_token, service.store.token_resolve) if cookie else None

    async def body_of(request: Request):
        try:
            return await request.json()
        except Exception:
            return None

    def run_bound(fn, ident: Identity, body):
        try:
            return JSONResponse(fn(body, identity=ident))
        except IdentityMismatch as exc:
            return error_response(403, str(exc), "identity_mismatch")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/") or auth.bypasses_guard(request.method, path):
            return await call_next(request)
        try:
            length = int(request.headers.get("content-length") or 0)
        except ValueError:
            length = 0
        client = request.client.host if request.client else "unknown"
        header = request.headers.get("authorization")
        if not header:
            cookie = auth.cookie_value(request.headers.get("cookie"))
            header = f"Bearer {cookie}" if cookie else None
        decision = policy.check(request.method, path, header, client, length)
        if not decision.allowed:
            return error_response(decision.status, FRIENDLY.get(decision.status, "Request refused."))
        return await call_next(request)

    @app.get("/health")
    async def health_check():
        """Liveness for the platform (Render's healthCheckPath). Public and cheap; it reveals nothing about the system."""
        return JSONResponse({"ok": True, "status": "alive", "version": "7.0.0"})

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return HTMLResponse((web_assets.WEB_DIR / "app.html").read_text(encoding="utf-8"),
                            headers={"Cache-Control": "no-store"})

    @app.get("/static/voice/{name:path}")
    async def static_asset(name: str):
        path = web_assets.resolve_asset(name)
        if path is None:
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(path, media_type=web_assets.content_type(path), headers={"Cache-Control": "no-cache"})

    @app.post("/api/pair")
    async def api_pair(request: Request):
        body = await body_of(request)
        code = body.get("code") if isinstance(body, dict) else None
        if not isinstance(code, str) or not pairing.redeem(code):
            return error_response(403, "Pairing code galat hai, use ho chuka hai, ya expire ho gaya hai. "
                                       "Server log me naya code dekhein.", "pairing_refused")
        session_token = service.store.token_issue(OWNER, ttl_s=DEFAULT_TOKEN_TTL_S)
        response = JSONResponse({"ok": True, "status": 200, "user_id": OWNER, "expires_in_s": DEFAULT_TOKEN_TTL_S,
                                 "text": "Device pair ho gaya."})
        response.headers["set-cookie"] = auth.session_cookie(session_token, DEFAULT_TOKEN_TTL_S,
                                                             secure=auth.is_https(request))
        return response

    @app.post("/api/unpair")
    async def api_unpair(request: Request):
        ident = who(request)
        if not is_user(ident):
            return error_response(401, FRIENDLY[401])
        service.store.token_revoke(ident.token_id)
        response = JSONResponse({"ok": True, "status": 200, "text": "Device unpair ho gaya."})
        response.headers["set-cookie"] = auth.session_cookie("", 0, secure=auth.is_https(request))
        return response

    @app.get("/api/me")
    async def api_me(request: Request):
        ident = who(request)
        if ident is None:
            return error_response(401, FRIENDLY[401])
        return JSONResponse({"ok": True, "status": 200, "mode": mode, "kind": ident.kind,
                             "user_id": ident.user_id, "token_id": ident.token_id, "text": ""})

    @app.get("/api/health")
    async def api_health(request: Request):
        if who(request) is None:
            return error_response(401, FRIENDLY[401])
        return JSONResponse(service.health())

    @app.post("/api/chat")
    async def api_chat(request: Request):
        ident = who(request)
        if ident is None:
            return error_response(401, FRIENDLY[401])
        body = await body_of(request)
        try:
            payload = await service.handle(body, identity=ident)
        except IdentityMismatch as exc:
            return error_response(403, str(exc), "identity_mismatch")
        except Exception:
            log.exception("chat request failed")
            return error_response(500, "Internal error - kuch execute nahi hua. Dobara try karein.", "internal")
        return JSONResponse(payload)

    @app.get("/api/memory/{user_id}")
    async def api_memory(user_id: str, request: Request):
        ident = who(request)
        if ident is None:
            return error_response(401, FRIENDLY[401])
        uid = owner_id(user_id)
        if uid is None:
            return error_response(404, "Ye user maujood nahi hai.", "no_such_user")
        if ident.kind == "user" and ident.user_id != uid:
            return error_response(403, "Ye session sirf apni memory dekh sakta hai.", "not_your_memory")
        return JSONResponse({"ok": True, "status": 200, "user_id": uid, "attributes": service.store.all_attrs(uid),
                             "text": ""})

    @app.get("/api/tuning")
    async def api_tuning(request: Request):
        ident = who(request)
        if ident is None:
            return error_response(401, FRIENDLY[401])
        if not is_admin(ident):
            return error_response(403, FRIENDLY[403])
        return JSONResponse({"ok": True, "status": 200, "history": service.store.tuning_history(20), "text": ""})

    @app.post("/api/evolution/tune")
    async def api_tune(request: Request):
        ident = who(request)
        if ident is None:
            return error_response(401, FRIENDLY[401])
        if not is_admin(ident):
            return error_response(403, FRIENDLY[403])
        return JSONResponse(service.evolution.tune())

    @app.post("/api/admin/pairing-code")
    async def api_new_pairing_code(request: Request):
        ident = who(request)
        if ident is None:
            return error_response(401, FRIENDLY[401])
        if not is_admin(ident):
            return error_response(403, FRIENDLY[403])
        code = pairing.issue()
        return JSONResponse({"ok": True, "status": 200, "code": f"{code[:4]} {code[4:]}",
                             "text": "Naya pairing code (10 minute, ek baar)."})

    @app.get("/api/admin/tokens")
    async def api_list_tokens(request: Request):
        ident = who(request)
        if ident is None:
            return error_response(401, FRIENDLY[401])
        if not is_admin(ident):
            return error_response(403, FRIENDLY[403])
        return JSONResponse({"ok": True, "status": 200, "tokens": service.store.token_list(), "text": ""})

    @app.post("/api/admin/tokens")
    async def api_issue_token(request: Request):
        ident = who(request)
        if ident is None:
            return error_response(401, FRIENDLY[401])
        if not is_admin(ident):
            return error_response(403, FRIENDLY[403])
        body = await body_of(request)
        uid = owner_id(body.get("user_id")) if isinstance(body, dict) else None
        if uid is None:
            return error_response(400, "user_id required", "bad_request")
        ttl = None
        if isinstance(body, dict) and body.get("ttl_days") is not None:
            try:
                ttl = clamp_ttl(int(body["ttl_days"]) * DAY_S)
            except (TypeError, ValueError):
                return error_response(400, "ttl_days must be a number", "bad_request")
        issued = service.store.token_issue(uid, ttl_s=ttl)
        return JSONResponse({"ok": True, "status": 200, "user_id": uid, "token": issued, "token_id": _token_id(issued),
                             "text": "Token sirf ek baar dikhaya gaya hai. Abhi save karein."})

    @app.post("/api/admin/tokens/revoke")
    async def api_revoke_token(request: Request):
        ident = who(request)
        if ident is None:
            return error_response(401, FRIENDLY[401])
        if not is_admin(ident):
            return error_response(403, FRIENDLY[403])
        body = await body_of(request)
        key = body.get("token_id") if isinstance(body, dict) else None
        if not isinstance(key, str) or not key:
            return error_response(400, "token_id required", "bad_request")
        return JSONResponse({"ok": True, "status": 200, "revoked": service.store.token_revoke(key), "text": ""})

    @app.post("/api/me/rotate")
    async def api_rotate_own_token(request: Request):
        ident = who(request)
        if ident is None:
            return error_response(401, FRIENDLY[401])
        if not is_user(ident):
            return error_response(403, "Sirf user session apna token rotate kar sakta hai.", "not_a_user")
        header = request.headers.get("authorization")
        current = credential_of(header) if header else auth.cookie_value(request.headers.get("cookie"))
        fresh = service.store.token_rotate(current)
        if fresh is None:
            return error_response(401, FRIENDLY[401])
        response = JSONResponse({"ok": True, "status": 200, "text": "Session rotate ho gaya. Purana session band ho gaya hai."})
        response.headers["set-cookie"] = auth.session_cookie(fresh, DEFAULT_TOKEN_TTL_S,
                                                             secure=auth.is_https(request))
        return response

    @app.post("/api/android/decision")
    async def api_android_decision(request: Request):
        ident = who(request)
        if ident is None:
            return error_response(401, FRIENDLY[401])
        return run_bound(service.handle_decision, ident, await body_of(request))

    @app.post("/api/android/revoke")
    async def api_android_revoke(request: Request):
        ident = who(request)
        if ident is None:
            return error_response(401, FRIENDLY[401])
        return run_bound(service.handle_revoke, ident, await body_of(request))

    @app.post("/api/android/trace")
    async def api_android_trace(request: Request):
        ident = who(request)
        if ident is None:
            return error_response(401, FRIENDLY[401])
        return run_bound(service.handle_trace, ident, await body_of(request))

    @app.post("/api/friend/profile")
    async def api_friend_profile(request: Request):
        ident = who(request)
        if ident is None:
            return error_response(401, FRIENDLY[401])
        return run_bound(service.handle_profile, ident, await body_of(request))

    return app
