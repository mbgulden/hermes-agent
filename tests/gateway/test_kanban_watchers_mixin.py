"""Tests for the extracted GatewayKanbanWatchersMixin (god-file Phase 3).

The kanban watcher loops were lifted out of gateway/run.py into a mixin that
GatewayRunner inherits. These tests confirm the mixin exposes the methods and
that GatewayRunner picks them up via the MRO (behavior-neutral relocation).
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

from gateway.kanban_watchers import GatewayKanbanWatchersMixin

KANBAN_METHODS = [
    "_kanban_notifier_watcher",
    "_kanban_dispatcher_watcher",
    "_kanban_advance",
    "_kanban_unsub",
    "_kanban_rewind",
    "_deliver_kanban_artifacts",
]


def test_mixin_defines_kanban_methods():
    for m in KANBAN_METHODS:
        assert hasattr(GatewayKanbanWatchersMixin, m), f"mixin missing {m}"


def test_gateway_runner_inherits_mixin():
    # Import here so a heavy gateway import only happens if the first test passed.
    from gateway.run import GatewayRunner

    assert issubclass(GatewayRunner, GatewayKanbanWatchersMixin)
    # Each kanban method resolves to the mixin's implementation via the MRO.
    for m in KANBAN_METHODS:
        owner = next(c for c in GatewayRunner.__mro__ if m in c.__dict__)
        assert owner is GatewayKanbanWatchersMixin, (
            f"{m} resolved to {owner.__name__}, expected the mixin"
        )


def test_watcher_loops_are_coroutines():
    # The two long-running watchers are async loops.
    assert inspect.iscoroutinefunction(GatewayKanbanWatchersMixin._kanban_notifier_watcher)
    assert inspect.iscoroutinefunction(GatewayKanbanWatchersMixin._kanban_dispatcher_watcher)


def test_singleton_dispatcher_lock_is_exclusive(tmp_path):
    """Only one holder of the dispatcher lock at a time — the backstop that
    stops concurrent dispatchers double reclaiming and corrupting shared
    kanban SQLite index pages under wal_autocheckpoint=0."""
    import os

    from gateway.kanban_watchers import _acquire_singleton_lock, _release_singleton_lock

    lock = tmp_path / "kanban" / ".dispatcher.lock"

    h1, st1 = _acquire_singleton_lock(lock)
    assert st1 == "held" and h1 is not None

    # A second acquire while the first is held must be refused, not granted.
    h2, st2 = _acquire_singleton_lock(lock)
    assert st2 == "contended" and h2 is None

    # Releasing the first lets a fresh acquire succeed (lock is reusable).
    _release_singleton_lock(h1)
    h3, st3 = _acquire_singleton_lock(lock)
    assert st3 == "held" and h3 is not None
    _release_singleton_lock(h3)


# ---------------------------------------------------------------------------
# Dispatcher singleton-lock retry (t_6e9624f4)
#
# The singleton dispatcher lock is machine-global and held for the process
# lifetime. Before the fix, a gateway that found it contended at startup
# logged "will NOT dispatch" and returned for its whole lifetime; when the
# original holder later died (flock released on process exit) no gateway ever
# re-acquired it and automatic dispatch — including `enforce_max_runtime`
# reaping, which runs inside `dispatch_once` — stayed dead until someone ran a
# manual `hermes kanban dispatch`. The fix puts a contended-at-startup gateway
# in "monitor mode": it retries acquisition every tick and becomes the
# dispatcher when it wins.
# ---------------------------------------------------------------------------

class _FakeDispatcherHost:
    """Minimal stand-in for GatewayRunner: just the state the watcher touches.

    Records every non-None value assigned to ``_kanban_dispatcher_lock_handle``
    in ``seen_handles`` so assertions can observe the handle mid-run — the
    watcher releases it (and sets it to None) on normal loop exit, so the
    raw attribute is only meaningful while the loop is alive.
    """

    def __init__(self):
        self._running = True
        self.seen_handles = set()
        self._lock_handle = None

    @property
    def _kanban_dispatcher_lock_handle(self):
        return self._lock_handle

    @_kanban_dispatcher_lock_handle.setter
    def _kanban_dispatcher_lock_handle(self, value):
        self._lock_handle = value
        if value is not None:
            self.seen_handles.add(value)


class _FakeKanbanDb:
    """Stand-in for hermes_cli.kanban_db: records calls, returns fakes."""

    DEFAULT_BOARD = "default"
    DEFAULT_FAILURE_LIMIT = 3

    def __init__(self, home):
        self._home = home
        self.dispatch_calls = 0
        self.reap_calls = 0

    def kanban_home(self):
        return self._home

    def kanban_db_path(self, slug):
        return self._home / "kanban" / f"{slug}.db"

    def reap_worker_zombies(self):
        self.reap_calls += 1
        return []

    def list_boards(self, include_archived=False):
        return [{"slug": "default"}]

    def read_board_metadata(self, slug):
        return {"slug": slug}

    def connect(self, board=None):
        return None

    def dispatch_once(self, conn, **kwargs):
        self.dispatch_calls += 1
        return SimpleNamespace(
            spawned=[],
            reclaimed=0,
            crashed=[],
            timed_out=[],
            promoted=0,
            auto_blocked=[],
        )

    def has_spawnable_ready(self, conn):
        return False

    def has_spawnable_review(self, conn):
        return False


def _patch_watcher_env(monkeypatch, fake_kb, cfg, acquire_impl):
    """Point the watcher at fakes: config loader, kanban_db, lock acquire."""
    import importlib

    import gateway.kanban_watchers as kw
    import hermes_cli
    import hermes_cli.config as _cfg_mod

    monkeypatch.setattr(_cfg_mod, "load_config", lambda: cfg)
    # Import the submodule first so `kanban_db` is a bound attribute on the
    # package (the watcher's deferred `from hermes_cli import kanban_db`
    # resolves the module object from the package's namespace).
    importlib.import_module("hermes_cli.kanban_db")
    monkeypatch.setattr(hermes_cli, "kanban_db", fake_kb)
    monkeypatch.setattr(kw, "_acquire_singleton_lock", acquire_impl)


def _enabled_cfg():
    return {
        "kanban": {
            "dispatch_in_gateway": True,
            "auto_decompose": False,
            "dispatch_interval_seconds": 1,
        }
    }


def test_monitor_mode_reacquires_after_holder_releases(tmp_path, monkeypatch, caplog):
    """(a) A gateway contended at startup must acquire the lock on a later
    tick once the current holder releases it, and then dispatch for real —
    the exact Oct 21 outage scenario: every surviving gateway was contended
    at the restart wave, the original holder later died, and nothing ever
    picked dispatch back up."""
    import asyncio
    import logging

    from gateway.kanban_watchers import GatewayKanbanWatchersMixin

    host = _FakeDispatcherHost()
    fake_kb = _FakeKanbanDb(tmp_path)
    acquire_calls = {"n": 0}
    phase = {"v": "contended"}  # "another gateway" holds it at our startup

    def acquire(path):
        acquire_calls["n"] += 1
        if phase["v"] == "contended":
            return None, "contended"
        return "HOLD", "held"

    _patch_watcher_env(monkeypatch, fake_kb, _enabled_cfg(), acquire)
    caplog.set_level(logging.INFO, logger="gateway.run")

    async def scenario():
        async def release_then_stop():
            # Wait for the startup acquire (#1) and at least one monitor
            # no-op retry (#2), then simulate the other gateway dying.
            while acquire_calls["n"] < 2:
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.3)
            phase["v"] = "held"
            # Once the monitor has won and run a dispatch tick, stop.
            while fake_kb.dispatch_calls < 1:
                await asyncio.sleep(0.05)
            host._running = False

        stopper = asyncio.get_running_loop().create_task(release_then_stop())
        await GatewayKanbanWatchersMixin._kanban_dispatcher_watcher(host)
        await stopper

    asyncio.run(asyncio.wait_for(scenario(), 60))

    # It retried (startup + monitor ticks) and ended as the dispatcher.
    assert acquire_calls["n"] >= 2
    assert host.seen_handles == {"HOLD"}
    # It actually ran dispatch work after winning the lock (zombie reap +
    # at least one dispatch_once tick).
    assert fake_kb.reap_calls >= 1
    assert fake_kb.dispatch_calls >= 1
    # The transition is logged so operators can see when a monitor takes over.
    assert any("becoming dispatcher now" in r.getMessage() for r in caplog.records)


def test_startup_holder_dispatches_without_retry(tmp_path, monkeypatch, caplog):
    """(b) A gateway that won the lock at startup behaves exactly as before:
    no per-tick re-acquisition, same dispatch work, lock held for the process
    lifetime. The monitor gate must be invisible to the owner path."""
    import asyncio
    import logging

    from gateway.kanban_watchers import GatewayKanbanWatchersMixin

    host = _FakeDispatcherHost()
    fake_kb = _FakeKanbanDb(tmp_path)
    acquire_calls = {"n": 0}

    def acquire(path):
        acquire_calls["n"] += 1
        return "HOLD", "held"

    _patch_watcher_env(monkeypatch, fake_kb, _enabled_cfg(), acquire)
    caplog.set_level(logging.INFO, logger="gateway.run")

    async def scenario():
        async def stopper():
            while fake_kb.dispatch_calls < 1:
                await asyncio.sleep(0.05)
            host._running = False

        t = asyncio.get_running_loop().create_task(stopper())
        await GatewayKanbanWatchersMixin._kanban_dispatcher_watcher(host)
        await t

    asyncio.run(asyncio.wait_for(scenario(), 60))

    # Acquisition attempted exactly once (at startup) — no monitor retries.
    assert acquire_calls["n"] == 1
    assert host.seen_handles == {"HOLD"}
    assert fake_kb.reap_calls >= 1
    assert fake_kb.dispatch_calls >= 1
    # Startup "holding" log present; no monitor-transition log.
    assert any("holding singleton dispatcher lock" in r.getMessage() for r in caplog.records)
    assert not any("becoming dispatcher now" in r.getMessage() for r in caplog.records)


def test_disabled_dispatcher_never_acquires_lock(tmp_path, monkeypatch):
    """(c) dispatch_in_gateway=false (and the env escape hatch) must return
    before any lock acquisition — a disabled gateway never contends for the
    singleton lock, same as before the fix."""
    import asyncio

    from gateway.kanban_watchers import GatewayKanbanWatchersMixin

    # Config flag off (env var unset).
    host = _FakeDispatcherHost()
    fake_kb = _FakeKanbanDb(tmp_path)
    acquire_calls = {"n": 0}

    def acquire(path):
        acquire_calls["n"] += 1
        return "HOLD", "held"

    monkeypatch.delenv("HERMES_KANBAN_DISPATCH_IN_GATEWAY", raising=False)
    _patch_watcher_env(
        monkeypatch, fake_kb,
        {"kanban": {"dispatch_in_gateway": False, "auto_decompose": False}},
        acquire,
    )
    asyncio.run(GatewayKanbanWatchersMixin._kanban_dispatcher_watcher(host))
    assert acquire_calls["n"] == 0
    assert fake_kb.dispatch_calls == 0
    assert host._kanban_dispatcher_lock_handle is None

    # Env escape hatch off (config says on).
    host2 = _FakeDispatcherHost()
    fake_kb2 = _FakeKanbanDb(tmp_path)
    acquire_calls2 = {"n": 0}

    def acquire2(path):
        acquire_calls2["n"] += 1
        return "HOLD", "held"

    monkeypatch.setenv("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "false")
    _patch_watcher_env(monkeypatch, fake_kb2, _enabled_cfg(), acquire2)
    asyncio.run(GatewayKanbanWatchersMixin._kanban_dispatcher_watcher(host2))
    assert acquire_calls2["n"] == 0
    assert fake_kb2.dispatch_calls == 0
    assert host2._kanban_dispatcher_lock_handle is None
