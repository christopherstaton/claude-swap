"""Command-path coverage for the `cswap ui` menu-bar setters (stacked/scoped/title)."""

from __future__ import annotations

from claude_swap import cli
from claude_swap.exceptions import AccountNotFoundError
from claude_swap.menubar import MenuBarSettings


class _Switcher:
    def __init__(self, backup_dir, accounts=()):
        self.backup_dir = backup_dir
        self._accounts = dict(accounts)  # ident -> (number, email, alias)

    def resolve_account(self, ident):
        for key, val in self._accounts.items():
            if ident in (key, val[1]):
                return val
        raise AccountNotFoundError(f"No account found with identifier: {ident}")


def _run(monkeypatch, tmp_path, argv, accounts=()):
    monkeypatch.setattr(cli, "ClaudeAccountSwitcher", lambda **k: _Switcher(tmp_path, accounts))
    cli._ui_command(argv)


def _load(tmp_path):
    return MenuBarSettings.load(tmp_path / "menubar_settings.json")


def test_ui_stacked_setter(monkeypatch, tmp_path, capsys):
    _run(monkeypatch, tmp_path, ["stacked", "off"])
    assert _load(tmp_path).stacked is False
    _run(monkeypatch, tmp_path, ["stacked", "on"])
    assert _load(tmp_path).stacked is True


def test_ui_scoped_setter(monkeypatch, tmp_path, capsys):
    _run(monkeypatch, tmp_path, ["scoped", "on"])
    assert _load(tmp_path).title_scoped is True
    assert "on" in capsys.readouterr().out


def test_ui_title_per_account_hides_weekly(monkeypatch, tmp_path, capsys):
    accts = {"UC": ("2", "uc@uchicago.edu", "")}
    _run(monkeypatch, tmp_path, ["title", "UC", "5h"], accts)   # UC: show 5h only, hide 7d
    assert _load(tmp_path).account_pct == {"uc@uchicago.edu": "5h"}


def test_ui_title_default_clears_override(monkeypatch, tmp_path, capsys):
    accts = {"UC": ("2", "uc@uchicago.edu", "")}
    _run(monkeypatch, tmp_path, ["title", "UC", "both"], accts)
    _run(monkeypatch, tmp_path, ["title", "uc@uchicago.edu", "default"], accts)
    assert _load(tmp_path).account_pct == {}


def test_ui_title_unknown_account_exits(monkeypatch, tmp_path):
    import pytest
    monkeypatch.setattr(cli, "ClaudeAccountSwitcher", lambda **k: _Switcher(tmp_path, {}))
    with pytest.raises(SystemExit):
        cli._ui_command(["title", "nobody", "5h"])
