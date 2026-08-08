# Aurora Live — protected live-streaming platform

A self-contained live-streaming web app that plays **sources you own or have
rights to**, with broadcast-grade resilience and the same protection
mechanisms real broadcasters use.

> The demo channels are 100 % synthetic (FFmpeg-generated test cards). Nothing
> in this project re-broadcasts third-party content. Plug in your own camera
> feeds, licensed footage, or officially published endpoints via
> `channels.json`.

## Feature map

| Requirement | Implementation |
|---|---|
| Player + channel guide + categories | Dark broadcast-style SPA (`web/`), channels grouped by category, per-channel status |
| Adaptive bitrate | 3-rung HLS ladder per channel: **720 p / 360 p / audio-only**, H.264 + AAC via FFmpeg, ABR in hls.js (auto or manual) |
| Smooth, interruption-free playback | 2 s segments, GOP-locked keyframes, tuned player buffering (`maxBufferLength 30 s`), exponential-backoff reconnect, rolling encoder handovers |
| Redundant encoders / origins | Every channel has multiple **source profiles** (primary + backup); a supervisor health-checks them every 1.5 s and fails over automatically, then rolls back when the primary recovers |
| Stream protection | **AES-128 segment encryption with rotating keys**, **short-lived signed URLs** for every segment and key, **token-gated** playlists, key delivery behind authentication |

## Architecture

```
                         ┌────────────────────────────── FFmpeg generation N ─┐
 channels.json ──▶ Supervisor ──▶ [720p enc]──▶ AES-128 ──▶ hi/*.ts          │
                 (health, failover, │  [360p enc]──▶ AES-128 ──▶ low/*.ts    │ key file
                  key rotation)     │  [audio enc]──▶ AES-128 ──▶ audio/*.ts | (0600)
                                    └─────────────────────────────────────────┘
                                              ▲ rolls forward every 60 s
                                              │ (fresh key, fresh generation)
        browser (hls.js) ◀── signed segment/key URLs (25–90 s TTL)
              │                    ▲
              └── session token ── FastAPI: /api/login · /live/* · /keys/*
```

* **Generation** = one FFmpeg process + one AES-128 key. Recovery, failover
  and key rotation all move *forward* to a new generation — an encoder is
  never restarted in place, so there is never a gap in the served playlist.
* The server merges each generation's segments into a per-channel registry
  and renders the HLS playlists itself. Public playlist URLs are stable;
  `#EXT-X-DISCONTINUITY` marks handover boundaries (spec-compliant).
* `-start_number` reserves a block of media-sequence numbers per generation.
  HLS implicit IV = media sequence number, so the IV the player derives is
  exactly the IV FFmpeg used while encrypting — across every rotation.

## Security model

| Layer | Mechanism |
|---|---|
| Viewer auth | Access code → HMAC-signed session token (12 h TTL), rate-limited login |
| Playlists | Only served against a valid session token; re-rendered on every request |
| Segment URLs | Fresh HMAC signature per render, **25 s expiry** — a leaked playlist is useless within seconds |
| Key URLs | Fresh signature, **90 s expiry**, constant-time verification, `no-store` |
| Keys on disk | `runtime/keys/**` mode `0600`; retired keys shredded after a grace period |
| Server secret | Random 32 bytes, `0600`, never leaves disk; rotate it to revoke everything |
| Re-embedding | No CORS grants, `X-Frame-Options: DENY`, CSP `frame-ancestors 'none'`, no static file fallback |

Explicit non-goals: this is not DRM (no Widevine/FairPlay) and no client-side
scheme makes the URL of a playing stream invisible to the viewer's own
browser — signed URLs + AES-128 is the industry-standard protection tier for
non-DRM delivery and stops scraping, hot-linking and re-distribution.

## Running

```bash
./run.sh                # bootstraps venv + deps, then serves
# → prints:  [auth] access code: xxxxxxxxxx
# → open http://localhost:8300  (or the preview URL) and enter the code
```

Environment overrides: `AURORA_PORT`, `ACCESS_CODE`, `AURORA_SESSION_TTL`,
`FFMPEG_BIN`.

### Trying the resilience features

* **Key rotation** — happens automatically every 60 s (`key_rotation_seconds`);
  watch `stats → key id / key rotations` while playing.
* **Failover** — click **“test failover”** in the player: the primary encoder
  is killed and blocked for 40 s; the supervisor spins the backup source and
  the player continues. When the block expires the pipeline rolls back to
  primary automatically.

## Adding your own sources

Edit `channels.json`. Each channel lists **profiles** (redundant sources) in
priority order:

```json
"input": { "type": "lavfi", "pattern": "testsrc2" }              // generated (demo)
"input": { "type": "file", "src": "/path/to/your-footage.mp4" }  // your file
"input": { "type": "url",  "src": "https://your-origin/feed.ts", // your feed
           "has_audio": true }
```

Tunables under `hls`: `segment_seconds`, `window_segments`, `fps`,
`key_rotation_seconds`.

## Layout

```
app/          server, pipeline supervisor, encoder jobs, key manager, tokens
web/          SPA (player, guide, stats) + vendored hls.js
channels.json channel & ladder configuration
runtime/      media tree, keys, logs, state  (git-ignored; chmod-restricted)
run.sh        bootstrap + launcher
```
