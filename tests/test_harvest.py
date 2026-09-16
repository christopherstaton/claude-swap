"""Coverage for the idle-headroom task scheduler (`cswap harvest`).

The decision core `decide_idle_run` is pure and gets exhaustive gate coverage —
it is the safety boundary that decides whether to spend tokens autonomously, so
every "do NOT run" path is asserted explicitly. Config round-trips over tmp files.
Written tests-first per /advanced-test-coverage.
"""

from __future__ import annotations

from datetime import datetime

from claude_swap import harvest as hv


def _decide(**over):
    base = dict(
        remaining_pct=80.0,
        min_remaining=40.0,
        active_sessions=0,
        session_scan_unreadable=0,
        tasks_available=True,
        now=datetime(2026, 1, 1, 3, 0),
        allow_start_hour=None,
        allow_end_hour=None,
        seconds_since_last_run=9999.0,
        min_interval_s=1800.0,
        runs_this_window=0,
        max_runs_per_window=4,
        usage_fresh=True,
        already_running=False,
    )
    base.update(over)
    return hv.decide_idle_run(**base)


# --- the one "yes" path --------------------------------------------------------

def test_runs_when_idle_with_headroom():
    d = _decide()
    assert d.run is True
    assert "headroom" in d.reason and "80%" in d.reason


def test_boundary_remaining_equals_min_runs():
    assert _decide(remaining_pct=40.0, min_remaining=40.0).run is True


# --- every "no" path (safety) --------------------------------------------------

def test_skip_when_already_running():
    d = _decide(already_running=True)
    assert d.run is False and "already" in d.reason.lower()


def test_skip_when_no_tasks_queued():
    assert _decide(tasks_available=False).run is False


def test_skip_when_usage_stale_never_acts_on_stale_percent():
    d = _decide(usage_fresh=False)
    assert d.run is False and "stale" in d.reason.lower()


def test_skip_when_remaining_unknown():
    assert _decide(remaining_pct=None).run is False


def test_unknown_usage_reported_as_unknown_not_stale():
    # No data at all reads as "unknown", not "stale" (self-review fix).
    d = _decide(remaining_pct=None, usage_fresh=False)
    assert d.run is False and "unknown" in d.reason.lower()


def test_skip_when_sessions_unreadable_fail_safe_busy():
    # can't read every session record → assume busy, never run underneath one
    d = _decide(session_scan_unreadable=1)
    assert d.run is False and ("unreadable" in d.reason.lower() or "assum" in d.reason.lower())


def test_skip_when_another_session_active():
    d = _decide(active_sessions=1)
    assert d.run is False and "active" in d.reason.lower()


def test_skip_when_insufficient_headroom():
    d = _decide(remaining_pct=30.0, min_remaining=40.0)
    assert d.run is False and "30%" in d.reason and "40%" in d.reason


def test_skip_when_outside_allowed_hours():
    # allowed 22:00–08:00; now is 12:00 → outside
    d = _decide(now=datetime(2026, 1, 1, 12, 0), allow_start_hour=22, allow_end_hour=8)
    assert d.run is False and "hours" in d.reason.lower()


def test_skip_when_window_run_cap_reached():
    d = _decide(runs_this_window=4, max_runs_per_window=4)
    assert d.run is False and "cap" in d.reason.lower()


def test_skip_when_min_interval_not_elapsed():
    d = _decide(seconds_since_last_run=60.0, min_interval_s=1800.0)
    assert d.run is False and "interval" in d.reason.lower()


def test_skip_when_never_run_before_but_interval_ok():
    # None = never run before → interval gate passes
    assert _decide(seconds_since_last_run=None).run is True


# --- allowed-hours window logic (pure) -----------------------------------------

def test_within_hours_none_means_always():
    assert hv.within_hours(datetime(2026, 1, 1, 3, 0), None, None) is True


def test_within_hours_same_day_window():
    assert hv.within_hours(datetime(2026, 1, 1, 12, 0), 9, 17) is True
    assert hv.within_hours(datetime(2026, 1, 1, 3, 0), 9, 17) is False


def test_within_hours_overnight_wrap():
    assert hv.within_hours(datetime(2026, 1, 1, 3, 0), 22, 8) is True   # 3am inside 22–8
    assert hv.within_hours(datetime(2026, 1, 1, 23, 0), 22, 8) is True  # 11pm inside
    assert hv.within_hours(datetime(2026, 1, 1, 12, 0), 22, 8) is False  # noon outside


# --- config round-trip ---------------------------------------------------------

def test_config_defaults_are_safe():
    cfg = hv.HarvestConfig()
    assert cfg.enabled is False            # master switch OFF by default
    assert cfg.min_remaining >= 25.0       # keeps real reserve
    assert cfg.tasks == ()


def test_config_save_load_round_trip(tmp_path):
    cfg = hv.HarvestConfig(
        enabled=True, min_remaining=50.0, allow_start_hour=22, allow_end_hour=8,
        max_runs_per_window=3, account="c@x.com",
        tasks=(hv.HarvestTask(name="docs", prompt="update the changelog", cwd="/repo"),),
    )
    hv.save_config(tmp_path, cfg)
    got = hv.load_config(tmp_path)
    assert got == cfg
    assert got.tasks[0].name == "docs" and got.tasks[0].cwd == "/repo"


def test_load_config_missing_returns_defaults(tmp_path):
    assert hv.load_config(tmp_path) == hv.HarvestConfig()


def test_load_config_ignores_garbage(tmp_path):
    (tmp_path / "harvest.json").write_text("{ broken", encoding="utf-8")
    assert hv.load_config(tmp_path) == hv.HarvestConfig()


# --- command path: `cswap harvest ...` -----------------------------------------

from claude_swap import cli  # noqa: E402
from claude_swap import process_detection as pd  # noqa: E402


class _Usage:
    def __init__(self, last_good=None, age_s=None):
        self.last_good = last_good
        self.age_s = age_s


class _Acct:
    def __init__(self, email, alias="", last_good=None, age_s=None, active=False):
        self.email = email
        self.alias = alias
        self.usage = _Usage(last_good, age_s)
        self.is_active = active


class _Snap:
    def __init__(self, accounts):
        self.accounts = accounts


class _Switcher:
    def __init__(self, backup_dir, accounts=()):
        self.backup_dir = backup_dir
        self._accounts = list(accounts)

    def accounts_snapshot(self, fetch=None):
        return _Snap(self._accounts)


def _cli(monkeypatch, tmp_path, argv, accounts=()):
    monkeypatch.setattr(cli, "ClaudeAccountSwitcher", lambda **k: _Switcher(tmp_path, accounts))
    cli._harvest_command(argv)


def test_cli_enable_then_disable(monkeypatch, capsys, tmp_path):
    _cli(monkeypatch, tmp_path, ["enable"])
    assert hv.load_config(tmp_path).enabled is True
    _cli(monkeypatch, tmp_path, ["disable"])
    assert hv.load_config(tmp_path).enabled is False


def test_cli_set_params_persist(monkeypatch, capsys, tmp_path):
    _cli(monkeypatch, tmp_path,
         ["set", "--min-remaining", "55", "--start-hour", "22", "--end-hour", "8", "--max-runs", "2"])
    cfg = hv.load_config(tmp_path)
    assert cfg.min_remaining == 55.0
    assert cfg.allow_start_hour == 22 and cfg.allow_end_hour == 8
    assert cfg.max_runs_per_window == 2


def test_cli_set_clear_hours(monkeypatch, capsys, tmp_path):
    _cli(monkeypatch, tmp_path, ["set", "--start-hour", "22", "--end-hour", "8"])
    _cli(monkeypatch, tmp_path, ["set", "--clear-hours"])
    cfg = hv.load_config(tmp_path)
    assert cfg.allow_start_hour is None and cfg.allow_end_hour is None


def test_cli_add_list_remove_task(monkeypatch, capsys, tmp_path):
    _cli(monkeypatch, tmp_path, ["add-task", "--name", "docs", "--cwd", "/r", "--prompt", "do docs"])
    assert hv.load_config(tmp_path).tasks[0].name == "docs"
    _cli(monkeypatch, tmp_path, ["tasks"])
    assert "docs" in capsys.readouterr().out
    _cli(monkeypatch, tmp_path, ["remove-task", "docs"])
    assert hv.load_config(tmp_path).tasks == ()


def test_cli_status_would_run_when_armed_and_idle(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(pd, "scan_sessions", lambda *a, **k: ([], 0))     # no other sessions
    acc = _Acct("c@x.com", "personal", {"five_hour": {"pct": 10}}, age_s=1.0, active=True)  # 90% left
    hv.save_config(tmp_path, hv.HarvestConfig(enabled=True, min_remaining=40,
                   tasks=(hv.HarvestTask("t", "p", "/r"),)))
    _cli(monkeypatch, tmp_path, ["status"], accounts=[acc])
    out = capsys.readouterr().out
    assert "ARMED" in out and "WOULD RUN" in out


def test_cli_status_disarmed_reports_would_skip_even_with_open_gate(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(pd, "scan_sessions", lambda *a, **k: ([], 0))
    acc = _Acct("c@x.com", "personal", {"five_hour": {"pct": 10}}, age_s=1.0, active=True)  # gate open
    hv.save_config(tmp_path, hv.HarvestConfig(enabled=False, min_remaining=40,
                   tasks=(hv.HarvestTask("t", "p", "/r"),)))
    _cli(monkeypatch, tmp_path, ["status"], accounts=[acc])
    out = capsys.readouterr().out
    assert "disarmed" in out and "would skip (disarmed)" in out


def test_cli_status_busy_session_blocks(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(pd, "scan_sessions", lambda *a, **k: (["s1"], 0))   # one active
    acc = _Acct("c@x.com", "personal", {"five_hour": {"pct": 10}}, age_s=1.0, active=True)
    hv.save_config(tmp_path, hv.HarvestConfig(enabled=True, min_remaining=40,
                   tasks=(hv.HarvestTask("t", "p", "/r"),)))
    _cli(monkeypatch, tmp_path, ["status"], accounts=[acc])
    out = capsys.readouterr().out
    assert "would skip" in out and "active" in out


# --- executor: pure helpers ----------------------------------------------------

def test_task_argv_prompt_vs_command():
    assert hv.task_argv(hv.HarvestTask("t", prompt="do it", cwd="/r")) == ["claude", "-p", "do it"]
    assert hv.task_argv(hv.HarvestTask("t", command="make test", cwd="/r")) == ["/bin/sh", "-c", "make test"]


def test_next_window_state_resets_after_window():
    assert hv.next_window_state({}, 1000.0) == (0, 1000.0)                    # first ever
    assert hv.next_window_state({"runs_this_window": 2, "window_start_ts": 990.0}, 1000.0) == (2, 990.0)
    assert hv.next_window_state({"runs_this_window": 2, "window_start_ts": 0.0}, 1000.0 + hv.WINDOW_S) == (0, 1000.0 + hv.WINDOW_S)


def test_pick_task_round_robin():
    tasks = (hv.HarvestTask("a", "p", "/r"), hv.HarvestTask("b", "p", "/r"))
    assert hv.pick_task(tasks, 0) == (tasks[0], 1)
    assert hv.pick_task(tasks, 1) == (tasks[1], 0)
    assert hv.pick_task((), 0) == (None, 0)


def test_lock_is_stale():
    assert hv.lock_is_stale({}, 100.0) is True                               # no lock
    assert hv.lock_is_stale({"running": True, "running_since": 100.0}, 150.0) is False  # fresh
    assert hv.lock_is_stale({"running": True, "running_since": 0.0}, hv.LOCK_TTL_S + 1) is True  # aged out


# --- executor: the runner (`cli._harvest_run`, injected seams) -----------------

class _RunSwitcher:
    def __init__(self, backup_dir):
        self.backup_dir = backup_dir


class _Exec:
    def __init__(self, code=0):
        self.calls = []
        self.code = code

    def __call__(self, task, timeout_s):
        self.calls.append((task, timeout_s))
        return self.code


_OPEN = (90.0, True, "personal", 0, 0)     # remaining, fresh, profile, active, unreadable
_BUSY = (90.0, True, "personal", 1, 0)


def _run(tmp_path, cfg, gather_ret=_OPEN, code=0, dry_run=False):
    ex = _Exec(code)
    res = cli._harvest_run(_RunSwitcher(tmp_path), cfg, dry_run=dry_run,
                           gather=lambda sw, c: gather_ret, execute=ex)
    return res, ex


def _task(name="t"):
    return hv.HarvestTask(name, prompt="p", cwd="/r")


def test_run_disarmed_never_executes(tmp_path):
    res, ex = _run(tmp_path, hv.HarvestConfig(enabled=False, tasks=(_task(),)))
    assert res["ran"] is False and ex.calls == []


def test_run_skips_when_busy_no_execute(tmp_path):
    cfg = hv.HarvestConfig(enabled=True, min_remaining=40, tasks=(_task(),))
    res, ex = _run(tmp_path, cfg, gather_ret=_BUSY)
    assert res["ran"] is False and ex.calls == []


def test_run_dry_run_evaluates_without_executing(tmp_path):
    cfg = hv.HarvestConfig(enabled=True, min_remaining=40, tasks=(_task(),))
    res, ex = _run(tmp_path, cfg, dry_run=True)
    assert res["reason"] == "dry-run" and res["task"] == "t" and ex.calls == []


def test_run_executes_and_records_state(tmp_path):
    cfg = hv.HarvestConfig(enabled=True, min_remaining=40, per_run_timeout_min=10,
                           tasks=(_task(),))
    res, ex = _run(tmp_path, cfg, code=0)
    assert res == {"ran": True, "task": "t", "exit_code": 0}
    assert ex.calls[0][0].name == "t" and ex.calls[0][1] == 600.0    # timeout seconds
    st = hv.load_state(tmp_path)
    assert st["runs_this_window"] == 1 and st["last_task"] == "t" and st["running"] is False


def test_run_window_cap_blocks_execute(tmp_path):
    import time
    cfg = hv.HarvestConfig(enabled=True, min_remaining=40, max_runs_per_window=1, tasks=(_task(),))
    hv.save_state(tmp_path, {"runs_this_window": 1, "window_start_ts": time.time()})
    res, ex = _run(tmp_path, cfg)
    assert res["ran"] is False and "cap" in res["reason"].lower() and ex.calls == []


def test_run_lock_blocks_concurrent(tmp_path):
    import time
    cfg = hv.HarvestConfig(enabled=True, min_remaining=40, tasks=(_task(),))
    hv.save_state(tmp_path, {"running": True, "running_since": time.time()})
    res, ex = _run(tmp_path, cfg)
    assert res["reason"] == "already running" and ex.calls == []


def test_run_round_robin_advances(tmp_path):
    cfg = hv.HarvestConfig(enabled=True, min_remaining=40, min_interval_min=0,
                           tasks=(_task("a"), _task("b")))
    r1, _ = _run(tmp_path, cfg)
    r2, _ = _run(tmp_path, cfg)
    assert r1["task"] == "a" and r2["task"] == "b"
