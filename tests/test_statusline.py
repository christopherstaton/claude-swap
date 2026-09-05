"""Tests for the native Claude Code statusline (``cswap statusline``).

Pure parsing/rendering/banding helpers plus the per-session state files — no
switcher, no network. State-file tests run against ``tmp_path``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from claude_swap import statusline as sl


# --- native payload parsing ----------------------------------------------------

_PAYLOAD = {
    "session_id": "abc123",
    "model": {"id": "claude-opus", "display_name": "Opus"},
    "workspace": {"current_dir": "/Users/me/luet-apps", "project_dir": "/Users/me/luet-apps"},
    "context_window": {"context_window_size": 200000, "used_percentage": 42},
    "effort": {"level": "high"},
    "fast_mode": True,
    "rate_limits": {
        "five_hour": {"used_percentage": 46.0, "resets_at": 1738425600},
        "seven_day": {"used_percentage": 12.0, "resets_at": 1738857600},
    },
}


def test_parse_input_full():
    inp = sl.parse_input(json.dumps(_PAYLOAD))
    assert inp.session_id == "abc123"
    assert inp.model == "Opus"
    assert inp.current_dir == "/Users/me/luet-apps"
    assert inp.context_pct == 42.0
    assert inp.effort == "high"
    assert inp.fast_mode is True
    assert inp.five_hour_used == 46.0
    assert inp.five_hour_resets_at == 1738425600.0
    assert inp.seven_day_used == 12.0


def test_parse_input_falls_back_to_cwd():
    inp = sl.parse_input(json.dumps({"cwd": "/tmp/x", "model": {"display_name": "Sonnet"}}))
    assert inp.current_dir == "/tmp/x"


def test_parse_input_absent_rate_limits_and_effort():
    # rate_limits only appears for Pro/Max after the first API response; effort
    # is absent on unsupported models. Both just drop their fields.
    inp = sl.parse_input(json.dumps({"model": {"display_name": "Haiku"}}))
    assert inp.five_hour_used is None
    assert inp.effort is None
    assert inp.context_pct is None


def test_parse_input_empty_and_broken():
    assert sl.parse_input("") == sl.StatuslineInput()
    assert sl.parse_input("not json") == sl.StatuslineInput()
    assert sl.parse_input("[1,2]") == sl.StatuslineInput()


# --- color banding -------------------------------------------------------------

def test_draining_usage_color_bands():
    assert sl.draining_usage_color(80) == "a4343a"   # >70 brick
    assert sl.draining_usage_color(60) == "3fb950"   # >50 green
    assert sl.draining_usage_color(40) == "e8890c"   # >25 orange
    assert sl.draining_usage_color(15) == "e0c020"   # >10 yellow
    assert sl.draining_usage_color(5) == "d0322b"    # <=10 red


def test_context_color_bands():
    assert sl.context_color(40) == "3fb950"   # <=40 green
    assert sl.context_color(60) == "e0c020"   # <=60 yellow
    assert sl.context_color(80) == "e8890c"   # <=80 orange
    assert sl.context_color(95) == "d0322b"   # >80 red


# --- line assembly -------------------------------------------------------------

def test_render_full_line_plain():
    line = sl.render(
        profile="UCHICAGO", profile_hex="800000", remaining_pct=54,
        model="Opus", effort="high", context_pct=42,
        branch="main", repo="luet-apps", color=False,
    )
    assert line == "UCHICAGO 54% │ Opus high 42% │ ⎇ main │ luet-apps"


def test_render_profile_verbatim_and_floors_percents():
    # The profile is shown exactly as given — no forced case (the caller lowercases
    # the email-prefix fallback and leaves an alias as typed); percents are floored.
    line = sl.render(profile="work", remaining_pct=54.9, context_pct=42.9,
                     model="Opus", color=False)
    assert line == "work 54% │ Opus 42%"
    assert sl.render(profile="chris.k.staton", model="Opus", color=False) == "chris.k.staton │ Opus"


def test_render_drops_missing_segments():
    assert sl.render(model="Opus", context_pct=10, color=False) == "Opus 10%"
    assert sl.render(profile="w", remaining_pct=90, color=False) == "w 90%"
    assert sl.render(repo="proj", color=False) == "proj"
    assert sl.render(color=False) == ""


def test_render_effort_optional():
    assert sl.render(model="Opus", color=False) == "Opus"
    assert sl.render(model="Opus", effort="max", color=False) == "Opus max"


def test_render_color_truecolor_and_balanced_resets():
    line = sl.render(profile="uchicago", profile_hex="800000", remaining_pct=54,
                     model="Opus", context_pct=42, branch="main", repo="x", color=True)
    assert "\033[38;2;128;0;0m" in line          # #800000 profile
    assert line.count("\033[38;2;") == line.count("\033[0m")  # every color closed


# --- switch-instant usage source ----------------------------------------------

def test_usage_source_first_sight_uses_store():
    source, state = sl.usage_source(None, "a@x.com", 1000.0)
    assert source == "store"
    assert state == {"account": "a@x.com", "switched_at": 1000.0, "updated_at": 1000.0}


def test_usage_source_within_grace_after_switch():
    source, state = sl.usage_source(
        {"account": "a@x.com", "switched_at": 1000.0}, "b@x.com", 1005.0
    )
    assert source == "store"           # account changed → new grace window
    assert state["switched_at"] == 1005.0


def test_usage_source_reverts_to_payload_after_grace():
    source, _ = sl.usage_source(
        {"account": "a@x.com", "switched_at": 1000.0}, "a@x.com", 1100.0
    )
    assert source == "payload"         # same account, 100s > 60s grace


def test_usage_source_still_in_grace_same_account():
    source, _ = sl.usage_source(
        {"account": "a@x.com", "switched_at": 1000.0}, "a@x.com", 1030.0
    )
    assert source == "store"           # 30s < 60s grace, no switch


# --- per-session state files ---------------------------------------------------

def test_session_state_path_sanitizes_and_defaults(tmp_path: Path):
    assert sl.session_state_path(tmp_path, "abc-123").name == ".statusline-abc-123.json"
    assert sl.session_state_path(tmp_path, None).name == ".statusline-default.json"
    assert sl.session_state_path(tmp_path, "../evil/x").name == ".statusline-evilx.json"


def test_session_state_round_trip_and_bad_read(tmp_path: Path):
    path = tmp_path / ".statusline-s.json"
    sl.write_session_state(path, {"account": "a@x.com", "switched_at": 1.0})
    assert sl.read_session_state(path) == {"account": "a@x.com", "switched_at": 1.0}
    assert sl.read_session_state(tmp_path / "missing.json") == {}
    path.write_text("{ broken", encoding="utf-8")
    assert sl.read_session_state(path) == {}


def test_prune_session_states_removes_stale_only(tmp_path: Path):
    fresh = tmp_path / ".statusline-fresh.json"
    stale = tmp_path / ".statusline-stale.json"
    fresh.write_text("{}", encoding="utf-8")
    stale.write_text("{}", encoding="utf-8")
    now = 2_000_000.0
    os.utime(stale, (now - 200000, now - 200000))  # > 1 day old
    os.utime(fresh, (now - 10, now - 10))
    sl.prune_session_states(tmp_path, now)
    assert fresh.exists()
    assert not stale.exists()


# --- settings.json install/uninstall -------------------------------------------

def test_install_sets_command_and_refresh(tmp_path: Path):
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")

    sl.install_statusline(path)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["theme"] == "dark"  # untouched
    assert data["statusLine"] == {
        "type": "command", "command": "cswap statusline", "refreshInterval": 30,
    }


def test_uninstall_statusline(tmp_path: Path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"statusLine": {"x": 1}, "theme": "dark"}), encoding="utf-8")
    assert sl.uninstall_statusline(path) is True
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "statusLine" not in data and data["theme"] == "dark"
    assert sl.uninstall_statusline(path) is False


# ============================================================================
# Low-confidence / alert UI + active-account confidence (TDD: written first)
# ============================================================================

def test_obfuscated_constant():
    assert sl.OBFUSCATED == "XXXXXX"


def test_render_alert_reds_whole_bar_keeps_values():
    # alert = something is off; the whole bar goes red but real values stay
    # legible ("without obfuscation") so you can see which field is wrong.
    line = sl.render(profile="personal", remaining_pct=54, model="Opus",
                     context_pct=42, branch="main", repo="app", color=True, alert=True)
    assert "personal" in line and "54%" in line and "main" in line  # values kept
    # every colored run is red, and every color is closed
    import re
    codes = re.findall(r"\033\[38;2;(\d+;\d+;\d+)m", line)
    assert codes and all(c == "208;50;43" for c in codes)  # _RED #d0322b
    assert line.count("\033[38;2;") == line.count("\033[0m")


def test_render_unknown_profile_obfuscates_and_reds():
    line = sl.render(profile="personal", remaining_pct=54, model="Opus", color=False,
                     unknown={"profile"})
    assert line == "XXXXXX 54% │ Opus"      # profile hidden, rest legible


def test_render_unknown_usage_shows_pct_placeholder():
    line = sl.render(profile="personal", remaining_pct=54, model="Opus", color=False,
                     unknown={"usage"})
    assert line == "personal XX% │ Opus"    # usage can't be trusted → XX%


def test_render_unknown_context_placeholder():
    line = sl.render(model="Opus", context_pct=42, color=False, unknown={"context"})
    assert line == "Opus XX%"


def test_render_unknown_multiple_and_implies_alert_color():
    line = sl.render(profile="p", remaining_pct=9, model="Opus", branch="main",
                     repo="app", color=True, unknown={"profile", "usage"})
    # obfuscated fields present, and the bar is all red (unknown implies alert)
    assert "XXXXXX" in line and "XX%" in line
    import re
    codes = set(re.findall(r"\033\[38;2;(\d+;\d+;\d+)m", line))
    assert codes == {"208;50;43"}


def test_render_full_obfuscation_across_bar():
    line = sl.render(profile="p", remaining_pct=1, model="Opus", context_pct=99,
                     branch="main", repo="app", color=False,
                     unknown={"profile", "usage", "model", "context", "branch", "repo"})
    assert line == "XXXXXX XX% │ XXXXXX XX% │ ⎇ XXXXXX │ XXXXXX"


# --- resolve_profile_and_usage: the confidence decision (pure) ---------------

def test_resolve_trusted_account_payload_usage():
    profile, remaining, unknown = sl.resolve_profile_and_usage(
        active_profile="personal", has_live_login=True, source="payload",
        store_remaining=None, payload_remaining=54)
    assert (profile, remaining, unknown) == ("personal", 54, set())


def test_resolve_trusted_account_store_usage_during_grace():
    profile, remaining, unknown = sl.resolve_profile_and_usage(
        active_profile="personal", has_live_login=True, source="store",
        store_remaining=63, payload_remaining=54)
    assert (profile, remaining, unknown) == ("personal", 63, set())


def test_resolve_unrecognized_live_login_obfuscates_profile():
    # A live login cswap can't identify → we must NOT show a confident name.
    profile, remaining, unknown = sl.resolve_profile_and_usage(
        active_profile=None, has_live_login=True, source="payload",
        store_remaining=None, payload_remaining=None)
    assert profile == sl.OBFUSCATED
    assert "profile" in unknown
    assert "usage" in unknown           # no usage either → also flagged
    assert remaining is None


def test_resolve_unrecognized_live_login_still_shows_payload_usage():
    profile, remaining, unknown = sl.resolve_profile_and_usage(
        active_profile=None, has_live_login=True, source="payload",
        store_remaining=None, payload_remaining=71)
    assert profile == sl.OBFUSCATED and "profile" in unknown
    assert remaining == 71 and "usage" not in unknown


def test_resolve_no_login_shows_nothing_no_alert():
    profile, remaining, unknown = sl.resolve_profile_and_usage(
        active_profile=None, has_live_login=False, source="payload",
        store_remaining=None, payload_remaining=None)
    assert (profile, remaining, unknown) == (None, None, set())


def test_resolve_trusted_account_but_usage_unknown_flags_usage():
    profile, remaining, unknown = sl.resolve_profile_and_usage(
        active_profile="personal", has_live_login=True, source="store",
        store_remaining=None, payload_remaining=None)
    assert profile == "personal" and remaining is None
    assert unknown == {"usage"}         # know the account, can't vouch for usage
