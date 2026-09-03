"""A Claude Code statusline for ``cswap`` (``cswap statusline``).

Renders the one-line status Claude Code shows under the prompt, styled after
the *Claude Usage Tracker* menu-bar app's statusline
(https://github.com/hamed-elfayome/Claude-Usage-Tracker):

    my-project │ ⎇ main │ Opus │ work │ Ctx: 48% │ Usage: 47% ▓▓▓▓┃░░░░░ → Reset: 4:15 PM

Claude Code invokes the configured command once per prompt render and pipes it
a small JSON blob on stdin (model, workspace dir, token counts). This module
combines that with claude-swap's own view of the active account — the profile
name and its live 5h session utilization/reset, read from the usage store with
no network — so the statusline shows *which swapped account is active* and how
close it is to its session limit.

The rendering helpers below are pure and import-safe (no switcher, no I/O), so
they unit-test without a live environment; the ``cswap statusline`` glue that
reads the store and stdin lives in ``cli._statusline_command``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# The 5h session window's fixed span, used to place the pace marker along the
# bar by elapsed time (matches the reference app's 18000s window).
SESSION_WINDOW_S = 5 * 3600
BAR_CELLS = 10
BAR_FULL = "▓"
BAR_EMPTY = "░"
PACE_MARKER = "┃"
SEP = " │ "

# ANSI 256-color codes lifted from the reference statusline so the colored
# output reads the same. Usage gradient: dark green → deep red, one step per
# 10% band. Pace tiers: comfortable → runaway.
_USAGE_GRADIENT = (22, 28, 34, 100, 142, 178, 172, 166, 160, 124)
_PACE_TIERS = ((50, 34), (75, 37), (90, 178), (100, 208), (120, 160))
_PACE_RUNAWAY = 135
_RESET = "\033[0m"


def _c(code: int, text: str, *, color: bool) -> str:
    """Wrap ``text`` in a 256-color ANSI escape, or return it plain."""
    if not color:
        return text
    return f"\033[38;5;{code}m{text}{_RESET}"


def _find_first(obj, key: str):
    """First value for ``key`` anywhere in a nested dict/list, else ``None``.

    Claude Code's statusline JSON has nested its token/context fields under
    different parents across versions; a recursive lookup keeps the parser from
    pinning to one shape.
    """
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for value in obj.values():
            found = _find_first(value, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_first(item, key)
            if found is not None:
                return found
    return None


@dataclass(frozen=True)
class StatuslineInput:
    """The parts of Claude Code's stdin JSON the statusline uses."""

    current_dir: str | None = None
    model: str | None = None
    context_pct: int | None = None


def parse_input(stdin_text: str) -> StatuslineInput:
    """Parse Claude Code's statusline JSON; unknown/broken input → empty fields.

    Context percentage is (input + cache-create + cache-read tokens) over the
    context window size, matching the reference. Missing token or window fields
    just drop the context segment rather than erroring.
    """
    try:
        data = json.loads(stdin_text) if stdin_text.strip() else {}
    except (ValueError, TypeError):
        return StatuslineInput()
    if not isinstance(data, dict):
        return StatuslineInput()

    current_dir = _find_first(data, "current_dir")
    model = _find_first(data, "display_name")

    context_pct = None
    window = _find_first(data, "context_window_size")
    if isinstance(window, (int, float)) and window > 0:
        used = 0
        for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
            value = _find_first(data, key)
            if isinstance(value, (int, float)):
                used += int(value)
        context_pct = int(used * 100 / window)

    return StatuslineInput(
        current_dir=current_dir if isinstance(current_dir, str) else None,
        model=model if isinstance(model, str) else None,
        context_pct=context_pct,
    )


def _filled_blocks(util: int) -> int:
    """Cells to fill for a utilization percent (round-half-up, clamped)."""
    if util <= 0:
        return 0
    if util >= 100:
        return BAR_CELLS
    return max(0, min(BAR_CELLS, (util * BAR_CELLS + 50) // 100))


def _pace_marker_pos(elapsed_s: int) -> int:
    """Bar cell (0..BAR_CELLS-1) for the elapsed fraction of the 5h window."""
    pos = (elapsed_s * BAR_CELLS + SESSION_WINDOW_S // 2) // SESSION_WINDOW_S
    return max(0, min(BAR_CELLS - 1, pos))


def usage_bar(
    util: int,
    reset_epoch: float | None = None,
    now: float | None = None,
    *,
    color: bool = False,
    usage_code: int | None = None,
) -> str:
    """A 10-cell ``▓``/``░`` gauge with a ``┃`` pace marker at elapsed time.

    The bar shows *consumed* usage (fills as you spend); the pace marker sits
    where the 5h clock has advanced to, so a fill lagging the marker means
    you're under pace and a fill past it means you're ahead. The marker only
    appears when ``reset_epoch``/``now`` place the clock inside the window.

    When ``color``, the marker gets its own pace color and then re-opens
    ``usage_code`` for the rest of the bar; the caller wraps the whole usage
    segment in ``usage_code``, so the marker's reset doesn't bleed.
    """
    filled = _filled_blocks(util)
    bar = " " + BAR_FULL * filled + BAR_EMPTY * (BAR_CELLS - filled)  # leading space + 10 cells

    if reset_epoch is not None and now is not None:
        remaining = reset_epoch - now
        if 0 < remaining < SESSION_WINDOW_S:
            elapsed = int(SESSION_WINDOW_S - remaining)
            idx = _pace_marker_pos(elapsed) + 1  # +1 for the leading space
            if color:
                marker = f"\033[38;5;{_pace_color(util, elapsed)}m{PACE_MARKER}{_RESET}"
                if usage_code is not None:
                    marker += f"\033[38;5;{usage_code}m"  # resume the bar's color
            else:
                marker = PACE_MARKER
            bar = bar[:idx] + marker + bar[idx + 1:]
    return bar


def _usage_color(util: int) -> int:
    """256-color code for a utilization percent (10% bands)."""
    band = min(len(_USAGE_GRADIENT) - 1, max(0, (max(0, util) - 1) // 10))
    return _USAGE_GRADIENT[band]


def _pace_color(util: int, elapsed_s: int) -> int:
    """256-color code for the pace marker, by projected end-of-window usage."""
    if elapsed_s <= 0:
        return _usage_color(util)
    projected = util * SESSION_WINDOW_S / elapsed_s
    for upper, code in _PACE_TIERS:
        if projected < upper:
            return code
    return _PACE_RUNAWAY


def format_reset(reset_epoch: float, use_24h: bool = False) -> str:
    """Local reset clock, rounded to the nearest minute (12h default)."""
    seconds_part = int(reset_epoch) % 60
    rounded = int(reset_epoch) + (60 - seconds_part if seconds_part >= 30 else -seconds_part)
    when = datetime.fromtimestamp(rounded)
    if use_24h:
        return when.strftime("%H:%M")
    # 12-hour without a leading zero on the hour (matches the reference).
    return f"{((when.hour - 1) % 12) + 1}:{when.minute:02d} {when.strftime('%p')}"


def render(
    inp: StatuslineInput,
    *,
    profile: str | None = None,
    branch: str | None = None,
    util: int | None = None,
    reset_epoch: float | None = None,
    now: float | None = None,
    color: bool = True,
    use_24h: bool = True,
) -> str:
    """Assemble the statusline from parsed stdin + claude-swap's active account.

    ``util``/``reset_epoch`` are the active account's 5h session utilization and
    reset time (from the usage store); ``profile`` is its alias or email local
    part. Any segment whose data is missing is dropped, and the separators
    collapse so there's never a dangling ``│``.
    """
    segments: list[str] = []
    if inp.current_dir:
        segments.append(_c(33, os.path.basename(inp.current_dir.rstrip("/")) or inp.current_dir, color=color))
    if branch:
        segments.append(_c(34, f"⎇ {branch}", color=color))
    if inp.model:
        segments.append(_c(178, inp.model, color=color))
    if profile:
        segments.append(_c(170, profile, color=color))
    if inp.context_pct is not None:
        segments.append(_c(37, f"Ctx: {inp.context_pct}%", color=color))
    if util is not None:
        usage_code = _usage_color(util)
        bar = usage_bar(util, reset_epoch, now, color=color, usage_code=usage_code)
        usage_seg = f"Usage: {util}%{bar}"
        if reset_epoch is not None:
            usage_seg += f" → Reset: {format_reset(reset_epoch, use_24h)}"
        if color:
            usage_seg = f"\033[38;5;{usage_code}m{usage_seg}{_RESET}"
        segments.append(usage_seg)

    sep = _c(90, SEP, color=color) if color else SEP
    return sep.join(segments)


def current_git_branch(cwd: str | None = None) -> str | None:
    """Current git branch for ``cwd`` (short-timeout, never raises)."""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    branch = out.stdout.strip()
    return branch or None


# ---- Claude Code settings.json integration (`cswap statusline --install`) ------

def default_settings_path() -> Path:
    """Claude Code's user settings file (``~/.claude/settings.json``)."""
    return Path.home() / ".claude" / "settings.json"


def _read_settings(path: Path) -> dict:
    """Read a Claude Code settings.json, tolerating a missing/broken file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_settings(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def install_statusline(path: Path, command: str = "cswap statusline") -> None:
    """Point Claude Code's ``statusLine`` at ``command``, preserving other keys."""
    data = _read_settings(path)
    data["statusLine"] = {"type": "command", "command": command, "padding": 0}
    _write_settings(path, data)


def uninstall_statusline(path: Path) -> bool:
    """Remove the ``statusLine`` key; ``False`` (no write) if it wasn't set."""
    data = _read_settings(path)
    if "statusLine" not in data:
        return False
    del data["statusLine"]
    _write_settings(path, data)
    return True
