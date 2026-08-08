"""Pipeline supervisor: encoder lifecycle, health, failover, key rotation.

Model
-----
channel
  └── profile            (a *source*: primary camera, backup feed, ...)
        └── generation   (one ffmpeg process + one AES-128 key)

Only forward motion: a generation is never restarted in place — recovery,
failover and key rotation all spawn the *next* generation with a fresh,
pre-reserved block of media sequence numbers. The active generation's
segments are ingested into a per-channel, per-variant registry which backs
the unified HLS playlists, so the public playlist URL never changes and
players glide across rotations and failovers (a spec-compliant
#EXT-X-DISCONTINUITY tag marks the boundary).
"""
from __future__ import annotations

import asyncio
import shutil
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import settings
from .encoder import VARIANTS, EncoderJob, GenSpec
from .keys import KeyManager


# ---------------------------------------------------------------------------
@dataclass
class Segment:
    sn: int
    dur: float
    file: str
    gen_key: str          # "<profile>/g<n>"
    kid: str
    disc: bool = False    # prepend #EXT-X-DISCONTINUITY


@dataclass
class Generation:
    spec: GenSpec
    job: EncoderJob
    gen_key: str
    healthy: bool = False
    unhealthy_checks: int = 0
    last_seg_time: float = 0.0


@dataclass
class ProfileState:
    cfg: dict
    gen_counter: int = 0
    blocked_until: float = 0.0
    failed_rolls: int = 0


@dataclass
class ChannelState:
    cfg: dict
    profiles: dict  # id -> ProfileState
    next_sn: int = 0
    active: Optional[Generation] = None
    active_profile: Optional[str] = None
    warming: Optional[Generation] = None
    warming_profile: Optional[str] = None
    warming_started: float = 0.0
    registry: dict = field(default_factory=dict)   # variant -> deque[Segment]
    last_ingested_gen: Optional[str] = None
    rotations: int = 0
    failovers: int = 0
    last_roll: float = 0.0
    retired_gens: list = field(default_factory=list)  # (time, dir)


# ---------------------------------------------------------------------------
def parse_playlist(path: Path):
    """Return (media_sequence, [(sn, dur, file), ...]) or None."""
    try:
        text = path.read_text()
    except (FileNotFoundError, OSError):
        return None
    mseq = 0
    entries = []
    pending_dur = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            mseq = int(line.split(":", 1)[1])
        elif line.startswith("#EXTINF:"):
            try:
                pending_dur = float(line.split(":", 1)[1].split(",", 1)[0])
            except ValueError:
                pending_dur = None
        elif line and not line.startswith("#"):
            if pending_dur is not None:
                entries.append((mseq + len(entries), pending_dur, line))
                pending_dur = None
    return mseq, entries


class Pipeline:
    def __init__(self, config: dict):
        self.config = config
        self.hls_cfg = config.get("hls", {})
        self.keys = KeyManager()
        self.channels: dict[str, ChannelState] = {}
        self.started = time.time()
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._stopping = False

        for ch in config["channels"]:
            st = ChannelState(cfg=ch, profiles={})
            for p in sorted(ch["profiles"], key=lambda x: x.get("priority", 99)):
                st.profiles[p["id"]] = ProfileState(cfg=p)
            for v in VARIANTS:
                st.registry[v] = deque(maxlen=64)
            self.channels[ch["id"]] = st

    # ------------------------------------------------------------------ api
    @property
    def fps(self) -> int:
        return int(self.hls_cfg.get("fps", 25))

    @property
    def seg_seconds(self) -> int:
        return int(self.hls_cfg.get("segment_seconds", 2))

    @property
    def list_size(self) -> int:
        return int(self.hls_cfg.get("window_segments", 8))

    @property
    def rotation_interval(self) -> float:
        return float(self.hls_cfg.get("key_rotation_seconds", settings.KEY_ROTATION_SECONDS))

    def profile_order(self, st: ChannelState) -> list[str]:
        return list(st.profiles.keys())

    # ------------------------------------------------------------- lifecycle
    async def start(self):
        from .branding import ensure_brand_png
        # each server run owns a fresh media tree
        shutil.rmtree(settings.MEDIA_ROOT, ignore_errors=True)
        settings.MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
        for st in self.channels.values():
            ensure_brand_png(st.cfg)
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        for st in self.channels.values():
            for gen in (st.active, st.warming):
                if gen:
                    await gen.job.stop(grace=1.5)

    # ------------------------------------------------------------- spawning
    def _spawn_generation(self, st: ChannelState, profile_id: str) -> Generation:
        ps = st.profiles[profile_id]
        ps.gen_counter += 1
        key = self.keys.rotate(st.cfg["id"])
        # reserve enough media-sequence numbers to cover a full rotation
        # period plus margin — SNs must never collide across generations
        # (HLS implicit AES-128 IV = media sequence number)
        reserve = max(settings.SN_RESERVE,
                      int(self.rotation_interval // self.seg_seconds) + 16)
        start_sn = st.next_sn
        st.next_sn += reserve
        spec = GenSpec(
            channel=st.cfg["id"],
            profile=profile_id,
            generation=ps.gen_counter,
            kid=key.kid,
            key_path=key.path,
            start_number=start_sn,
            input_cfg=ps.cfg["input"],
            ts_offset=time.time() - self.started,
            fps=self.fps,
            seg_seconds=self.seg_seconds,
            list_size=self.list_size,
        )
        from .branding import brand_path
        job = EncoderJob(spec, brand_path(st.cfg))
        gen = Generation(spec=spec, job=job, gen_key=f"{profile_id}/g{ps.gen_counter}")
        return gen

    # --------------------------------------------------------------- health
    def _check_health(self, gen: Generation) -> bool:
        if gen.job.proc is None:
            return False
        if not gen.job.running:
            return False
        if time.time() - gen.job.started < 2.0:
            return gen.healthy  # still warming, keep last verdict
        pl = gen.spec.variant_playlist("hi")
        if not pl.exists():
            return False
        age = time.time() - pl.stat().st_mtime
        if age > settings.HEALTH_STALE_SECONDS:
            return False
        parsed = parse_playlist(pl)
        if not parsed:
            return False
        _, entries = parsed
        if not entries:
            return False
        newest = gen.spec.dir / "hi" / entries[-1][2]
        if not newest.exists():
            return False
        if time.time() - newest.stat().st_mtime > settings.HEALTH_STALE_SECONDS:
            return False
        gen.last_seg_time = newest.stat().st_mtime
        return True

    # -------------------------------------------------------------- ingest
    def _ingest(self, st: ChannelState) -> None:
        gen = st.active
        if not gen:
            return
        for variant in VARIANTS:
            parsed = parse_playlist(gen.spec.variant_playlist(variant))
            if not parsed:
                continue
            _, entries = parsed
            reg = st.registry[variant]
            last_sn = reg[-1].sn if reg else None
            for sn, dur, fname in entries:
                if last_sn is not None and sn <= last_sn:
                    continue
                disc = st.last_ingested_gen is not None and st.last_ingested_gen != gen.gen_key
                reg.append(Segment(sn=sn, dur=dur, file=fname,
                                   gen_key=gen.gen_key, kid=gen.spec.kid, disc=disc))
                st.last_ingested_gen = gen.gen_key
                last_sn = sn

    # ------------------------------------------------------------- promote
    async def _promote(self, st: ChannelState) -> None:
        assert st.warming is not None
        old = st.active
        old_profile = st.active_profile
        st.active = st.warming
        st.active_profile = st.warming_profile
        st.warming = None
        st.warming_profile = None
        if old is not None:
            await old.job.stop()
            st.retired_gens.append((time.time(), old.spec.dir))
            if old_profile != st.active_profile:
                st.failovers += 1
            else:
                st.rotations += 1
        st.last_roll = time.time()

    # ---------------------------------------------------------------- loop
    async def _loop(self):
        while not self._stopping:
            try:
                async with self._lock:
                    for st in self.channels.values():
                        await self._tick_channel(st)
                    self.keys.prune()
                    self._prune_dirs()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # never let the supervisor die
                print(f"[pipeline] tick error: {e}", flush=True)
            await asyncio.sleep(1.5)

    async def _tick_channel(self, st: ChannelState):
        now = time.time()
        ch_id = st.cfg["id"]
        order = self.profile_order(st)

        def unblocked(pid: str) -> bool:
            return now >= st.profiles[pid].blocked_until

        # ---------- nothing active yet: bring something up -----------------
        if st.active is None:
            if st.warming is not None:
                st.warming.healthy = self._check_health(st.warming)
                if st.warming.healthy:
                    await self._promote(st)
                elif now - st.warming_started > settings.ROLL_TIMEOUT_SECONDS:
                    await st.warming.job.stop()
                    st.warming, st.warming_profile = None, None
            else:
                for pid in order:
                    if unblocked(pid):
                        st.warming = self._spawn_generation(st, pid)
                        st.warming_profile = pid
                        st.warming_started = now
                        await st.warming.job.start()
                        print(f"[pipeline] {ch_id}: booting {st.warming.gen_key}", flush=True)
                        break
            self._ingest(st)
            return

        # ---------- health of the active generation ------------------------
        st.active.healthy = self._check_health(st.active)
        if st.active.healthy:
            st.active.unhealthy_checks = 0
        else:
            st.active.unhealthy_checks += 1

        # ---------- warming generation (roll / failover / recovery) --------
        if st.warming is not None:
            st.warming.healthy = self._check_health(st.warming)
            if st.warming.healthy:
                print(f"[pipeline] {ch_id}: promoting {st.warming.gen_key} "
                      f"(was {st.active.gen_key})", flush=True)
                await self._promote(st)
                self._ingest(st)
                return
            if now - st.warming_started > settings.ROLL_TIMEOUT_SECONDS:
                print(f"[pipeline] {ch_id}: warming {st.warming.gen_key} "
                      f"timed out, aborting", flush=True)
                await st.warming.job.stop()
                shutil.rmtree(st.warming.spec.dir, ignore_errors=True)
                st.warming, st.warming_profile = None, None

        # ---------- active unhealthy: recover / fail over ------------------
        if not st.active.healthy and st.warming is None:
            cur_profile = st.active_profile
            ps = st.profiles[cur_profile]
            if now < ps.blocked_until:
                # this source is intentionally offline -> fail over right away
                for pid in order:
                    if pid != cur_profile and unblocked(pid):
                        st.warming = self._spawn_generation(st, pid)
                        st.warming_profile = pid
                        st.warming_started = now
                        await st.warming.job.start()
                        print(f"[pipeline] {ch_id}: {cur_profile} blocked, "
                              f"FAILOVER to {pid} ({st.warming.gen_key})",
                              flush=True)
                        break
                self._ingest(st)
                return
            if (st.active.unhealthy_checks >= settings.FAIL_AFTER_CHECKS
                    and now >= ps.blocked_until):
                # give the current profile one more shot on a fresh generation
                if ps.failed_rolls < 2:
                    ps.failed_rolls += 1
                    st.warming = self._spawn_generation(st, cur_profile)
                    st.warming_profile = cur_profile
                    st.warming_started = now
                    await st.warming.job.start()
                    print(f"[pipeline] {ch_id}: respawning on {cur_profile} "
                          f"({st.warming.gen_key})", flush=True)
                else:
                    # move to the next profile
                    ps.failed_rolls = 0
                    for pid in order:
                        if pid != cur_profile and unblocked(pid):
                            st.warming = self._spawn_generation(st, pid)
                            st.warming_profile = pid
                            st.warming_started = now
                            await st.warming.job.start()
                            print(f"[pipeline] {ch_id}: FAILOVER to {pid} "
                                  f"({st.warming.gen_key})", flush=True)
                            break
            elif not st.active.job.running and now >= st.profiles[cur_profile].blocked_until:
                # process died outright — roll forward immediately
                ps.failed_rolls = 0
                st.warming = self._spawn_generation(st, cur_profile)
                st.warming_profile = cur_profile
                st.warming_started = now
                await st.warming.job.start()
                print(f"[pipeline] {ch_id}: process died, rolling to "
                      f"{st.warming.gen_key}", flush=True)
            self._ingest(st)
            return

        # ---------- periodic key rotation (rolling generation) -------------
        if (st.warming is None
                and st.active.healthy
                and now - st.active.job.started >= self.rotation_interval
                and now >= st.profiles[st.active_profile].blocked_until):
            st.warming = self._spawn_generation(st, st.active_profile)
            st.warming_profile = st.active_profile
            st.warming_started = now
            await st.warming.job.start()
            print(f"[pipeline] {ch_id}: key rotation, warming "
                  f"{st.warming.gen_key} (kid={st.warming.spec.kid})", flush=True)
            self._ingest(st)
            return

        # ---------- return to primary profile after failover ---------------
        primary = order[0]
        if (st.warming is None and st.active_profile != primary
                and unblocked(primary) and st.active.healthy):
            st.warming = self._spawn_generation(st, primary)
            st.warming_profile = primary
            st.warming_started = now
            await st.warming.job.start()
            print(f"[pipeline] {ch_id}: recovery roll back to {primary} "
                  f"({st.warming.gen_key})", flush=True)

        self._ingest(st)

    # ------------------------------------------------------------- pruning
    def _prune_dirs(self):
        now = time.time()
        for st in self.channels.values():
            keep = []
            for t, d in st.retired_gens:
                if now - t > settings.KEY_GRACE_SECONDS / 10:  # ~90s of grace
                    shutil.rmtree(d, ignore_errors=True)
                else:
                    keep.append((t, d))
            st.retired_gens = keep[-settings.KEEP_GENERATIONS:]

    # ----------------------------------------------------- public controls
    async def simulate_failure(self, channel: str, profile: str, seconds: float) -> bool:
        async with self._lock:
            st = self.channels.get(channel)
            if not st or profile not in st.profiles:
                return False
            st.profiles[profile].blocked_until = time.time() + seconds
            for gen, gprof in ((st.active, st.active_profile),
                               (st.warming, st.warming_profile)):
                if gen is not None and gprof == profile:
                    await gen.job.stop(grace=1.0)
            return True

    # ------------------------------------------------------------- snapshot
    def snapshot(self) -> dict:
        out = {"uptime": round(time.time() - self.started, 1), "channels": {}}
        for ch_id, st in self.channels.items():
            chs = {
                "active_profile": st.active_profile,
                "active_generation": st.active.gen_key if st.active else None,
                "warming_generation": st.warming.gen_key if st.warming else None,
                "rotations": st.rotations,
                "failovers": st.failovers,
                "current_kid": self.keys.current.get(ch_id),
                "profiles": {},
            }
            for pid, ps in st.profiles.items():
                gen = None
                if st.active_profile == pid:
                    gen = st.active
                elif st.warming_profile == pid:
                    gen = st.warming
                chs["profiles"][pid] = {
                    "blocked": time.time() < ps.blocked_until,
                    "generation": gen.gen_key if gen else None,
                    "process_alive": bool(gen and gen.job.running),
                    "healthy": bool(gen and gen.healthy),
                    "segment_age": round(time.time() - gen.last_seg_time, 1)
                                   if (gen and gen.last_seg_time) else None,
                }
            out["channels"][ch_id] = chs
        return out

    def render_variant_playlist(self, channel: str, variant: str) -> Optional[str]:
        st = self.channels.get(channel)
        if not st:
            return None
        reg = st.registry.get(variant)
        if not reg or len(reg) == 0:
            return None
        window = list(reg)[-self.list_size:]
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            f"#EXT-X-TARGETDURATION:{self.seg_seconds + 1}",
            f"#EXT-X-MEDIA-SEQUENCE:{window[0].sn}",
            "#EXT-X-INDEPENDENT-SEGMENTS",
        ]
        last_kid = None
        for seg in window:
            if seg.disc:
                lines.append("#EXT-X-DISCONTINUITY")
                last_kid = None
            if seg.kid != last_kid:
                lines.append(
                    f'#EXT-X-KEY:METHOD=AES-128,URI="/keys/{channel}/{seg.kid}.key"'
                )
                last_kid = seg.kid
            lines.append(f"#EXTINF:{seg.dur:.3f},")
            lines.append(f"{seg.gen_key}/{seg.file}")
        return "\n".join(lines) + "\n"
