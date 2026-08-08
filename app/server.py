"""Aurora Live — protected HLS delivery + control API + web app.

Security model
--------------
* Viewers authenticate once with the shared access code -> session token
  (HMAC-signed, expiring).
* Playlists are fetched with the session token; every playlist is rendered
  fresh on each request, and the segment/key URLs embedded in it carry
  *brand-new* short-lived signatures (segments ~25 s, keys ~90 s).
* Segment and key endpoints verify the path signature before serving a
  single byte; nothing is served by static file fallback.
* Keys themselves are only ever produced inside the runtime directory and
  delivered through the signed, authenticated key endpoint.
"""
from __future__ import annotations

import json
import re
import secrets
import time
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import settings, tokens
from .pipeline import Pipeline

app = FastAPI(title="Aurora Live", docs_url=None, redoc_url=None)

# ---------------------------------------------------------------------------
# Config & pipeline
# ---------------------------------------------------------------------------
CONFIG = json.loads(settings.CONFIG_FILE.read_text())
PIPE = Pipeline(CONFIG)

# ---------------------------------------------------------------------------
# Access code
# ---------------------------------------------------------------------------
def _access_code() -> str:
    import os
    env = os.environ.get("ACCESS_CODE")
    if env:
        return env.strip()
    path = settings.STATE_ROOT / "access_code.txt"
    try:
        code = path.read_text().strip()
        if code:
            return code
    except FileNotFoundError:
        pass
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    code = "".join(secrets.choice(alphabet) for _ in range(10))
    path.write_text(code + "\n")
    import os as _os
    _os.chmod(path, 0o600)
    return code


ACCESS_CODE = _access_code()

# crude per-IP login rate limiter
_login_hits: dict[str, list[float]] = defaultdict(list)

SESSION_COOKIE = "aurora_session"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def require_session(request: Request) -> dict:
    tok = request.query_params.get("token") or ""
    if not tok:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            tok = auth[7:]
    payload = tokens.verify_token(tok, "session") if tok else None
    if not payload:
        raise HTTPException(status_code=401, detail="invalid or expired token")
    return payload


def sign_url(path: str, ttl: int) -> str:
    q = tokens.sign_path(path, ttl)
    return f"{path}?exp={q['exp']}&sig={q['sig']}"


def sign_url_quantized(path: str, ttl: int) -> str:
    q = tokens.sign_path_quantized(path, ttl)
    return f"{path}?exp={q['exp']}&sig={q['sig']}"


def require_signed(request: Request, path: str):
    ok = tokens.verify_path(path, request.query_params.get("exp"),
                            request.query_params.get("sig", ""))
    if not ok:
        raise HTTPException(status_code=403, detail="invalid signature")


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def _startup():
    await PIPE.start()
    print("=" * 66, flush=True)
    print(f"Aurora Live running on port {settings.PORT}", flush=True)
    print(f"Access code: {ACCESS_CODE}", flush=True)
    print("=" * 66, flush=True)


@app.on_event("shutdown")
async def _shutdown():
    await PIPE.stop()


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Content-Security-Policy",
                            "frame-ancestors 'none'; default-src 'self'")
    if request.url.path.startswith(("/live/", "/keys/")):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------------------
# auth + control API
# ---------------------------------------------------------------------------
@app.post("/api/login")
async def login(request: Request):
    ip = request.client.host if request.client else "?"
    now = time.time()
    hits = [t for t in _login_hits[ip] if now - t < 60]
    if len(hits) >= 8:
        raise HTTPException(status_code=429, detail="too many attempts")
    hits.append(now)
    _login_hits[ip] = hits

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="bad request")
    code = str(body.get("code", "")).strip()
    import hmac as _hmac
    if not _hmac.compare_digest(code.encode(), ACCESS_CODE.encode()):
        raise HTTPException(status_code=401, detail="invalid access code")
    tok = tokens.issue_token("session", settings.SESSION_TTL)
    return {"token": tok, "expires_in": settings.SESSION_TTL}


@app.get("/api/channels")
async def api_channels(request: Request):
    require_session(request)
    out = []
    for ch in CONFIG["channels"]:
        out.append({
            "id": ch["id"],
            "name": ch["name"],
            "category": ch.get("category", "General"),
            "description": ch.get("description", ""),
            "color": ch.get("color", "#6d5df6"),
            "url": f"/live/{ch['id']}/index.m3u8",
            "profiles": [p["id"] for p in ch["profiles"]],
        })
    return {"channels": out}


@app.get("/api/status")
async def api_status(request: Request):
    require_session(request)
    return PIPE.snapshot()


@app.post("/api/simulate-failure")
async def api_simulate_failure(request: Request):
    require_session(request)
    body = await request.json()
    channel = str(body.get("channel", ""))
    profile = str(body.get("profile", ""))
    seconds = float(body.get("seconds", 30))
    if not (5 <= seconds <= 300):
        raise HTTPException(status_code=400, detail="seconds must be 5..300")
    ok = await PIPE.simulate_failure(channel, profile, seconds)
    if not ok:
        raise HTTPException(status_code=404, detail="unknown channel/profile")
    return {"ok": True, "channel": channel, "profile": profile, "seconds": seconds}


# ---------------------------------------------------------------------------
# HLS delivery
# ---------------------------------------------------------------------------
@app.get("/live/{channel}/index.m3u8")
async def master_playlist(channel: str, request: Request):
    require_session(request)
    cfg = next((c for c in CONFIG["channels"] if c["id"] == channel), None)
    if not cfg:
        raise HTTPException(status_code=404, detail="unknown channel")
    tok = request.query_params.get("token", "")
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-INDEPENDENT-SEGMENTS"]
    lines += [
        '#EXT-X-STREAM-INF:BANDWIDTH=2300000,AVERAGE-BANDWIDTH=2100000,'
        'CODECS="avc1.42c01f,mp4a.40.2",RESOLUTION=1280x720,VIDEO-RANGE=SDR',
        f"hi/index.m3u8?token={tok}",
        '#EXT-X-STREAM-INF:BANDWIDTH=650000,AVERAGE-BANDWIDTH=580000,'
        'CODECS="avc1.42c01f,mp4a.40.2",RESOLUTION=640x360,VIDEO-RANGE=SDR',
        f"low/index.m3u8?token={tok}",
        '#EXT-X-STREAM-INF:BANDWIDTH=110000,AVERAGE-BANDWIDTH=100000,'
        'CODECS="mp4a.40.2"',
        f"audio/index.m3u8?token={tok}",
    ]
    return PlainTextResponse("\n".join(lines) + "\n",
                             media_type="application/vnd.apple.mpegurl")


@app.get("/live/{channel}/{variant}/index.m3u8")
async def variant_playlist(channel: str, variant: str, request: Request):
    require_session(request)
    if variant not in ("hi", "low", "audio"):
        raise HTTPException(status_code=404, detail="unknown variant")
    text = PIPE.render_variant_playlist(channel, variant)
    if text is None:
        raise HTTPException(status_code=503, detail="no media available yet")

    out_lines = []
    for line in text.splitlines():
        if line.startswith("#EXT-X-KEY:"):
            m = re.search(r'URI="([^"]+)"', line)
            if m:
                out_lines.append(line.replace(
                    m.group(1),
                    sign_url_quantized(m.group(1), settings.KEY_TTL_QUANTIZED)))
            else:
                out_lines.append(line)
        elif line and not line.startswith("#"):
            base = f"/live/{channel}/{variant}/{line}"
            out_lines.append(sign_url(base, settings.SEGMENT_TTL))
        else:
            out_lines.append(line)
    return PlainTextResponse("\n".join(out_lines) + "\n",
                             media_type="application/vnd.apple.mpegurl")


_TS_RE = re.compile(r"^[\w.-]+\.ts$")


@app.get("/live/{channel}/{variant}/{profile}/g{generation}/{filename}")
async def segment(channel: str, variant: str, profile: str, generation: int,
                  filename: str, request: Request):
    path = request.url.path
    require_signed(request, path)
    if not _TS_RE.match(filename):
        raise HTTPException(status_code=400, detail="bad filename")
    p = (settings.MEDIA_ROOT / channel / profile / f"g{generation}"
         / variant / filename)
    try:
        p = p.resolve()
        if settings.MEDIA_ROOT.resolve() not in p.parents:
            raise HTTPException(status_code=400, detail="bad path")
    except (OSError, RuntimeError):
        raise HTTPException(status_code=400, detail="bad path")
    if not p.is_file():
        raise HTTPException(status_code=404, detail="segment expired or unknown")
    return FileResponse(p, media_type="video/mp2t",
                        headers={"Cache-Control": "no-store"})


@app.get("/keys/{channel}/{kid}.key")
async def key_material(channel: str, kid: str, request: Request):
    path = request.url.path
    require_signed(request, path)
    if not re.match(r"^[0-9a-f]{12}$", kid):
        raise HTTPException(status_code=400, detail="bad kid")
    info = PIPE.keys.get(kid, channel)
    if not info:
        raise HTTPException(status_code=404, detail="unknown key")
    data = info.path.read_bytes()
    return Response(content=data, media_type="application/octet-stream",
                    headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# web app (registered last: catch-all)
# ---------------------------------------------------------------------------
WEB_ROOT = Path(__file__).resolve().parent.parent / "web"


@app.get("/")
async def index():
    return FileResponse(WEB_ROOT / "index.html")


app.mount("/", StaticFiles(directory=WEB_ROOT), name="static")
