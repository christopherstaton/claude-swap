"""Command-path coverage for `cswap usage` and `cswap threshold` output.

The settings layer is tested in test_settings.py; this pins the *commands'* own
JSON/human rendering, driven with a fake switcher (mock accounts + mock usage)
over a real tmp backup dir. Written against intended behavior per
/advanced-test-coverage.
"""

from __future__ import annotations

import json

import pytest

from claude_swap import cli
from claude_swap.switcher import USAGE_API_KEY


class _Usage:
    def __init__(self, last_good=None, sentinel=None, fetched_at=None, age_s=None):
        self.last_good = last_good
        self.sentinel = sentinel
        self.fetched_at = fetched_at
        self.age_s = age_s


class _Acct:
    def __init__(self, number, email, alias="", usage=None, active=False):
        self.number = number
        self.email = email
        self.alias = alias
        self.usage = usage or _Usage()
        self.is_active = active


class _Snap:
    def __init__(self, accounts):
        self.accounts = accounts


class _Switcher:
    def __init__(self, backup_dir, accounts):
        self.backup_dir = backup_dir
        self._accounts = list(accounts)

    def accounts_snapshot(self, fetch=None):
        return _Snap(self._accounts)

    def resolve_account(self, ident):
        for a in self._accounts:
            if ident in (a.number, a.email):
                return (a.number, a.email, "")
        from claude_swap.exceptions import AccountNotFoundError
        raise AccountNotFoundError(f"No account found with identifier: {ident}")


def _patch(monkeypatch, switcher):
    monkeypatch.setattr(cli, "ClaudeAccountSwitcher", lambda **k: switcher)


# ============================ cswap usage ============================

def test_usage_active_human_full(monkeypatch, capsys, tmp_path):
    lg = {"five_hour": {"pct": 37, "clock": "06:59"}, "seven_day": {"pct": 41},
          "scoped": [{"name": "Fable", "pct": 4}],
          "spend": {"used": 3.0, "limit": 10.0, "pct": 30, "currency": "USD"}}
    _patch(monkeypatch, _Switcher(tmp_path, [
        _Acct("1", "c@x.com", "personal", _Usage(last_good=lg), active=True)]))
    cli._usage_command([])
    out = capsys.readouterr().out
    assert "personal (c@x.com)  [ok]" in out
    assert "5h session: 37% used · 63% left (resets 06:59)" in out
    assert "7d weekly: 41% used · 59% left" in out
    assert "Fable: 4% used" in out
    assert "spend: 30%" in out


def test_usage_json_structure(monkeypatch, capsys, tmp_path):
    lg = {"five_hour": {"pct": 37}}
    _patch(monkeypatch, _Switcher(tmp_path, [
        _Acct("2", "c@x.com", "dev", _Usage(last_good=lg, age_s=12.34), active=True)]))
    cli._usage_command(["--json"])
    d = json.loads(capsys.readouterr().out)
    assert d["account"] == {"number": 2, "email": "c@x.com", "alias": "dev"}
    assert d["usageStatus"] == "ok"
    assert "fiveHour" in d["usage"]
    assert d["ageSeconds"] == 12.3


def test_usage_specific_account_not_active(monkeypatch, capsys, tmp_path):
    _patch(monkeypatch, _Switcher(tmp_path, [
        _Acct("1", "a@x.com", "alpha", _Usage(last_good={"five_hour": {"pct": 10}}), active=True),
        _Acct("2", "b@x.com", "beta", _Usage(last_good={"five_hour": {"pct": 80}}))]))
    cli._usage_command(["b@x.com"])
    out = capsys.readouterr().out
    assert "beta (b@x.com)" in out
    assert "80% used · 20% left" in out
    assert "alpha" not in out


def test_usage_sentinel_status_no_windows(monkeypatch, capsys, tmp_path):
    _patch(monkeypatch, _Switcher(tmp_path, [
        _Acct("1", "a@x.com", "", _Usage(sentinel=USAGE_API_KEY), active=True)]))
    cli._usage_command([])
    out = capsys.readouterr().out
    assert "[api_key]" in out
    assert "used ·" not in out          # sentinel → no window lines


def test_usage_no_data_unavailable(monkeypatch, capsys, tmp_path):
    _patch(monkeypatch, _Switcher(tmp_path, [_Acct("1", "a@x.com", "", _Usage(), active=True)]))
    cli._usage_command([])
    out = capsys.readouterr().out
    assert "[unavailable]" in out and "used ·" not in out


def test_usage_no_active_json_error_exits(monkeypatch, capsys, tmp_path):
    _patch(monkeypatch, _Switcher(tmp_path, []))
    with pytest.raises(SystemExit) as e:
        cli._usage_command(["--json"])
    assert e.value.code == 1
    assert json.loads(capsys.readouterr().out)["error"]


def test_usage_unknown_account_exits(monkeypatch, capsys, tmp_path):
    _patch(monkeypatch, _Switcher(tmp_path, [_Acct("1", "a@x.com", active=True)]))
    with pytest.raises(SystemExit) as e:
        cli._usage_command(["nobody@x.com"])
    assert e.value.code == 1


# ============================ cswap threshold ============================

def test_threshold_list_empty(monkeypatch, capsys, tmp_path):
    _patch(monkeypatch, _Switcher(tmp_path, []))
    cli._threshold_command([])
    out = capsys.readouterr().out
    assert "Global auto-switch threshold: 90%" in out
    assert "(no per-account overrides)" in out


def test_threshold_set_then_list(monkeypatch, capsys, tmp_path):
    _patch(monkeypatch, _Switcher(tmp_path, [_Acct("1", "a@x.com")]))
    cli._threshold_command(["a@x.com", "80"])
    assert "set to 80%" in capsys.readouterr().out
    cli._threshold_command([])
    assert "a@x.com: 80%" in capsys.readouterr().out


def test_threshold_json_list(monkeypatch, capsys, tmp_path):
    _patch(monkeypatch, _Switcher(tmp_path, [_Acct("1", "a@x.com")]))
    cli._threshold_command(["a@x.com", "85"])
    capsys.readouterr()
    cli._threshold_command(["--json"])
    d = json.loads(capsys.readouterr().out)
    assert d["global"] == 90.0 and d["perAccount"] == {"a@x.com": 85.0}


def test_threshold_unset(monkeypatch, capsys, tmp_path):
    _patch(monkeypatch, _Switcher(tmp_path, [_Acct("1", "a@x.com")]))
    cli._threshold_command(["a@x.com", "80"])
    capsys.readouterr()
    cli._threshold_command(["a@x.com", "--unset"])
    assert "Cleared per-account threshold for a@x.com" in capsys.readouterr().out
    cli._threshold_command(["a@x.com", "--unset"])
    assert "No per-account threshold set" in capsys.readouterr().out


def test_threshold_show_effective_falls_back_to_global(monkeypatch, capsys, tmp_path):
    _patch(monkeypatch, _Switcher(tmp_path, [_Acct("1", "a@x.com")]))
    cli._threshold_command(["a@x.com"])
    assert capsys.readouterr().out.strip() == "a@x.com: 90%"


def test_threshold_out_of_range_exits(monkeypatch, capsys, tmp_path):
    _patch(monkeypatch, _Switcher(tmp_path, [_Acct("1", "a@x.com")]))
    with pytest.raises(SystemExit) as e:
        cli._threshold_command(["a@x.com", "40"])   # below the 50 floor
    assert e.value.code == 1
