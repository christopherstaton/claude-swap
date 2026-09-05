"""Integration coverage for ``cswap statusline`` (the command path).

Drives ``cli._statusline_command`` end-to-end with a **fake switcher** carrying
mock accounts / usage, a fake stdin payload, and a tmp backup dir — asserting
the rendered line so the displayed profile/usage/fields are guaranteed to match
the account actually active. Covers trusted display, the switch-instant grace,
account swaps, and the low-confidence obfuscation (``XXXXXX`` + red bar) that
prevents a wrong account/usage from ever reading as right.
"""

from __future__ import annotations

import io
import json
import sys
import time

from claude_swap import cli
from claude_swap import statusline as sl


# --- fakes ---------------------------------------------------------------------

class _FakeUsage:
    def __init__(self, last_good=None):
        self.last_good = last_good
        self.fetched_at = None
        self.age_s = None
        self.sentinel = None


class _FakeAcct:
    def __init__(self, number, alias, email, last_good=None, is_active=False):
        self.number = number
        self.alias = alias
        self.email = email
        self.usage = _FakeUsage(last_good)
        self.is_active = is_active


class _FakeSnap:
    def __init__(self, accounts):
        self.accounts = accounts


class _FakeSwitcher:
    """Just the surface ``_statusline_command`` touches."""

    def __init__(self, backup_dir, *, active=None, has_live=False, accounts=()):
        self.backup_dir = backup_dir
        self._active = active
        self._has_live = has_live
        self._accounts = list(accounts)

    def has_live_login(self):
        return self._has_live

    def current_account_number(self):
        return self._active

    def accounts_snapshot(self, fetch=None):
        return _FakeSnap(self._accounts)


def _run(monkeypatch, capsys, switcher, payload, *extra_args):
    monkeypatch.setattr(cli, "ClaudeAccountSwitcher", lambda **k: switcher)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    cli._statusline_command(["--no-color", "--no-branch", *extra_args])
    return capsys.readouterr().out.strip()


def _acct(n, alias, email, used_5h=None, active=False):
    lg = {"five_hour": {"pct": used_5h}} if used_5h is not None else None
    return _FakeAcct(n, alias, email, lg, is_active=active)


# --- trusted display -----------------------------------------------------------

def test_cli_trusted_account_grace_uses_store(monkeypatch, capsys, tmp_path):
    # First render after seeing the account = switch-instant grace → store usage
    # (37% used → 63% remaining), not the laggy payload (46%).
    sw = _FakeSwitcher(tmp_path, active="1", has_live=True,
                       accounts=[_acct("1", "personal", "c@x.com", 37, active=True)])
    out = _run(monkeypatch, capsys, sw,
               {"session_id": "s", "model": {"display_name": "Opus"},
                "rate_limits": {"five_hour": {"used_percentage": 46}}})
    assert out == "personal 63% │ Opus"


def test_cli_trusted_account_past_grace_uses_payload(monkeypatch, capsys, tmp_path):
    sid = "s2"
    state_dir = tmp_path / "cache"
    state_dir.mkdir()
    sl.write_session_state(sl.session_state_path(state_dir, sid),
                           {"account": "c@x.com", "switched_at": time.time() - 999})
    sw = _FakeSwitcher(tmp_path, active="1", has_live=True,
                       accounts=[_acct("1", "personal", "c@x.com", 37, active=True)])
    out = _run(monkeypatch, capsys, sw,
               {"session_id": sid, "model": {"display_name": "Opus"},
                "rate_limits": {"five_hour": {"used_percentage": 46}}})
    assert out == "personal 54% │ Opus"     # 46% used → 54% remaining (live payload)


def test_cli_email_prefix_lowercased_when_no_alias(monkeypatch, capsys, tmp_path):
    sw = _FakeSwitcher(tmp_path, active="1", has_live=True,
                       accounts=[_acct("1", "", "Chris.K.Staton@x.com", 10, active=True)])
    out = _run(monkeypatch, capsys, sw,
               {"session_id": "s", "model": {"display_name": "Opus"}})
    assert out.startswith("chris.k.staton 90% ")    # email prefix, lowercased


# --- the account swap is reflected accurately ----------------------------------

def test_cli_swap_reflects_new_account_and_usage(monkeypatch, capsys, tmp_path):
    accts = [_acct("1", "alpha", "a@x.com", 10, active=True),
             _acct("2", "beta", "b@x.com", 80)]
    sw = _FakeSwitcher(tmp_path, active="1", has_live=True, accounts=accts)
    payload = {"session_id": "sw", "model": {"display_name": "Opus"},
               "rate_limits": {"five_hour": {"used_percentage": 46}}}
    out1 = _run(monkeypatch, capsys, sw, payload)
    assert out1 == "alpha 90% │ Opus"        # A: 10% used → 90% left

    sw._active = "2"                          # a switch happened
    accts[0].is_active, accts[1].is_active = False, True
    out2 = _run(monkeypatch, capsys, sw, payload)
    assert out2 == "beta 20% │ Opus"         # B: 80% used → 20% left (switch-instant store)


# --- low confidence: never show a wrong account ---------------------------------

def test_cli_unrecognized_live_login_obfuscates_profile(monkeypatch, capsys, tmp_path):
    # A live login cswap can't identify → profile hidden, usage still from payload.
    sw = _FakeSwitcher(tmp_path, active=None, has_live=True, accounts=[])
    out = _run(monkeypatch, capsys, sw,
               {"session_id": "s3", "model": {"display_name": "Opus"},
                "rate_limits": {"five_hour": {"used_percentage": 46}}})
    assert out == "XXXXXX 54% │ Opus"


def test_cli_unrecognized_live_login_reds_bar_in_color(monkeypatch, capsys, tmp_path):
    sw = _FakeSwitcher(tmp_path, active=None, has_live=True, accounts=[])
    monkeypatch.setattr(cli, "ClaudeAccountSwitcher", lambda **k: sw)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"session_id": "s3c", "model": {"display_name": "Opus"},
         "rate_limits": {"five_hour": {"used_percentage": 46}}})))
    cli._statusline_command(["--no-branch"])          # color ON
    out = capsys.readouterr().out
    assert "XXXXXX" in out
    assert "\033[38;2;208;50;43m" in out              # _RED — whole bar alerts


def test_cli_active_account_usage_unknown_placeholder(monkeypatch, capsys, tmp_path):
    # Account known, but no store usage and no payload rate_limits → XX%, not a
    # confident number.
    sw = _FakeSwitcher(tmp_path, active="1", has_live=True,
                       accounts=[_acct("1", "personal", "c@x.com", None, active=True)])
    out = _run(monkeypatch, capsys, sw,
               {"session_id": "su", "model": {"display_name": "Opus"}})
    assert out == "personal XX% │ Opus"


def test_cli_no_login_renders_stdin_only(monkeypatch, capsys, tmp_path):
    sw = _FakeSwitcher(tmp_path, active=None, has_live=False, accounts=[])
    out = _run(monkeypatch, capsys, sw,
               {"session_id": "s4", "model": {"display_name": "Opus"},
                "context_window": {"used_percentage": 20}})
    assert out == "Opus 20%"                 # no profile, no usage, no alarm


def test_cli_switcher_failure_still_renders_stdin(monkeypatch, capsys, tmp_path):
    def _boom(**k):
        raise RuntimeError("keychain down")
    monkeypatch.setattr(cli, "ClaudeAccountSwitcher", _boom)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"session_id": "s5", "model": {"display_name": "Opus"},
         "context_window": {"used_percentage": 30}})))
    cli._statusline_command(["--no-color", "--no-branch"])
    assert capsys.readouterr().out.strip() == "Opus 30%"   # never breaks the prompt
