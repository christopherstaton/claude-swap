"""Coverage for the Claude Desktop MCP server (`cswap mcp`).

The tool handlers are plain functions over a switcher, so they test with a fake
switcher and WITHOUT the optional ``mcp`` SDK (importing the module never pulls
it). The stdio transport / SDK wiring is smoke-tested separately.
"""

from __future__ import annotations

import pytest

from claude_swap import cli
from claude_swap import mcp_server


# --- fakes ---------------------------------------------------------------------

class _Usage:
    def __init__(self, last_good=None, sentinel=None, fetched_at=100.0, age_s=5.0):
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
    def __init__(self, accounts):
        self._accounts = list(accounts)

    def accounts_snapshot(self, fetch=None):
        return _Snap(self._accounts)


# --- get_usage handler ---------------------------------------------------------

def test_usage_report_active_account():
    lg = {"five_hour": {"pct": 37}, "seven_day": {"pct": 41},
          "spend": {"used": 3.0, "limit": 10.0, "pct": 30, "currency": "USD"}}
    sw = _Switcher([_Acct("1", "c@x.com", "personal", _Usage(last_good=lg), active=True)])
    r = mcp_server.usage_report(sw)
    assert r["account"] == {"number": 1, "email": "c@x.com", "alias": "personal"}
    assert r["usageStatus"] == "ok"
    assert "fiveHour" in r["usage"]
    assert r["ageSeconds"] == 5.0


def test_usage_report_specific_by_email_and_alias():
    sw = _Switcher([
        _Acct("1", "a@x.com", "alpha", _Usage(last_good={"five_hour": {"pct": 10}}), active=True),
        _Acct("2", "b@x.com", "beta", _Usage(last_good={"five_hour": {"pct": 80}})),
    ])
    assert mcp_server.usage_report(sw, "b@x.com")["account"]["email"] == "b@x.com"
    assert mcp_server.usage_report(sw, "beta")["account"]["number"] == 2
    assert mcp_server.usage_report(sw, "2")["account"]["number"] == 2


def test_usage_report_no_active_errors():
    assert "error" in mcp_server.usage_report(_Switcher([]))


def test_usage_report_unknown_account_errors():
    sw = _Switcher([_Acct("1", "a@x.com", active=True)])
    r = mcp_server.usage_report(sw, "nobody@x.com")
    assert "error" in r and "nobody@x.com" in r["error"]


# --- list_accounts handler (draining remaining %) ------------------------------

def test_accounts_report_active_flag_and_remaining_pct():
    sw = _Switcher([
        _Acct("1", "a@x.com", "alpha", _Usage(last_good={"five_hour": {"pct": 10}}), active=True),
        _Acct("2", "b@x.com", "", _Usage(last_good={"five_hour": {"pct": 80}})),
    ])
    rows = mcp_server.accounts_report(sw)
    assert rows[0] == {"number": 1, "email": "a@x.com", "alias": "alpha",
                       "active": True, "fiveHourRemainingPct": 90.0}
    assert rows[1]["active"] is False and rows[1]["fiveHourRemainingPct"] == 20.0


def test_accounts_report_missing_usage_is_none():
    sw = _Switcher([_Acct("1", "a@x.com", active=True)])
    assert mcp_server.accounts_report(sw)[0]["fiveHourRemainingPct"] is None


# --- command path --------------------------------------------------------------

def test_cli_mcp_help_needs_no_dependency(capsys):
    cli._mcp_command(["--help"])
    out = capsys.readouterr().out
    assert "MCP server" in out and "mcp" in out


def test_cli_mcp_missing_dependency_reports_install(monkeypatch, capsys):
    def boom():
        raise ModuleNotFoundError("No module named 'mcp'", name="mcp")
    monkeypatch.setattr(mcp_server, "run_server", boom)
    with pytest.raises(SystemExit) as e:
        cli._mcp_command([])
    assert e.value.code == 1
    both = capsys.readouterr()
    assert "mcp" in (both.out + both.err)


def test_cli_mcp_reraises_unrelated_import_error(monkeypatch):
    def boom():
        raise ModuleNotFoundError("No module named 'somethingelse'", name="somethingelse")
    monkeypatch.setattr(mcp_server, "run_server", boom)
    with pytest.raises(ModuleNotFoundError):
        cli._mcp_command([])
