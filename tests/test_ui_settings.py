"""Tests for the portable UI-settings bundle (``claude_swap.ui_settings``).

Uses a fake switcher (email/alias/set_alias) over a real tmp backup dir, so the
settings-file reads/writes (menu-bar prefs, statusline colors, per-account
thresholds) exercise the real code paths.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_swap import ui_settings as uis
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.menubar import MenuBarSettings
from claude_swap.models import normalize_alias


class _Acct:
    def __init__(self, number, email, alias=""):
        self.number = number
        self.email = email
        self.alias = alias


class _Snap:
    def __init__(self, accounts):
        self.accounts = accounts


class _FakeSwitcher:
    def __init__(self, backup_dir, accounts):
        self.backup_dir = backup_dir
        self._accounts = list(accounts)

    def accounts_snapshot(self, fetch=None):
        return _Snap(self._accounts)

    def set_alias(self, identifier, name):
        norm = normalize_alias(name)  # mirrors the real switcher (lowercase + validate)
        for a in self._accounts:
            if identifier in (a.email, a.number):
                a.alias = norm
                return (a.number, a.email)
        raise ClaudeSwitchError(f"No account: {identifier}")


def _sw(tmp_path, accounts):
    return _FakeSwitcher(tmp_path, accounts)


# --- collect -------------------------------------------------------------------

def test_collect_empty_defaults(tmp_path: Path):
    sw = _sw(tmp_path, [_Acct("1", "a@x.com")])
    b = uis.collect_ui_settings(sw, claude_settings_path=tmp_path / "none.json")
    assert b["schema"] == uis.UI_SCHEMA
    assert b["statuslineInstalled"] is False
    assert b["menubar"]["title_pct"] == "both"   # default
    assert b["accounts"] == [{"email": "a@x.com"}]


def test_collect_captures_per_account_ui(tmp_path: Path):
    from claude_swap.settings import set_per_account_threshold, set_statusline_color
    sw = _sw(tmp_path, [_Acct("1", "a@x.com", alias="personal")])
    set_statusline_color(tmp_path, "a@x.com", "b57edc")
    set_per_account_threshold(tmp_path, "a@x.com", 80)
    mb = MenuBarSettings(account_pct={"a@x.com": "7d"})
    mb.save(tmp_path / "menubar_settings.json")

    b = uis.collect_ui_settings(sw, claude_settings_path=tmp_path / "none.json")
    assert b["accounts"] == [
        {"email": "a@x.com", "label": "personal", "color": "b57edc",
         "threshold": 80.0, "titlePct": "7d"}
    ]


# --- apply ---------------------------------------------------------------------

def test_apply_then_collect_round_trips(tmp_path: Path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    # build a bundle on a "source" machine
    src_sw = _sw(src, [_Acct("1", "a@x.com", alias="personal")])
    from claude_swap.settings import set_per_account_threshold, set_statusline_color
    set_statusline_color(src, "a@x.com", "800000")
    set_per_account_threshold(src, "a@x.com", 90)
    mb = MenuBarSettings(show_icon=False, title_pct="7d", title_battery=True)
    mb.save(src / "menubar_settings.json")
    bundle = uis.collect_ui_settings(src_sw, claude_settings_path=tmp_path / "none.json")

    # apply on a "destination" machine that has the same account (by email)
    dst_sw = _sw(dst, [_Acct("1", "a@x.com")])
    result = uis.apply_ui_settings(dst_sw, bundle)
    assert result["applied"] == ["a@x.com"]
    assert result["pending"] == [] and result["skipped"] == []

    # collecting on the destination reproduces the bundle's UI
    got = uis.collect_ui_settings(dst_sw, claude_settings_path=tmp_path / "none.json")
    assert got["menubar"] == bundle["menubar"]
    assert got["accounts"] == bundle["accounts"]


def test_apply_reports_pending_for_unknown_account(tmp_path: Path):
    sw = _sw(tmp_path, [_Acct("1", "here@x.com")])
    bundle = {
        "schema": uis.UI_SCHEMA,
        "menubar": {},
        "accounts": [
            {"email": "here@x.com", "label": "mine"},
            {"email": "elsewhere@x.com", "label": "work", "color": "800000"},
        ],
    }
    result = uis.apply_ui_settings(sw, bundle)
    assert result["applied"] == ["here@x.com"]
    assert [p["email"] for p in result["pending"]] == ["elsewhere@x.com"]


def test_apply_rejects_non_bundle(tmp_path: Path):
    sw = _sw(tmp_path, [])
    with pytest.raises(ClaudeSwitchError):
        uis.apply_ui_settings(sw, {"not": "a bundle"})
    with pytest.raises(ClaudeSwitchError):
        uis.apply_ui_settings(sw, {"schema": "something-else"})


def test_apply_skips_bad_threshold_keeps_rest(tmp_path: Path):
    from claude_swap.settings import load_per_account_thresholds, load_statusline_colors
    sw = _sw(tmp_path, [_Acct("1", "a@x.com")])
    bundle = {
        "schema": uis.UI_SCHEMA,
        "menubar": {},
        "accounts": [{"email": "a@x.com", "color": "800000", "threshold": 5}],  # 5 < 50 floor
    }
    result = uis.apply_ui_settings(sw, bundle)
    assert result["applied"] == ["a@x.com"]          # color still applied
    assert any("threshold" in s for s in result["skipped"])
    assert load_statusline_colors(tmp_path) == {"a@x.com": "800000"}
    assert load_per_account_thresholds(tmp_path) == {}   # bad threshold not written


def test_apply_no_labels_skips_alias(tmp_path: Path):
    sw = _sw(tmp_path, [_Acct("1", "a@x.com")])
    bundle = {"schema": uis.UI_SCHEMA, "menubar": {},
              "accounts": [{"email": "a@x.com", "label": "personal", "color": "800000"}]}
    uis.apply_ui_settings(sw, bundle, set_labels=False)
    assert sw._accounts[0].alias == ""              # alias untouched


def test_apply_global_menubar_prefs(tmp_path: Path):
    sw = _sw(tmp_path, [])
    uis.apply_ui_settings(sw, {"schema": uis.UI_SCHEMA,
                               "menubar": {"show_icon": False, "title_pct": "5h"},
                               "accounts": []})
    mb = MenuBarSettings.load(tmp_path / "menubar_settings.json")
    assert mb.show_icon is False and mb.title_pct == "5h"


# --- statusline-installed detection --------------------------------------------

def test_statusline_installed_detection(tmp_path: Path):
    p = tmp_path / "settings.json"
    assert uis.statusline_installed(p) is False           # missing file
    p.write_text(json.dumps({"statusLine": {"command": "cswap statusline"}}), encoding="utf-8")
    assert uis.statusline_installed(p) is True
    p.write_text(json.dumps({"statusLine": {"command": "/other/thing"}}), encoding="utf-8")
    assert uis.statusline_installed(p) is False


# --- bundle file I/O -----------------------------------------------------------

def test_write_read_bundle(tmp_path: Path):
    path = tmp_path / "sub" / "ui.json"
    uis.write_bundle(path, {"schema": uis.UI_SCHEMA, "accounts": []})
    assert uis.read_bundle(path)["schema"] == uis.UI_SCHEMA
    (tmp_path / "bad.json").write_text("[1,2]", encoding="utf-8")
    with pytest.raises(ClaudeSwitchError):
        uis.read_bundle(tmp_path / "bad.json")
