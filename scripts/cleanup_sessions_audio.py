#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SessionResult:
    session_id: str
    before_bytes: int
    after_bytes: int
    converted: bool
    pruned: bool
    skipped: bool
    reason: str
    error: str | None = None


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def is_audio_file(path: Path) -> bool:
    return path.exists() and path.is_file() and file_size(path) > 44


def dir_size(path: Path) -> int:
    total = 0
    for p in path.iterdir():
        if p.is_file():
            total += file_size(p)
    return total


def rm_if_exists(path: Path) -> bool:
    if not path.exists():
        return False
    path.unlink()
    return True


def run_ffmpeg(cmd: list[str]) -> tuple[bool, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode == 0:
        return True, ""
    msg = (proc.stderr or proc.stdout or "").strip()
    return False, msg[-1200:]


def convert_session(
    session_dir: Path,
    ffmpeg_bin: str,
    raw_sample_rate: int,
    compression_level: int,
    apply: bool,
) -> SessionResult:
    sid = session_dir.name
    before = dir_size(session_dir)

    combined_flac = session_dir / "combined.flac"
    combined_wav = session_dir / "combined.wav"
    mic_wav = session_dir / "mic.wav"
    system_wav = session_dir / "system.wav"
    system_raw = session_dir / "system.raw"

    target_exists = is_audio_file(combined_flac)
    to_prune = [combined_wav, mic_wav, system_wav, system_raw]

    if target_exists:
        pruned = False
        if apply:
            for p in to_prune:
                pruned = rm_if_exists(p) or pruned
        after = dir_size(session_dir) if apply else before
        return SessionResult(
            session_id=sid,
            before_bytes=before,
            after_bytes=after,
            converted=False,
            pruned=pruned,
            skipped=not pruned,
            reason="already_has_combined_flac",
        )

    # Build ffmpeg input and mix plan
    has_combined_wav = is_audio_file(combined_wav)
    has_mic_wav = is_audio_file(mic_wav)
    has_system_wav = is_audio_file(system_wav)
    has_system_raw = is_audio_file(system_raw)

    if has_combined_wav:
        mode = "combined_wav"
        cmd = [
            ffmpeg_bin,
            "-y",
            "-i",
            str(combined_wav),
            "-ar",
            "24000",
            "-ac",
            "1",
            "-c:a",
            "flac",
            "-compression_level",
            str(compression_level),
            str(combined_flac),
        ]
    elif has_mic_wav and has_system_wav:
        mode = "mic_plus_system_wav"
        cmd = [
            ffmpeg_bin,
            "-y",
            "-i",
            str(mic_wav),
            "-i",
            str(system_wav),
            "-filter_complex",
            "amix=inputs=2:duration=longest",
            "-ar",
            "24000",
            "-ac",
            "1",
            "-c:a",
            "flac",
            "-compression_level",
            str(compression_level),
            str(combined_flac),
        ]
    elif has_mic_wav and has_system_raw:
        mode = "mic_plus_system_raw"
        cmd = [
            ffmpeg_bin,
            "-y",
            "-i",
            str(mic_wav),
            "-f",
            "f32le",
            "-ar",
            str(raw_sample_rate),
            "-ac",
            "1",
            "-i",
            str(system_raw),
            "-filter_complex",
            "amix=inputs=2:duration=longest",
            "-ar",
            "24000",
            "-ac",
            "1",
            "-c:a",
            "flac",
            "-compression_level",
            str(compression_level),
            str(combined_flac),
        ]
    elif has_mic_wav:
        mode = "mic_only"
        cmd = [
            ffmpeg_bin,
            "-y",
            "-i",
            str(mic_wav),
            "-ar",
            "24000",
            "-ac",
            "1",
            "-c:a",
            "flac",
            "-compression_level",
            str(compression_level),
            str(combined_flac),
        ]
    elif has_system_wav:
        mode = "system_wav_only"
        cmd = [
            ffmpeg_bin,
            "-y",
            "-i",
            str(system_wav),
            "-ar",
            "24000",
            "-ac",
            "1",
            "-c:a",
            "flac",
            "-compression_level",
            str(compression_level),
            str(combined_flac),
        ]
    elif has_system_raw:
        mode = "system_raw_only"
        cmd = [
            ffmpeg_bin,
            "-y",
            "-f",
            "f32le",
            "-ar",
            str(raw_sample_rate),
            "-ac",
            "1",
            "-i",
            str(system_raw),
            "-ar",
            "24000",
            "-ac",
            "1",
            "-c:a",
            "flac",
            "-compression_level",
            str(compression_level),
            str(combined_flac),
        ]
    else:
        return SessionResult(
            session_id=sid,
            before_bytes=before,
            after_bytes=before,
            converted=False,
            pruned=False,
            skipped=True,
            reason="no_supported_audio_source",
        )

    if not apply:
        return SessionResult(
            session_id=sid,
            before_bytes=before,
            after_bytes=before,
            converted=False,
            pruned=False,
            skipped=True,
            reason=f"dry_run:{mode}",
        )

    ok, err = run_ffmpeg(cmd)
    if not ok or not is_audio_file(combined_flac):
        rm_if_exists(combined_flac)
        return SessionResult(
            session_id=sid,
            before_bytes=before,
            after_bytes=dir_size(session_dir),
            converted=False,
            pruned=False,
            skipped=False,
            reason=f"ffmpeg_failed:{mode}",
            error=err or "ffmpeg failed",
        )

    pruned = False
    for p in to_prune:
        if p == combined_flac:
            continue
        pruned = rm_if_exists(p) or pruned

    after = dir_size(session_dir)
    return SessionResult(
        session_id=sid,
        before_bytes=before,
        after_bytes=after,
        converted=True,
        pruned=pruned,
        skipped=False,
        reason=f"converted:{mode}",
    )


def fmt_mb(v: int) -> str:
    return f"{v / (1024 * 1024):.1f}MB"


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Convert old Seam sessions to combined.flac and remove bulky intermediates."
    )
    ap.add_argument(
        "--sessions-dir",
        default=str(Path("~/.seam/sessions").expanduser()),
        help="Target sessions directory (default: ~/.seam/sessions)",
    )
    ap.add_argument(
        "--raw-sample-rate",
        type=int,
        default=48000,
        help="Sample rate used when decoding system.raw (default: 48000)",
    )
    ap.add_argument(
        "--compression-level",
        type=int,
        default=8,
        help="FLAC compression level passed to ffmpeg (default: 8)",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process only newest N sessions (0 = all)",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Actually execute conversion and prune files. Without this, dry-run only.",
    )
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    sessions_dir = Path(args.sessions_dir).expanduser()
    if not sessions_dir.exists():
        print(f"[error] sessions dir not found: {sessions_dir}", file=sys.stderr)
        return 1

    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        print("[error] ffmpeg not found on PATH", file=sys.stderr)
        return 1

    sessions = sorted(
        [p for p in sessions_dir.iterdir() if p.is_dir()],
        key=lambda p: p.name,
        reverse=True,
    )
    if args.limit > 0:
        sessions = sessions[: args.limit]

    results: list[SessionResult] = []
    for s in sessions:
        results.append(
            convert_session(
                session_dir=s,
                ffmpeg_bin=ffmpeg_bin,
                raw_sample_rate=max(8000, min(48000, int(args.raw_sample_rate))),
                compression_level=max(0, min(12, int(args.compression_level))),
                apply=bool(args.apply),
            )
        )

    converted = sum(1 for r in results if r.converted)
    pruned = sum(1 for r in results if r.pruned)
    failed = [r for r in results if r.error]
    before_total = sum(r.before_bytes for r in results)
    after_total = sum(r.after_bytes for r in results)

    print(
        f"[summary] sessions={len(results)} converted={converted} pruned={pruned} failed={len(failed)} apply={args.apply}"
    )
    print(f"[size] before={fmt_mb(before_total)} after={fmt_mb(after_total)} saved={fmt_mb(before_total - after_total)}")

    for r in failed[:20]:
        print(f"[failed] {r.session_id} {r.reason} {r.error}")

    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
