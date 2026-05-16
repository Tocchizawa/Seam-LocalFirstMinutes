#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


AUDIO_FILENAMES = ("combined.flac", "combined.wav", "system.wav", "mic.wav")
TRANSCRIPT_FILENAME = "transcript.jsonl"
SESSION_ID_RE = re.compile(r"^(?P<ts>\d{8}_\d{6})(?:_(?P<suffix>\d+))?$")
MIN_AUDIO_BYTES = 44


@dataclass
class SessionInfo:
    session_id: str
    path: Path
    timestamp: datetime | None
    has_transcript: bool
    audio_files: list[str]
    in_minutes_db: bool

    @property
    def has_audio(self) -> bool:
        return bool(self.audio_files)


@dataclass
class PairPlan:
    target: SessionInfo
    source: SessionInfo
    delta_sec: int


def _is_nonempty_file(path: Path, min_bytes: int = 0) -> bool:
    try:
        return path.is_file() and path.stat().st_size > min_bytes
    except OSError:
        return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Repair split Seam sessions where transcript and audio were written "
            "to different session directories."
        )
    )
    parser.add_argument(
        "--sessions-dir",
        default=str(Path("~/.seam/sessions").expanduser()),
        help="Target sessions directory (default: ~/.seam/sessions)",
    )
    parser.add_argument(
        "--db-path",
        default=str(Path("~/.seam/minutes.db").expanduser()),
        help="minutes.db path for session_id cross-check (default: ~/.seam/minutes.db)",
    )
    parser.add_argument(
        "--max-delta-sec",
        type=int,
        default=3,
        help="Maximum timestamp distance when pairing split sessions (default: 3)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually move files. Without this, dry-run only.",
    )
    parser.add_argument(
        "--prune-empty-source",
        action="store_true",
        help="Remove source directory when it becomes empty after moving files.",
    )
    return parser.parse_args()


def parse_timestamp_from_session_id(session_id: str) -> datetime | None:
    m = SESSION_ID_RE.match(session_id)
    if not m:
        return None
    try:
        return datetime.strptime(m.group("ts"), "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def load_minutes_session_ids(db_path: Path) -> set[str]:
    if not db_path.exists():
        return set()
    try:
        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute("SELECT session_id FROM minutes").fetchall()
        return {str(r[0]) for r in rows if r and r[0]}
    except Exception:
        return set()


def collect_sessions(sessions_dir: Path, db_session_ids: set[str]) -> list[SessionInfo]:
    sessions: list[SessionInfo] = []
    for p in sorted((x for x in sessions_dir.iterdir() if x.is_dir()), key=lambda x: x.name):
        transcript_path = p / TRANSCRIPT_FILENAME
        has_transcript = _is_nonempty_file(transcript_path)
        audio_files: list[str] = []
        for name in AUDIO_FILENAMES:
            audio_path = p / name
            if _is_nonempty_file(audio_path, MIN_AUDIO_BYTES):
                audio_files.append(name)
        sessions.append(
            SessionInfo(
                session_id=p.name,
                path=p,
                timestamp=parse_timestamp_from_session_id(p.name),
                has_transcript=has_transcript,
                audio_files=audio_files,
                in_minutes_db=(p.name in db_session_ids),
            )
        )
    return sessions


def build_pair_plans(
    sessions: list[SessionInfo],
    max_delta_sec: int,
) -> tuple[list[PairPlan], list[str]]:
    transcript_only = [s for s in sessions if s.has_transcript and not s.has_audio and s.timestamp is not None]
    audio_only = [s for s in sessions if s.has_audio and not s.has_transcript and s.timestamp is not None]
    used_audio_ids: set[str] = set()
    plans: list[PairPlan] = []
    warnings: list[str] = []

    for t in transcript_only:
        ranked: list[tuple[int, int, int, SessionInfo]] = []
        for a in audio_only:
            if a.session_id in used_audio_ids:
                continue
            delta_sec = int((a.timestamp - t.timestamp).total_seconds())
            abs_delta = abs(delta_sec)
            if abs_delta > max_delta_sec:
                continue
            # Prefer:
            # 1) smaller absolute delta
            # 2) non-negative delta (recorder-side session id often becomes same or +1s)
            # 3) source not present in minutes DB
            ranked.append(
                (
                    abs_delta,
                    0 if delta_sec >= 0 else 1,
                    0 if not a.in_minutes_db else 1,
                    a,
                )
            )

        if not ranked:
            continue
        ranked.sort(key=lambda x: (x[0], x[1], x[2], x[3].session_id))
        best_abs = ranked[0][0]
        equally_close = [r for r in ranked if r[0] == best_abs]
        if len(equally_close) > 1:
            cand = ", ".join(
                f"{r[3].session_id}(delta={int((r[3].timestamp - t.timestamp).total_seconds())})"
                for r in equally_close
            )
            warnings.append(
                f"ambiguous pair for {t.session_id}: {cand} (skipped)"
            )
            continue

        source = ranked[0][3]
        delta_sec = int((source.timestamp - t.timestamp).total_seconds())
        used_audio_ids.add(source.session_id)
        plans.append(PairPlan(target=t, source=source, delta_sec=delta_sec))

    return plans, warnings


def move_audio_files(plan: PairPlan, apply: bool) -> tuple[list[str], list[str], list[str]]:
    moved: list[str] = []
    dedup_removed: list[str] = []
    conflicts: list[str] = []

    for name in plan.source.audio_files:
        src = plan.source.path / name
        dst = plan.target.path / name
        if not src.exists():
            continue

        if dst.exists():
            try:
                same = src.stat().st_size == dst.stat().st_size
            except OSError:
                same = False
            if same:
                if apply:
                    try:
                        src.unlink(missing_ok=True)
                    except OSError:
                        conflicts.append(name)
                        continue
                dedup_removed.append(name)
                continue
            conflicts.append(name)
            continue

        if apply:
            try:
                src.replace(dst)
            except OSError:
                conflicts.append(name)
                continue
        moved.append(name)

    return moved, dedup_removed, conflicts


def main() -> int:
    args = parse_args()
    sessions_dir = Path(args.sessions_dir).expanduser()
    db_path = Path(args.db_path).expanduser()
    if not sessions_dir.exists():
        print(f"[error] sessions dir not found: {sessions_dir}", file=sys.stderr)
        return 1

    db_session_ids = load_minutes_session_ids(db_path)
    sessions = collect_sessions(sessions_dir, db_session_ids)
    plans, warnings = build_pair_plans(sessions, max_delta_sec=max(0, int(args.max_delta_sec)))

    transcript_only_count = sum(1 for s in sessions if s.has_transcript and not s.has_audio)
    audio_only_count = sum(1 for s in sessions if s.has_audio and not s.has_transcript)
    print(
        f"[scan] sessions={len(sessions)} transcript_only={transcript_only_count} "
        f"audio_only={audio_only_count} db_sessions={len(db_session_ids)}"
    )
    print(f"[plan] matched_pairs={len(plans)} apply={bool(args.apply)}")

    for w in warnings:
        print(f"[warn] {w}")

    moved_pairs = 0
    conflict_pairs = 0
    moved_files_total = 0
    dedup_removed_total = 0
    pruned_sources = 0

    for plan in plans:
        moved, dedup_removed, conflicts = move_audio_files(plan, apply=bool(args.apply))
        moved_files_total += len(moved)
        dedup_removed_total += len(dedup_removed)

        status = "ok"
        if conflicts:
            status = "conflict"
            conflict_pairs += 1
        else:
            moved_pairs += 1

        print(
            f"[pair] {plan.target.session_id} <= {plan.source.session_id} "
            f"delta={plan.delta_sec:+d}s status={status} "
            f"moved={moved or '-'} dedup={dedup_removed or '-'} conflicts={conflicts or '-'}"
        )

        if bool(args.apply) and bool(args.prune_empty_source):
            remaining: list[Path] | None
            try:
                remaining = list(plan.source.path.iterdir())
            except OSError:
                remaining = None
            if remaining == []:
                plan.source.path.rmdir()
                pruned_sources += 1

    print(
        f"[summary] pairs={len(plans)} repaired={moved_pairs} conflicts={conflict_pairs} "
        f"moved_files={moved_files_total} dedup_removed={dedup_removed_total} "
        f"pruned_sources={pruned_sources}"
    )
    return 2 if conflict_pairs > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
