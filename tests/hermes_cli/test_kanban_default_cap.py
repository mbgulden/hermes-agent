"""Default runtime cap: every task carries a cap (2026-11-10 build).

Pins the two binding layers:
- kernel: ``kanban_db.create_task()`` normalizes
  ``max_runtime_seconds=None`` to ``DEFAULT_MAX_RUNTIME_SECONDS``
  (14400) — the single choke point under the CLI, swarm, and dashboard
  writers, so no creation path can skip the cap;
- schema: the tasks-table DDL carries ``DEFAULT 14400`` for any writer
  that bypasses the kernel and omits the column.

An explicit cap is always honored as-is.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_swarm import SwarmWorkerSpec, create_swarm

DEFAULT = kb.DEFAULT_MAX_RUNTIME_SECONDS


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _cap(conn, task_id):
    row = conn.execute(
        "SELECT max_runtime_seconds FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    assert row is not None
    return row[0]


def test_default_cap_constant_is_four_hours():
    assert DEFAULT == 14400


def test_create_without_cap_gets_default(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="no cap given")
        assert _cap(conn, task_id) == DEFAULT


def test_create_with_explicit_cap_keeps_it(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="ten minutes", max_runtime_seconds=600)
        assert _cap(conn, task_id) == 600


def test_fresh_schema_carries_column_default(kanban_home):
    with kb.connect() as conn:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchone()[0]
    assert "max_runtime_seconds" in sql
    assert "DEFAULT 14400" in sql


def test_raw_insert_omitting_column_gets_default(kanban_home):
    """Schema backstop: a non-kernel writer that omits the column
    still lands the default."""
    with kb.connect() as conn:
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at) "
            "VALUES ('t_raw', 'raw insert', 'ready', 1)"
        )
        assert _cap(conn, "t_raw") == DEFAULT


def test_raw_insert_explicit_null_stays_null(kanban_home):
    """Documents the layering: the schema default fires only when the
    column is omitted. An explicit NULL still lands NULL — which is
    why the kernel normalization in create_task(), not the schema,
    is the binding layer for writers that name the column."""
    with kb.connect() as conn:
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at, "
            "max_runtime_seconds) VALUES ('t_null', 'explicit null', "
            "'ready', 1, NULL)"
        )
        assert _cap(conn, "t_null") is None


def test_cli_create_without_max_runtime_gets_default(kanban_home):
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    args = parser.parse_args(["kanban", "create", "cli task"])
    assert kc.kanban_command(args) == 0
    with kb.connect() as conn:
        tasks = kb.list_tasks(conn)
    assert len(tasks) == 1
    assert tasks[0].max_runtime_seconds == DEFAULT


def test_swarm_create_gets_default_on_every_card(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        created = create_swarm(
            conn,
            goal="Default-cap swarm probe.",
            workers=[SwarmWorkerSpec(profile="w1", title="Work", body="Do it")],
            verifier_assignee="reviewer",
            synthesizer_assignee="writer",
            created_by="orchestrator",
        )
        ids = [
            created.root_id,
            *created.worker_ids,
            created.verifier_id,
            created.synthesizer_id,
        ]
        for task_id in ids:
            assert _cap(conn, task_id) == DEFAULT
    finally:
        conn.close()


def test_dashboard_create_without_cap_gets_default(kanban_home):
    from plugins.kanban.dashboard.plugin_api import (
        CreateTaskBody,
        create_task,
    )

    # board= is passed explicitly: called outside FastAPI, the route's
    # Query(None) default would otherwise leak through unresolved.
    result = create_task(CreateTaskBody(title="dashboard task"), board="default")
    task = result["task"]
    assert task is not None
    assert task["max_runtime_seconds"] == DEFAULT
