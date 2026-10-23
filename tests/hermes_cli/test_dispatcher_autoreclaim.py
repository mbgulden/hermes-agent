"""Regression tests: the dispatcher's stale-claim release self-heals after a
gateway restart without ever touching a live worker.

Task: t_65319f13 (worker runs survive dispatcher restarts) / t_98e37fcc
(scoped three cases). Semantics pinned on ``release_stale_claims`` in
``hermes_cli/kanban_db.py``, which ``dispatch_once`` calls on every
in-gateway dispatch tick (main line 6726):

  (a) claim EXPIRED + worker PID DEAD        -> reclaimed, task back to
                                                ``ready`` (the recovery path)
  (b) claim EXPIRED + worker PID ALIVE       -> claim EXTENDED, worker
      (heartbeat fresh)                            untouched — the live
                                                worker must never be killed
  (c) claim UNEXPIRED (any PID state)      -> not selected, untouched
  (d) claim EXPIRED + worker ALIVE but    -> reclaimed via the heartbeat-
      heartbeat >30m stale                       stale backstop (#29747
                                                gap 3) — the self-heal that
                                                resolves the Oct 21
                                                stuck-but-alive orphans

Context: run 236 REJECTED the earlier "reclaim when the claim_lock PID is
dead, even if the worker is alive" proposal (RESTART-RESILIENCE.md §3a).
The claim_lock PID is a *transient claim-process* PID, not the gateway's
stable PID — on 2026-10-22 all 8/8 healthy in-flight tasks on the live
board had a dead claim_lock PID with a live, heartbeating worker. Reclaiming
on that signal is a false positive that would mass-kill in-flight work.
These tests pin the correct semantics so the regression cannot return.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Load kanban_db from THIS repo checkout. The ambient env may carry an
# editable-install mapping pointing at a different (possibly dirty) checkout;
# the test must always exercise the tree it lives in.
# ---------------------------------------------------------------------------
_REPO = Path(__file__).resolve().parents[2]


def _force_load_kanban_db():
    pkg_dir = _REPO / "hermes_cli"
    existing = sys.modules.get("hermes_cli")
    if (
        existing is not None
        and getattr(existing, "__file__", "")
        and not str(existing.__file__).startswith(str(_REPO))
    ):
        for name in [
            n
            for n in sys.modules
            if n == "hermes_cli" or n.startswith("hermes_cli.")
        ]:
            del sys.modules[name]
    if "hermes_cli" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "hermes_cli",
            pkg_dir / "__init__.py",
            submodule_search_locations=[str(pkg_dir)],
        )
        pkg = importlib.util.module_from_spec(spec)
        sys.modules["hermes_cli"] = pkg
        spec.loader.exec_module(pkg)
    kb = importlib.import_module("hermes_cli.kanban_db")
    assert str(kb.__file__).startswith(str(_REPO)), (
        f"loaded kanban_db from unexpected tree: {kb.__file__}"
    )
    return kb


kb = _force_load_kanban_db()


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db.resolve()))
    kb.init_db()
    with kb.connect(board="default") as conn:
        yield conn


@pytest.fixture
def dead_pid():
    p = subprocess.Popen(["true"])
    p.wait()
    assert not os.path.exists(f"/proc/{p.pid}"), "test pid must be dead"
    return p.pid


@pytest.fixture
def live_worker():
    sleeper = subprocess.Popen(["sleep", "120"])
    time.sleep(0.2)
    assert os.path.exists(f"/proc/{sleeper.pid}"), "worker must be alive"
    yield sleeper
    try:
        sleeper.kill()
        sleeper.wait(timeout=5)
    except Exception:
        pass


HOST = kb._claimer_id().split(":", 1)[0]


def _make_running_task(
    conn,
    title: str,
    *,
    claimer: str,
    worker_pid: int | None,
    expired: bool,
    hb_age_seconds: int | None = None,
) -> str:
    """Create + claim a task and force it into a precise reclaim state."""
    tid = kb.create_task(conn, title=title, assignee="w")
    assert kb.claim_task(conn, tid, claimer=claimer) is not None
    now = int(time.time())
    if worker_pid is not None:
        kb._set_worker_pid(conn, tid, worker_pid)
    expires = now - 60 if expired else now + 900
    hb = (now - hb_age_seconds) if hb_age_seconds is not None else None
    conn.execute(
        "UPDATE tasks SET claim_expires = ?, started_at = ?, "
        "last_heartbeat_at = ? WHERE id = ?",
        (expires, now - 300, hb, tid),
    )
    conn.commit()
    return tid


def _row(conn, tid: str):
    return conn.execute(
        "SELECT status, claim_lock, worker_pid, claim_expires "
        "FROM tasks WHERE id = ?",
        (tid,),
    ).fetchone()


def _events(conn, tid: str, kind: str):
    return conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
        (tid, kind),
    ).fetchall()


# ---------------------------------------------------------------------------
# (a) EXPIRED claim + DEAD worker PID -> released, task back to ready.
#     This is the Oct 21 recovery path (the "crashed/reclaimed requeue").
# ---------------------------------------------------------------------------
def test_dead_worker_expired_claim_is_released_and_requeued(
    board, dead_pid
):
    tid = _make_running_task(
        board, "dead worker expired",
        claimer=f"{HOST}:{dead_pid}",   # Oct 21 shape: dead claimer PID
        worker_pid=dead_pid,            # ...and a dead worker PID
        expired=True,
    )
    reclaimed = kb.release_stale_claims(board)
    assert reclaimed == 1
    row = _row(board, tid)
    assert row["status"] == "ready"
    assert row["claim_lock"] is None
    assert row["worker_pid"] is None
    assert _events(board, tid, "reclaimed"), "expected a reclaim event"


# ---------------------------------------------------------------------------
# (b) EXPIRED claim + LIVE worker PID (heartbeat fresh) -> claim EXTENDED,
#     worker never touched. A live worker must survive an expired claim.
# ---------------------------------------------------------------------------
def test_live_worker_expired_claim_is_extended_not_touched(
    board, dead_pid, live_worker
):
    tid = _make_running_task(
        board, "live worker expired",
        claimer=f"{HOST}:{dead_pid}",   # dead claimer + live worker is the
        worker_pid=live_worker.pid,     # NORMAL healthy state (8/8 on the
        expired=True,                   # live board, 2026-10-22)
        hb_age_seconds=60,              # fresh heartbeat
    )
    before = _row(board, tid)
    reclaimed = kb.release_stale_claims(board)
    assert reclaimed == 0, "a live worker must never be reclaimed here"
    row = _row(board, tid)
    assert row["status"] == "running"
    assert row["worker_pid"] == before["worker_pid"]
    assert row["claim_expires"] > int(time.time()), "claim must be extended"
    assert not _events(board, tid, "reclaimed")
    assert _events(board, tid, "claim_extended"), (
        "expected the pid_alive extension event"
    )
    assert os.path.exists(f"/proc/{live_worker.pid}"), (
        "the live worker process must still be running"
    )


# ---------------------------------------------------------------------------
# (c) UNEXPIRED claim + dead worker PID -> not selected at all, untouched.
# ---------------------------------------------------------------------------
def test_dead_worker_unexpired_claim_is_untouched(board, dead_pid):
    tid = _make_running_task(
        board, "dead worker unexpired",
        claimer=f"{HOST}:{dead_pid}",
        worker_pid=dead_pid,
        expired=False,
    )
    before = _row(board, tid)
    reclaimed = kb.release_stale_claims(board)
    assert reclaimed == 0
    row = _row(board, tid)
    assert row["status"] == "running"
    assert row["claim_expires"] == before["claim_expires"], (
        "an unexpired claim must not be touched at all"
    )
    assert not _events(board, tid, "reclaimed")
    assert not _events(board, tid, "claim_extended")


# ---------------------------------------------------------------------------
# (d) EXPIRED claim + LIVE worker but heartbeat >1h stale -> reclaimed by the
#     existing heartbeat-stale backstop. This is what auto-recovers the Oct 21
#     stuck-but-alive orphans (~40 min after the manual reclaim pre-empted it).
# ---------------------------------------------------------------------------
def test_live_worker_stale_heartbeat_is_reclaimed(board, live_worker):
    tid = _make_running_task(
        board, "stale heartbeat",
        claimer=f"{HOST}:{os.getpid()}",
        worker_pid=live_worker.pid,
        expired=True,
        hb_age_seconds=2 * 3600,  # 2h > DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS
    )
    reclaimed = kb.release_stale_claims(board)
    assert reclaimed == 1
    row = _row(board, tid)
    assert row["status"] == "ready"
    assert row["claim_lock"] is None
    # the orphaned worker was terminated as part of the reclaim.
    # Reap the child first: a SIGTERM'd but un-reaped child is a zombie and
    # /proc/<pid> still exists for it (the dispatcher's _pid_alive treats
    # zombies as dead, but os.path.exists does not).
    assert live_worker.poll() is not None, (
        "the stale-heartbeat orphaned worker should have been terminated"
    )
