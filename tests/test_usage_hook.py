"""Coverage for the `cswap usage --hook` usage-badge feature.

Pure functions (hook line, settings-hook add/remove, CLAUDE.md managed block)
are tested exhaustively; the install/uninstall glue is driven over tmp files.
Written tests-first per /advanced-test-coverage.
"""

from __future__ import annotations

import json

from claude_swap import cli
from claude_swap import statusline as sl
from claude_swap import usage_hook as uh


# --- fakes for the command-path tests ------------------------------------------

class _Usage:
    def __init__(self, last_good=None):
        self.last_good = last_good


class _Acct:
    def __init__(self, number, email, alias="", last_good=None):
        self.number = number
        self.email = email
        self.alias = alias
        self.usage = _Usage(last_good)


class _Snap:
    def __init__(self, accounts):
        self.accounts = accounts


class _Switcher:
    def __init__(self, backup_dir, active, accounts):
        self.backup_dir = backup_dir
        self._active = active
        self._accounts = list(accounts)

    def current_account_number(self):
        return self._active

    def accounts_snapshot(self, fetch=None):
        return _Snap(self._accounts)


# --- hook line -----------------------------------------------------------------

def test_hook_line_formats_percentages():
    line = uh.hook_line("personal", 63.0, 59.0)
    assert line == "cswap-usage » personal · 5h 63% left · 7d 59% left"


def test_hook_line_unknown_values_shown_as_question_not_made_up():
    line = uh.hook_line("personal", None, None)
    assert "5h ? left" in line and "7d ? left" in line


def test_hook_line_no_active_account_label():
    assert uh.hook_line(None, None, None).startswith("cswap-usage » (no active account)")


def test_hook_line_floors_fractional_percent():
    assert "5h 63% left" in uh.hook_line("p", 63.9, 10.0)


# --- settings.json UserPromptSubmit hook (pure) --------------------------------

def test_add_prompt_hook_into_empty_settings():
    out = uh.add_prompt_hook({}, "cswap usage --hook")
    assert out["hooks"]["UserPromptSubmit"] == [
        {"hooks": [{"type": "command", "command": "cswap usage --hook"}]}
    ]


def test_add_prompt_hook_is_idempotent():
    once = uh.add_prompt_hook({}, "cswap usage --hook")
    twice = uh.add_prompt_hook(once, "cswap usage --hook")
    assert once == twice


def test_add_prompt_hook_preserves_other_hooks_and_keys():
    existing = {
        "model": "opus",
        "hooks": {
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "other --thing"}]}],
            "Stop": [{"hooks": [{"type": "command", "command": "cleanup"}]}],
        },
    }
    out = uh.add_prompt_hook(existing, "cswap usage --hook")
    assert out["model"] == "opus"
    assert out["hooks"]["Stop"] == existing["hooks"]["Stop"]
    cmds = [h["command"] for g in out["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
    assert cmds == ["other --thing", "cswap usage --hook"]


def test_remove_prompt_hook_keeps_others_and_drops_empty():
    settings = uh.add_prompt_hook(
        {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "keep --me"}]}]}},
        "cswap usage --hook",
    )
    out = uh.remove_prompt_hook(settings, "cswap usage --hook")
    cmds = [h["command"] for g in out["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
    assert cmds == ["keep --me"]


def test_remove_prompt_hook_drops_hooks_key_when_empty():
    settings = uh.add_prompt_hook({}, "cswap usage --hook")
    out = uh.remove_prompt_hook(settings, "cswap usage --hook")
    assert "hooks" not in out


# --- CLAUDE.md managed block (pure) --------------------------------------------

def test_upsert_block_appends_when_absent():
    out = uh.upsert_block("# My global instructions\n\nDo the thing.\n")
    assert "# My global instructions" in out
    assert uh._BEGIN in out and uh._END in out


def test_upsert_block_replaces_existing_block_no_duplication():
    first = uh.upsert_block("prior\n")
    second = uh.upsert_block(first)
    assert second.count(uh._BEGIN) == 1
    assert second.count(uh._END) == 1
    assert second.startswith("prior")


def test_remove_block_strips_it_and_keeps_surrounding_text():
    text = uh.upsert_block("keep before\n")
    out = uh.remove_block(text)
    assert uh._BEGIN not in out and uh._END not in out
    assert "keep before" in out


def test_remove_block_noop_when_absent():
    assert uh.remove_block("nothing here\n") == "nothing here\n"


# --- install / uninstall glue --------------------------------------------------

def test_install_writes_hook_and_block(tmp_path):
    settings = tmp_path / "settings.json"
    claude_md = tmp_path / "CLAUDE.md"
    settings.write_text(json.dumps({"model": "opus"}), encoding="utf-8")
    claude_md.write_text("# existing\n", encoding="utf-8")

    uh.install(settings, claude_md)

    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["model"] == "opus"
    cmds = [h["command"] for g in data["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
    assert uh.HOOK_COMMAND in cmds
    md = claude_md.read_text(encoding="utf-8")
    assert "# existing" in md and uh._BEGIN in md


def test_install_then_uninstall_is_clean_round_trip(tmp_path):
    settings = tmp_path / "settings.json"
    claude_md = tmp_path / "CLAUDE.md"
    settings.write_text(json.dumps({"model": "opus"}), encoding="utf-8")
    claude_md.write_text("# existing\n", encoding="utf-8")

    uh.install(settings, claude_md)
    uh.uninstall(settings, claude_md)

    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data == {"model": "opus"}                  # hooks fully removed
    md = claude_md.read_text(encoding="utf-8")
    assert uh._BEGIN not in md and "# existing" in md


def test_install_is_idempotent_no_duplicate_hook(tmp_path):
    settings = tmp_path / "settings.json"
    claude_md = tmp_path / "CLAUDE.md"
    uh.install(settings, claude_md)
    uh.install(settings, claude_md)
    data = json.loads(settings.read_text(encoding="utf-8"))
    cmds = [h["command"] for g in data["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
    assert cmds.count(uh.HOOK_COMMAND) == 1
    assert claude_md.read_text(encoding="utf-8").count(uh._BEGIN) == 1


# --- command path: `cswap usage --hook / --install-hook / --uninstall-hook` -----

def test_cli_hook_prints_active_badge(monkeypatch, capsys, tmp_path):
    lg = {"five_hour": {"pct": 37}, "seven_day": {"pct": 41}}
    sw = _Switcher(tmp_path, "1", [_Acct("1", "c@x.com", "personal", lg)])
    monkeypatch.setattr(cli, "ClaudeAccountSwitcher", lambda **k: sw)
    cli._usage_hook_line()
    assert capsys.readouterr().out.strip() == "cswap-usage » personal · 5h 63% left · 7d 59% left"


def test_cli_hook_prefers_fresh_cross_window_5h(monkeypatch, capsys, tmp_path):
    # The statusline wrote a fresher (higher-used) 5h reading into the live file;
    # the badge must reflect it, not the staler store lastGood.
    (tmp_path / "cache").mkdir()
    sl.write_live_usage(sl.live_usage_path(tmp_path / "cache"),
                        {"c@x.com": {"five_hour_used": 80.0, "resets_at": 1.0, "updated_at": 1.0}})
    lg = {"five_hour": {"pct": 37}, "seven_day": {"pct": 41}}
    sw = _Switcher(tmp_path, "1", [_Acct("1", "c@x.com", "personal", lg)])
    monkeypatch.setattr(cli, "ClaudeAccountSwitcher", lambda **k: sw)
    cli._usage_hook_line()
    assert "5h 20% left" in capsys.readouterr().out    # 80% used → 20% left (live), not 63


def test_cli_hook_no_active_prints_nothing(monkeypatch, capsys, tmp_path):
    sw = _Switcher(tmp_path, None, [])
    monkeypatch.setattr(cli, "ClaudeAccountSwitcher", lambda **k: sw)
    cli._usage_hook_line()
    assert capsys.readouterr().out == ""


def test_cli_hook_never_raises_on_switcher_error(monkeypatch, capsys):
    def boom(**k):
        raise RuntimeError("nope")
    monkeypatch.setattr(cli, "ClaudeAccountSwitcher", boom)
    cli._usage_hook_line()                              # must not raise / must exit 0
    assert capsys.readouterr().out == ""


def test_cli_install_then_uninstall_hook_command(monkeypatch, capsys, tmp_path):
    settings = tmp_path / "settings.json"
    claude_md = tmp_path / "CLAUDE.md"
    monkeypatch.setattr(sl, "default_settings_path", lambda: settings)
    monkeypatch.setattr(uh, "default_claude_md_path", lambda: claude_md)

    cli._usage_command(["--install-hook"])
    msg = capsys.readouterr().out
    assert "Installed the usage-badge hook" in msg
    assert "%%" not in msg and "usage % will show" in msg   # no stray argparse escape
    data = json.loads(settings.read_text(encoding="utf-8"))
    cmds = [h["command"] for g in data["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
    assert uh.HOOK_COMMAND in cmds
    assert uh._BEGIN in claude_md.read_text(encoding="utf-8")

    cli._usage_command(["--uninstall-hook"])
    assert "Removed the usage-badge hook" in capsys.readouterr().out
    assert uh._BEGIN not in claude_md.read_text(encoding="utf-8")
