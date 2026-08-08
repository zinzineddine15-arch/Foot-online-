"""FFmpeg encoder jobs.

One job = one ffmpeg process producing one *generation* of a channel profile:
a 3-rung HLS ladder (720p / 360p / audio-only), all rungs AES-128 encrypted
with the generation's key via `-hls_key_info_file`.

`-start_number` is used so that the media sequence numbers of each
generation continue a global, per-channel counter — this is what lets the
server merge generations (and profiles) into one continuous playlist while
keeping AES-128 implicit IVs (IV == media sequence number) consistent
between the encrypter (ffmpeg) and the player.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .settings import FFMPEG_BIN, LOG_ROOT, MEDIA_ROOT

VARIANTS = ("hi", "low", "audio")

# ladder definition: rung -> (video bitrate, audio bitrate, resolution)
LADDER = {
    "hi": dict(vbits="2000k", abits="96k", size="1280:720"),
    "low": dict(vbits="500k", abits="64k", size="640:360"),
    "audio": dict(vbits=None, abits="96k", size=None),
}

_ffmpeg_bin: Optional[str] = FFMPEG_BIN


def ffmpeg_bin() -> str:
    global _ffmpeg_bin
    if _ffmpeg_bin:
        return _ffmpeg_bin
    try:
        import imageio_ffmpeg
        _ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        found = shutil.which("ffmpeg")
        if not found:
            raise RuntimeError("ffmpeg binary not found")
        _ffmpeg_bin = found
    return _ffmpeg_bin


@dataclass
class GenSpec:
    channel: str
    profile: str
    generation: int
    kid: str
    key_path: Path
    start_number: int
    input_cfg: dict
    ts_offset: float
    fps: int = 25
    seg_seconds: int = 2
    list_size: int = 8

    @property
    def dir(self) -> Path:
        return MEDIA_ROOT / self.channel / self.profile / f"g{self.generation}"

    @property
    def key_info_file(self) -> Path:
        return self.dir / "key_info"

    def variant_playlist(self, variant: str) -> Path:
        return self.dir / variant / "index.m3u8"


def _write_key_info(spec: GenSpec) -> None:
    # line 1: URI written verbatim into playlists (server re-signs on serve)
    # line 2: local path ffmpeg reads the key bytes from
    spec.dir.mkdir(parents=True, exist_ok=True)
    spec.key_info_file.write_text(
        f"/keys/{spec.channel}/{spec.kid}.key\n{spec.key_path}\n"
    )
    os.chmod(spec.key_info_file, 0o600)


def build_command(spec: GenSpec, brand_png: Optional[Path]) -> list[str]:
    d = spec.dir
    for v in VARIANTS:
        (d / v).mkdir(parents=True, exist_ok=True)
    _write_key_info(spec)

    fps, seg = spec.fps, spec.seg_seconds
    gop = fps * seg
    inp = spec.input_cfg

    cmd: list[str] = [ffmpeg_bin(), "-nostdin", "-nostats", "-hide_banner",
                      "-loglevel", "error", "-y", "-re"]

    # ---- inputs ------------------------------------------------------------
    if inp["type"] == "lavfi":
        cmd += ["-f", "lavfi", "-i", f"{inp['pattern']}=size=1280x720:rate={fps}",
                "-f", "lavfi", "-i", f"sine=frequency={inp.get('tone_a', 440)}:sample_rate=48000",
                "-f", "lavfi", "-i", f"sine=frequency={inp.get('tone_b', 554)}:sample_rate=48000"]
        audio_src = "[1:a][2:a]amix=inputs=2:duration=longest,volume=0.35[a0]"
        video_base = "[0:v]"
        voffset = 3
    elif inp["type"] in ("url", "file"):
        cmd += ["-i", inp["src"]]
        audio_src = "[0:a]volume=1.0[a0]" if inp.get("has_audio", True) else None
        video_base = "[0:v]"
        voffset = 1
    else:
        raise ValueError(f"unknown input type {inp['type']}")

    # ---- filtergraph --------------------------------------------------------
    fc: list[str] = []
    if audio_src:
        fc.append(audio_src)
        fc.append("[a0]asplit=3[a1][a2][a3]")
    else:
        fc.append("anullsrc=r=48000:cl=stereo[a0]")
        fc.append("[a0]asplit=3[a1][a2][a3]")

    if brand_png and brand_png.exists():
        cmd += ["-i", str(brand_png)]
        bi = voffset
        fc.append(f"{video_base}[{bi}:v]overlay=0:0[vb]")
        video_mid = "[vb]"
    else:
        video_mid = video_base

    fc.append(f"{video_mid}format=yuv420p,split=2[v1][v2]")
    fc.append("[v1]scale=1280:720[o1]")
    fc.append("[v2]scale=640:360[o2]")

    cmd += ["-filter_complex", ";".join(fc)]

    # ---- common HLS options -------------------------------------------------
    def hls_opts(variant: str, sn: int) -> list[str]:
        return [
            "-f", "hls",
            "-hls_time", str(seg),
            "-hls_list_size", str(spec.list_size),
            "-hls_flags", "delete_segments+independent_segments+program_date_time",
            "-hls_key_info_file", str(spec.key_info_file),
            "-start_number", str(sn),
            "-muxdelay", "0", "-muxpreload", "0",
            "-output_ts_offset", f"{spec.ts_offset:.3f}",
        ]

    sn = spec.start_number
    # video rungs
    for rung in ("hi", "low"):
        L = LADDER[rung]
        cmd += ["-map", "[o1]" if rung == "hi" else "[o2]", "-map", "[a1]" if rung == "hi" else "[a2]",
                "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                "-profile:v", "baseline", "-pix_fmt", "yuv420p",
                "-b:v", L["vbits"], "-maxrate", L["vbits"], "-bufsize", str(int(L["vbits"].rstrip('k')) * 2) + "k",
                "-r", str(fps), "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
                "-c:a", "aac", "-b:a", L["abits"], "-ac", "2", "-ar", "48000"]
        cmd += hls_opts(rung, sn)
        cmd += [str(d / rung / "index.m3u8")]
    # audio-only rung
    cmd += ["-map", "[a3]", "-vn", "-c:a", "aac", "-b:a", LADDER['audio']["abits"], "-ac", "2", "-ar", "48000"]
    cmd += hls_opts("audio", sn)
    cmd += [str(d / "audio" / "index.m3u8")]
    return cmd


class EncoderJob:
    """Supervises a single ffmpeg generation process."""

    def __init__(self, spec: GenSpec, brand_png: Optional[Path]):
        self.spec = spec
        self.brand_png = brand_png
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.started = time.time()
        self.stopped_reason: Optional[str] = None
        self._log_path = LOG_ROOT / f"{spec.channel}-{spec.profile}-g{spec.generation}.log"

    async def start(self) -> None:
        cmd = build_command(self.spec, self.brand_png)
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        logf = open(self._log_path, "ab", buffering=0)
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=logf
            )
        finally:
            # stderr fd is now owned by the child; close our handle
            try:
                logf.close()
            except Exception:
                pass
        self.started = time.time()

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def stop(self, grace: float = 3.0) -> None:
        if not self.running:
            return
        assert self.proc is not None
        try:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=grace)
                return
            except asyncio.TimeoutError:
                pass
            self.proc.kill()
            await self.proc.wait()
        except ProcessLookupError:
            pass

    async def wait(self) -> Optional[int]:
        if self.proc is None:
            return None
        return await self.proc.wait()
