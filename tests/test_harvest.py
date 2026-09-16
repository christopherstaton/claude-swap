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
