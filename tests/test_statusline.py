"""Tests for the Claude Code statusline (``cswap statusline``).

Pure rendering/parsing helpers only — no switcher, no network. The reset-time
tests build epochs from naive *local* datetimes so ``.timestamp()`` and
``fromtimestamp`` round-trip in whatever timezone CI runs in.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from claude_swap import statusline as sl


# --- stdin parsing -------------------------------------------------------------

def test_parse_input_full():
    stdin = json.dumps({
        "model": {"display_name": "Opus"},
        "workspace": {"current_dir": "/Users/me/my-project"},
        "input_tokens": 1000,
        "cache_read_input_tokens": 95000,
        "context_window_size": 200000,
    })
    inp = sl.parse_input(stdin)
    assert inp.current_dir == "/Users/me/my-project"
    assert inp.model == "Opus"
    assert inp.context_pct == 48  # (1000 + 95000) / 200000


def test_parse_input_empty_and_broken():
    assert sl.parse_input("") == sl.StatuslineInput()
    assert sl.parse_input("not json") == sl.StatuslineInput()
    assert sl.parse_input("[1, 2, 3]") == sl.StatuslineInput()


def test_parse_input_context_dropped_without_window():
    inp = sl.parse_input(json.dumps({"input_tokens": 500}))
    assert inp.context_pct is None


# --- bar geometry --------------------------------------------------------------

def test_filled_blocks_rounds_and_clamps():
    assert sl._filled_blocks(0) == 0
    assert sl._filled_blocks(100) == 10
    assert sl._filled_blocks(47) == 5  # round(4.7)
    assert sl._filled_blocks(4) == 0   # round(0.4)
    assert sl._filled_blocks(5) == 1   # round-half-up
    assert sl._filled_blocks(150) == 10


def test_usage_bar_plain_no_marker_without_reset():
    assert sl.usage_bar(0) == " ░░░░░░░░░░"
    assert sl.usage_bar(100) == " ▓▓▓▓▓▓▓▓▓▓"
    assert sl.usage_bar(50) == " ▓▓▓▓▓░░░░░"


def test_usage_bar_pace_marker_at_elapsed_position():
    now = 1_000_000.0
    reset = now + 2 * 3600  # 3h elapsed of the 5h window → marker near cell 6
    assert sl.usage_bar(47, reset, now) == " ▓▓▓▓▓░┃░░░"


def test_usage_bar_no_marker_when_reset_outside_window():
    now = 1_000_000.0
    # reset more than 5h out (fresh window, clock not started) → no marker
    assert "┃" not in sl.usage_bar(10, now + 6 * 3600, now)
    # reset already passed → no marker
    assert "┃" not in sl.usage_bar(10, now - 60, now)


def test_pace_marker_pos_bounds():
    assert sl._pace_marker_pos(0) == 0
    assert sl._pace_marker_pos(sl.SESSION_WINDOW_S) == 9  # clamped to last cell


# --- reset time ----------------------------------------------------------------

def test_format_reset_12h_no_leading_zero():
    epoch = dt.datetime(2026, 9, 1, 16, 15, 0).timestamp()
    assert sl.format_reset(epoch) == "4:15 PM"


def test_format_reset_24h():
    epoch = dt.datetime(2026, 9, 1, 16, 15, 0).timestamp()
    assert sl.format_reset(epoch, use_24h=True) == "16:15"


def test_format_reset_rounds_to_nearest_minute():
    up = dt.datetime(2026, 9, 1, 6, 59, 45).timestamp()
    assert sl.format_reset(up, use_24h=True) == "07:00"
    down = dt.datetime(2026, 9, 1, 6, 59, 20).timestamp()
    assert sl.format_reset(down, use_24h=True) == "06:59"


def test_format_reset_noon_and_midnight():
    assert sl.format_reset(dt.datetime(2026, 9, 1, 0, 5).timestamp()) == "12:05 AM"
    assert sl.format_reset(dt.datetime(2026, 9, 1, 12, 5).timestamp()) == "12:05 PM"


# --- full line assembly --------------------------------------------------------

def test_render_full_line_plain():
    inp = sl.StatuslineInput(current_dir="/Users/me/my-project", model="Opus", context_pct=48)
    now = 1_000_000.0
    line = sl.render(
        inp, profile="work", branch="main", util=47,
        reset_epoch=now + 2 * 3600, now=now, color=False, use_24h=True,
    )
    assert line.startswith("my-project │ ⎇ main │ Opus │ work │ Ctx: 48% │ Usage: 47% ▓▓▓▓▓░┃░░░ → Reset: ")


def test_render_drops_missing_segments():
    # Only usage present: no dangling separators, no leading │.
    line = sl.render(sl.StatuslineInput(), util=10, color=False)
    assert line == "Usage: 10% ▓░░░░░░░░░"


def test_render_empty_when_nothing_available():
    assert sl.render(sl.StatuslineInput(), color=False) == ""


def test_render_color_emits_ansi_and_balances_resets():
    inp = sl.StatuslineInput(current_dir="proj", model="Opus")
    line = sl.render(inp, util=95, color=True)
    assert "\033[38;5;" in line
    # Every color opened is closed: equal count of SGR-set and reset codes.
    assert line.count("\033[38;5;") == line.count("\033[0m")


# --- settings.json install/uninstall -------------------------------------------

def test_install_statusline_adds_and_preserves(tmp_path: Path):
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")

    sl.install_statusline(path)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["theme"] == "dark"  # untouched
    assert data["statusLine"] == {"type": "command", "command": "cswap statusline", "padding": 0}


def test_install_statusline_creates_missing_file(tmp_path: Path):
    path = tmp_path / ".claude" / "settings.json"
    sl.install_statusline(path)
    assert json.loads(path.read_text(encoding="utf-8"))["statusLine"]["command"] == "cswap statusline"


def test_uninstall_statusline(tmp_path: Path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"statusLine": {"x": 1}, "theme": "dark"}), encoding="utf-8")

    assert sl.uninstall_statusline(path) is True
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "statusLine" not in data
    assert data["theme"] == "dark"

    assert sl.uninstall_statusline(path) is False  # already gone → no-op
