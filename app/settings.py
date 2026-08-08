"""Central settings and filesystem layout for Aurora Live."""
from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "runtime"
MEDIA_ROOT = RUNTIME / "media"          # HLS output tree (per channel/profile/generation)
KEY_ROOT = RUNTIME / "keys"              # AES-128 key material (chmod 0600)
BRAND_ROOT = RUNTIME / "brand"           # generated branding overlay PNGs
LOG_ROOT = RUNTIME / "logs"              # ffmpeg stderr logs
STATE_ROOT = RUNTIME / "state"           # persisted secrets / access code

CONFIG_FILE = ROOT / "channels.json"

for _p in (RUNTIME, MEDIA_ROOT, KEY_ROOT, BRAND_ROOT, LOG_ROOT, STATE_ROOT):
    _p.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
HOST = os.environ.get("AURORA_HOST", "0.0.0.0")
PORT = int(os.environ.get("AURORA_PORT", "8300"))

# ---------------------------------------------------------------------------
# Token lifetimes (seconds)
# ---------------------------------------------------------------------------
SESSION_TTL = int(os.environ.get("AURORA_SESSION_TTL", str(12 * 3600)))  # viewer session
PLAYLIST_TTL = 20      # signed query on playlist URLs (re-signed on every fetch)
SEGMENT_TTL = 25       # signed query on segment URLs (~12 segments of headroom)
KEY_TTL = 90           # signed query on key URLs
KEY_TTL_QUANTIZED = 120  # quantized window for key-URL signatures (stable URIs)

# ---------------------------------------------------------------------------
# Pipeline behaviour
# ---------------------------------------------------------------------------
KEY_ROTATION_SECONDS = 60     # rolling key rotation period per channel
SN_RESERVE = 20               # media-sequence numbers reserved per generation
HEALTH_STALE_SECONDS = 10.0   # newest segment older than this => origin stale
FAIL_AFTER_CHECKS = 3         # consecutive unhealthy checks before failover
ROLL_TIMEOUT_SECONDS = 18.0   # max wait for a new generation to warm up
KEEP_GENERATIONS = 2          # generations of segments kept on disk for grace
KEY_GRACE_SECONDS = 15 * 60   # how long retired keys stay available

FFMPEG_BIN = os.environ.get("FFMPEG_BIN") or None  # resolved lazily in encoder.py
