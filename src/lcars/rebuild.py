"""The rebuild (PLAN-CODE 9.1; PLAN-DATA §1–§3, "Phase 9.1 decisions").

    lcars rebuild <run-dir> --snapshot <09-06 file> --live <live backup> --inputs <dir>
                  [--from-stage N] [--until-stage N]

Builds the new database from the 09-06 snapshot (the 08:53Z state) with the new
engines and the user's decisions, on copies only. Every stage ends in a labelled
checkpoint (`<run-dir>/NN-<stage>.db`), so a later stage can be re-run from the
previous one and any point can be rolled back to. External writes are always
captured here (PLAN-CODE 9.0), whatever the config says; reads (Sonarr, AniList,
MAL, TVDB) are real.

Every input decision is consumed exactly once and written to the ledger
(`<run-dir>/ledger.jsonl`: applied / skipped-with-reason); a decision that can't
be placed fails the run. The ledger is the proof that everything the user
decided landed.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from lcars import util

STAGES = [
    "base", "sources", "structure", "sonarr", "numbering", "statuses", "replay", "checks",
    "writes",
]
SOURCE_TABLES = [
    "anidb_anime", "anidb_title", "anidb_episode", "anime_list_entry", "anime_list_mapping",
    "tvmaze_episode", "syoboi_title", "syoboi_program",
]


class RebuildError(RuntimeError):
    """A decision or input that can't be placed: the run stops."""


@dataclass
class Run:
    dir: Path
    snapshot: Path
    live: Path
    inputs: Path
    ledger: list = field(default_factory=list)

    def checkpoint(self, n: int) -> Path:
        return self.dir / f"{n:02d}-{STAGES[n - 1]}.db"

    def work(self) -> Path:
        return self.dir / "work.db"

    def record(self, kind: str, key: str, outcome: str, detail: str = "") -> None:
        entry = {"at": util.now_utc_iso(), "kind": kind, "key": key, "outcome": outcome,
                 "detail": detail}
        self.ledger.append(entry)
        with open(self.dir / "ledger.jsonl", "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _backup(src: Path, dst: Path) -> None:
    """A consistent copy (`.backup`, never `cp` — PLAN-DATA §1)."""
    if dst.exists():
        dst.unlink()
    with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as s, sqlite3.connect(dst) as d:
        s.backup(d)


def _connect(path: Path) -> sqlite3.Connection:
    from lcars import db

    db.close()
    return db.connect(path)


# ── stage 1: base ────────────────────────────────────────────────────────


def stage_base(run: Run) -> None:
    """The 09-06 snapshot, upgraded to the current schema."""
    _backup(run.snapshot, run.work())
    done = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{run.work()}"},
        capture_output=True, text=True,
    )
    if done.returncode != 0:
        raise RebuildError("schema upgrade failed:\n" + done.stderr[-2000:])
    run.record("stage", "base", "applied", str(run.snapshot))


# ── stage 2: sources ─────────────────────────────────────────────────────


def stage_sources(run: Run) -> None:
    """Source tables from live (PLAN-DATA "Source data first"), plus the
    AniDB answers fetched on 09-28 (`<inputs>/anidb_xml/*.xml`)."""
    import xml.etree.ElementTree as ET

    from lcars import anidb

    conn = _connect(run.work())
    conn.execute("ATTACH DATABASE ? AS live", (str(run.live),))  # the file is read-only
    for table in SOURCE_TABLES:
        cols = [r[1] for r in conn.execute(f"PRAGMA main.table_info('{table}')")]
        live_cols = {r[1] for r in conn.execute(f"PRAGMA live.table_info('{table}')")}
        shared = [c for c in cols if c in live_cols]
        if not shared:
            raise RebuildError(f"source table {table} missing from live")
        conn.execute(f"DELETE FROM main.{table}")
        conn.execute(f"INSERT INTO main.{table} ({', '.join(shared)})"
                     f" SELECT {', '.join(shared)} FROM live.{table}")
        n = conn.execute(f"SELECT COUNT(*) FROM main.{table}").fetchone()[0]
        run.record("source", table, "applied", f"{n} rows")
    conn.commit()
    conn.execute("DETACH DATABASE live")
    xml_dir = run.inputs / "anidb_xml"
    fetched = 0
    for path in sorted(xml_dir.glob("*.xml")):
        anidb_id = int(path.stem.split("_")[-1]) if path.stem.split("_")[-1].isdigit() else None
        if anidb_id is None:
            run.record("anidb_xml", path.name, "skipped", "no AniDB id in the file name")
            continue
        root = ET.parse(path).getroot()
        if root.tag == "error":
            run.record("anidb_xml", path.name, "skipped", f"AniDB error: {root.text}")
            continue
        episodes = anidb._parse_episodes_xml(root)
        conn.execute("DELETE FROM anidb_episode WHERE anidb_anime_id = ?", (anidb_id,))
        anidb.ingest_anime_episodes(conn, anidb_id, episodes, "2026-09-28T00:00:00Z")
        fetched += 1
        run.record("anidb_xml", path.name, "applied", f"{len(episodes)} episodes")
    conn.commit()
    run.record("stage", "sources", "applied", f"{fetched} AniDB answers ingested")


STAGE_FUNCS = {"base": stage_base, "sources": stage_sources}


def main(argv=None) -> int:
    import argparse

    from lcars import config

    parser = argparse.ArgumentParser(prog="lcars rebuild", description=__doc__.split("\n\n")[0])
    parser.add_argument("run_dir")
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--live", required=True)
    parser.add_argument("--inputs", required=True)
    parser.add_argument("--from-stage", type=int, default=1)
    parser.add_argument("--until-stage", type=int, default=len(STAGES))
    args = parser.parse_args(argv)

    os.environ["LCARS_EXTERNAL_WRITES"] = "capture"  # never sends, whatever the config says
    os.environ["LCARS_AUTOMATION_FROZEN"] = "0"  # the engines run — on this copy only
    config.set_current(config.load_config())
    run = Run(*(Path(p).expanduser().resolve()
                for p in (args.run_dir, args.snapshot, args.live, args.inputs)))
    run.dir.mkdir(parents=True, exist_ok=True)
    if args.from_stage > 1:
        _backup(run.checkpoint(args.from_stage - 1), run.work())
    for n in range(args.from_stage, args.until_stage + 1):
        name = STAGES[n - 1]
        if name not in STAGE_FUNCS:
            raise RebuildError(f"stage {n} ({name}) isn't built yet")
        print(f"stage {n}: {name}", flush=True)
        STAGE_FUNCS[name](run)
        from lcars import db

        db.close()
        _backup(run.work(), run.checkpoint(n))
    return 0
