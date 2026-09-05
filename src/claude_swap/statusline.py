"""A native Claude Code statusline for ``cswap`` (``cswap statusline``).

Renders the one-line status Claude Code shows under the prompt — active swapped
account + draining 5h usage, model + effort + context, git branch, and repo:

    UCHICAGO 54% │ Opus high 42% │ ⎇ main │ luet-apps
    └ profile+usage ┘ └ model effort ctx% ┘ └ branch ┘ └ repo ┘

Claude Code pipes a JSON payload on stdin every render (schema:
https://code.claude.com/docs/en/statusline.md). This reads its *native* live
fields — ``rate_limits.five_hour.used_percentage``, ``context_window``,
``effort.level``, ``model.display_name``, ``session_id`` — rather than
recomputing anything, and blends in claude-swap's own view of the active
account (profile name, brand color, and a switch-instant usage grace so the %
matches a freshly-switched account before Claude's payload catches up).

Everything here is pure and import-safe except the session-state file helpers
(which take an explicit path, so they unit-test against ``tmp_path``). The
``cswap statusline`` glue that reads the store and stdin lives in
``cli._statusline_command``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

SEP = " │ "
BRANCH_GLYPH = "⎇"

# Usage source switches to the store for this many seconds after an account
# change, because Claude's own ``rate_limits`` payload lags the switch (#125).
SWITCH_GRACE_S = 60.0
# Per-session state files older than this are pruned on each render.
STATE_MAX_AGE_S = 86400.0

# --- palette (24-bit truecolor hex) --------------------------------------------
# Brand + banded colors from the vault statusline note. Draining usage is shown
# as *remaining* quota (100 − used); context is shown as *used*.
PROFILE_DEFAULT_HEX = "af00af"  # magenta — any account without a set brand color
COL_MODEL = "c9d1d9"            # model + effort (neutral light)
COL_BRANCH = "3fb950"           # git branch (green)
COL_REPO = "58a6ff"             # repo / working dir (blue)
COL_SEP = "6e7681"              # the │ separators (gray)

_BRICK = "a4343a"
_GREEN = "3fb950"
_ORANGE = "e8890c"
_YELLOW = "e0c020"
_RED = "d0322b"

# Low-confidence display: a field we can't vouch for is obfuscated so a wrong
# value can never masquerade as right — critical for the profile, since it says
# which account is burning tokens. `unknown` fields render as these placeholders
# and force the whole bar red ("alert"), while trusted fields stay legible.
OBFUSCATED = "XXXXXX"
_PCT_UNKNOWN = "XX%"
_FIELDS = ("profile", "usage", "model", "context", "branch", "repo")


def _paint(hex_color: str | None, text: str, *, color: bool) -> str:
    """Wrap ``text`` in a 24-bit ANSI color, or return it plain."""
    if not color or not hex_color:
        return text
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    return f"\033[38;2;{r};{g};{b}m{text}\033[0m"


def draining_usage_color(remaining_pct: float) -> str:
    """Hex color for *remaining* 5h quota — greener with more left, red at the end."""
    if remaining_pct > 70:
        return _BRICK
    if remaining_pct > 50:
        return _GREEN
    if remaining_pct > 25:
        return _ORANGE
    if remaining_pct > 10:
        return _YELLOW
    return _RED


def context_color(used_pct: float) -> str:
    """Hex color for context-window *used* % — signals when /clear would help."""
    if used_pct <= 40:
        return _GREEN
    if used_pct <= 60:
        return _YELLOW
    if used_pct <= 80:
        return _ORANGE
    return _RED


def _dig(data, *keys):
    """Nested lookup ``data[k1][k2]…``; ``None`` if any level is missing."""
    cur = data
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


@dataclass(frozen=True)
class StatuslineInput:
    """The native Claude Code payload fields the statusline reads."""

    current_dir: str | None = None
    model: str | None = None
    session_id: str | None = None
    context_pct: float | None = None
    effort: str | None = None
    fast_mode: bool = False
    five_hour_used: float | None = None
    five_hour_resets_at: float | None = None
    seven_day_used: float | None = None


def parse_input(stdin_text: str) -> StatuslineInput:
    """Parse Claude Code's statusline JSON; unknown/broken input → empty fields.

    Reads the documented native fields directly (no token recomputation). Every
    field is optional — ``rate_limits`` only appears for Pro/Max after the first
    API response, ``context_window.used_percentage`` can be null early, and
    ``effort`` is absent on models without the parameter — so each missing piece
    just drops its segment rather than erroring.
    """
    try:
        data = json.loads(stdin_text) if stdin_text.strip() else {}
    except (ValueError, TypeError):
        return StatuslineInput()
    if not isinstance(data, dict):
        return StatuslineInput()

    def _num(value) -> float | None:
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def _str(value) -> str | None:
        return value if isinstance(value, str) and value else None

    return StatuslineInput(
        current_dir=_str(_dig(data, "workspace", "current_dir")) or _str(data.get("cwd")),
        model=_str(_dig(data, "model", "display_name")),
        session_id=_str(data.get("session_id")),
        context_pct=_num(_dig(data, "context_window", "used_percentage")),
        effort=_str(_dig(data, "effort", "level")),
        fast_mode=bool(data.get("fast_mode")),
        five_hour_used=_num(_dig(data, "rate_limits", "five_hour", "used_percentage")),
        five_hour_resets_at=_num(_dig(data, "rate_limits", "five_hour", "resets_at")),
        seven_day_used=_num(_dig(data, "rate_limits", "seven_day", "used_percentage")),
    )


def render(
    *,
    profile: str | None = None,
    profile_hex: str | None = None,
    remaining_pct: float | None = None,
    model: str | None = None,
    effort: str | None = None,
    context_pct: float | None = None,
    branch: str | None = None,
    repo: str | None = None,
    color: bool = True,
    alert: bool = False,
    unknown=(),
) -> str:
    """Assemble the ``profile usage │ model effort ctx │ ⎇ branch │ repo`` line.

    Any segment whose data is missing is dropped, and the ``│`` separators
    collapse so there's never a dangling divider. ``remaining_pct`` is the
    *remaining* 5h quota (drains toward 0); ``context_pct`` is context *used*.

    **Low-confidence UI.** ``unknown`` is a set of field names
    (``profile``/``usage``/``model``/``context``/``branch``/``repo``) we can't
    vouch for: each renders as a placeholder (``XXXXXX`` for text, ``XX%`` for a
    percent) so a wrong value never reads as right. Any ``unknown`` field — or
    an explicit ``alert=True`` — turns the **whole bar red**, but the trusted
    fields keep their real values ("without obfuscation") so you can see exactly
    which one is off.
    """
    unknown = set(unknown)
    alert = alert or bool(unknown)

    def paint(base_hex: str, text: str) -> str:
        return _paint(_RED if alert else base_hex, text, color=color)

    groups: list[str] = []

    # 1) profile + draining usage
    tokens: list[str] = []
    if profile:
        # Verbatim — the caller controls case (alias as typed, email-prefix
        # fallback lowercased); obfuscated when we can't confirm the account.
        tokens.append(paint(profile_hex or PROFILE_DEFAULT_HEX,
                            OBFUSCATED if "profile" in unknown else profile))
    if "usage" in unknown:
        tokens.append(paint(_RED, _PCT_UNKNOWN))
    elif remaining_pct is not None:
        tokens.append(paint(draining_usage_color(remaining_pct), f"{int(remaining_pct)}%"))
    if tokens:
        groups.append(" ".join(tokens))

    # 2) model + effort + context%
    tokens = []
    if model:
        label = OBFUSCATED if "model" in unknown else (f"{model} {effort}" if effort else model)
        tokens.append(paint(COL_MODEL, label))
    if "context" in unknown:
        tokens.append(paint(_RED, _PCT_UNKNOWN))
    elif context_pct is not None:
        tokens.append(paint(context_color(context_pct), f"{int(context_pct)}%"))
    if tokens:
        groups.append(" ".join(tokens))

    # 3) branch, 4) repo
    if branch:
        groups.append(paint(COL_BRANCH, f"{BRANCH_GLYPH} {OBFUSCATED if 'branch' in unknown else branch}"))
    if repo:
        groups.append(paint(COL_REPO, OBFUSCATED if "repo" in unknown else repo))

    sep = _paint(_RED if alert else COL_SEP, SEP, color=color) if color else SEP
    return sep.join(groups)


def resolve_profile_and_usage(
    *,
    active_profile: str | None,
    has_live_login: bool,
    source: str,
    store_remaining: float | None,
    payload_remaining: float | None,
) -> tuple[str | None, float | None, set[str]]:
    """Decide the profile label, remaining usage %, and which fields are untrusted.

    This is the confidence core — it guarantees the statusline never *confidently*
    shows the wrong account or usage:

    - ``active_profile`` set (cswap resolved a managed account whose identity
      matches the live login) → show it, trusted.
    - No managed active account but there **is** a live login cswap can't
      identify → obfuscate the profile (``XXXXXX``): we can't say which account
      is burning tokens, so we refuse to guess.
    - No login at all → no profile, no alarm.

    Usage prefers the store during the switch grace, else the live payload; if
    neither is available while an account (managed or unrecognized) is active,
    the usage % is flagged ``unknown`` (shown as ``XX%``).
    """
    unknown: set[str] = set()
    if active_profile is not None:
        profile: str | None = active_profile
    elif has_live_login:
        profile = OBFUSCATED
        unknown.add("profile")
    else:
        profile = None

    if source == "store" and store_remaining is not None:
        remaining = store_remaining
    elif payload_remaining is not None:
        remaining = payload_remaining
    else:
        remaining = None
        if profile is not None or has_live_login:
            unknown.add("usage")
    return profile, remaining, unknown


# --- switch-instant usage source (per-session state) ---------------------------

def usage_source(
    prev_state: dict | None,
    current_account: str | None,
    now: float,
    *,
    grace_s: float = SWITCH_GRACE_S,
) -> tuple[str, dict]:
    """Decide whether to read usage from the store or Claude's payload.

    For ``grace_s`` after the active account changes (or on first sight), prefer
    the store — it reflects the new account immediately, while Claude's
    ``rate_limits`` payload still shows the old account for ~30s. Returns
    ``("store"|"payload", new_state)``; the caller persists ``new_state``.
    """
    prev_account = prev_state.get("account") if isinstance(prev_state, dict) else None
    switched_at = prev_state.get("switched_at") if isinstance(prev_state, dict) else None
    if not isinstance(switched_at, (int, float)) or prev_account != current_account:
        switched_at = now
    new_state = {"account": current_account, "switched_at": switched_at, "updated_at": now}
    in_grace = (now - switched_at) < grace_s
    return ("store" if in_grace else "payload"), new_state


def session_state_path(state_dir: Path, session_id: str | None) -> Path:
    """Per-session state file (falls back to a shared file when no session id)."""
    safe = "".join(c for c in (session_id or "default") if c.isalnum() or c in "-_") or "default"
    return state_dir / f".statusline-{safe}.json"


def read_session_state(path: Path) -> dict:
    """Read a per-session state file; ``{}`` on any problem."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_session_state(path: Path, data: dict) -> None:
    """Write a per-session state file, best-effort (never raises)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def prune_session_states(state_dir: Path, now: float, max_age_s: float = STATE_MAX_AGE_S) -> None:
    """Delete stale ``.statusline-*.json`` state files (best-effort)."""
    try:
        entries = list(state_dir.glob(".statusline-*.json"))
    except OSError:
        return
    for entry in entries:
        try:
            if now - entry.stat().st_mtime > max_age_s:
                entry.unlink()
        except OSError:
            pass


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
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_settings(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def install_statusline(path: Path, command: str = "cswap statusline", refresh_interval: int = 30) -> None:
    """Point Claude Code's ``statusLine`` at ``command``, preserving other keys.

    ``refreshInterval`` re-runs the line every N seconds even while idle, so a
    freshly-switched account or a passing rate-limit reset shows up promptly.
    """
    data = _read_settings(path)
    data["statusLine"] = {"type": "command", "command": command, "refreshInterval": refresh_interval}
    _write_settings(path, data)


def uninstall_statusline(path: Path) -> bool:
    """Remove the ``statusLine`` key; ``False`` (no write) if it wasn't set."""
    data = _read_settings(path)
    if "statusLine" not in data:
        return False
    del data["statusLine"]
    _write_settings(path, data)
    return True
