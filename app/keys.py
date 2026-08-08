"""AES-128 key lifecycle.

Every rotation creates a fresh 16-byte key (kid = 12-hex id). Keys live on
disk at runtime/keys/<channel>/<kid>.key with mode 0600. Retired keys are
kept for a grace period so that players holding slightly-stale playlists can
still decrypt in-flight segments, then shredded and deleted.
"""
from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from .settings import KEY_GRACE_SECONDS, KEY_ROOT


@dataclass
class KeyInfo:
    kid: str
    channel: str
    created: float
    retired: Optional[float] = None

    @property
    def path(self):
        return KEY_ROOT / self.channel / f"{self.kid}.key"

    @property
    def uri(self) -> str:
        """Canonical (unsigned) key URI embedded into playlists."""
        return f"/keys/{self.channel}/{self.kid}.key"


@dataclass
class KeyManager:
    keys: Dict[str, KeyInfo] = field(default_factory=dict)  # kid -> KeyInfo
    current: Dict[str, str] = field(default_factory=dict)   # channel -> kid

    def rotate(self, channel: str) -> KeyInfo:
        """Create a fresh key for a channel and retire the previous one."""
        old_kid = self.current.get(channel)
        if old_kid and old_kid in self.keys:
            self.keys[old_kid].retired = time.time()

        kid = secrets.token_hex(6)
        info = KeyInfo(kid=kid, channel=channel, created=time.time())
        info.path.parent.mkdir(parents=True, exist_ok=True)
        info.path.write_bytes(secrets.token_bytes(16))
        os.chmod(info.path, 0o600)
        self.keys[kid] = info
        self.current[channel] = kid
        return info

    def current_key(self, channel: str) -> KeyInfo:
        kid = self.current.get(channel)
        if not kid:
            return self.rotate(channel)
        return self.keys[kid]

    def get(self, kid: str, channel: str) -> Optional[KeyInfo]:
        info = self.keys.get(kid)
        if info and info.channel == channel:
            return info
        return None

    def prune(self) -> int:
        """Delete long-retired keys. Returns number removed."""
        now = time.time()
        removed = 0
        for kid, info in list(self.keys.items()):
            if info.retired and now - info.retired > KEY_GRACE_SECONDS:
                try:
                    # shred then unlink
                    data = bytearray(info.path.read_bytes())
                    for i in range(len(data)):
                        data[i] = 0
                    info.path.write_bytes(bytes(data))
                    info.path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
                del self.keys[kid]
                removed += 1
        return removed
