"""Idle-headroom task scheduler (`cswap harvest`).

Unused 5h/weekly quota does not roll over, so this runs *your* queued tasks
(headless `claude -p`) during idle windows — but only when there is real headroom
and nobody else is working, and always capped so it cannot exhaust a window.

`decide_idle_run` is the pure safety core: it decides whether to spend tokens
autonomously, so every "do NOT run" branch is explicit and the default posture is
to skip. The glue (live usage, session scan, launchd) wraps it thinly.

Nothing here runs a task; execution and scheduling live in the CLI/service layer,
gated on `HarvestConfig.enabled` (off by default).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from claude_swap.settings import atomic_write_json

CONFIG_FILENAME = "harvest.json"
STATE_FILENAME = "harvest-state.json"

# Usage older than this is not trusted to gate autonomous spend (the real usage
# may already be higher, risking an overrun) — a stale reading means "skip".
FRESH_MAX_S = 300.0


# --- decision core (pure) ------------------------------------------------------

@dataclass(frozen=True)
class HarvestDecision:
    """Whether to launch a harvest task now, and a human-readable reason."""

    run: bool
    reason: str


def within_hours(now: datetime, start_hour: int | None, end_hour: int | None) -> bool:
    """Is ``now`` inside the allowed ``[start, end)`` hour window?

    ``None``/``None`` means always allowed. A window whose end is not after its
    start wraps past midnight (e.g. ``22``–``8`` = 22:00 through 08:00).
    """
    if start_hour is None or end_hour is None:
        return True
    h = now.hour
    if start_hour == end_hour:
        return True  # a full-day window
    if start_hour < end_hour:
        return start_hour <= h < end_hour
    return h >= start_hour or h < end_hour  # wraps midnight


def decide_idle_run(
    *,
    remaining_pct: float | None,
    min_remaining: float,
    active_sessions: int,
    session_scan_unreadable: int,
    tasks_available: bool,
    now: datetime,
    allow_start_hour: int | None,
    allow_end_hour: int | None,
    seconds_since_last_run: float | None,
    min_interval_s: float,
    runs_this_window: int,
    max_runs_per_window: int,
    usage_fresh: bool,
    already_running: bool,
) -> HarvestDecision:
    """Decide whether to launch a queued task now. Skip is always the safe default.

    Gates, in order (first failure wins its reason):
    already-running · no-tasks · unknown-usage · stale-usage · unreadable-sessions
    (fail-safe busy) · another-session-active · outside-hours · insufficient-headroom
    · window-cap · min-interval. Only a clean pass runs.
    """
    if already_running:
        return HarvestDecision(False, "a harvest task is already running")
    if not tasks_available:
        return HarvestDecision(False, "no tasks queued")
    if remaining_pct is None:
        return HarvestDecision(False, "usage unknown")
    if not usage_fresh:
        return HarvestDecision(False, "usage reading is stale — refusing to act on it")
    if session_scan_unreadable > 0:
        return HarvestDecision(
            False, "could not read every session record — assuming busy")
    if active_sessions > 0:
        return HarvestDecision(
            False, f"another Claude session is active ({active_sessions})")
    if not within_hours(now, allow_start_hour, allow_end_hour):
        return HarvestDecision(
            False, f"outside allowed hours ({allow_start_hour}–{allow_end_hour})")
    if remaining_pct < min_remaining:
        return HarvestDecision(
            False, f"insufficient headroom ({int(remaining_pct)}% < {int(min_remaining)}%)")
    if runs_this_window >= max_runs_per_window:
        return HarvestDecision(
            False, f"window run cap reached ({runs_this_window}/{max_runs_per_window})")
    if seconds_since_last_run is not None and seconds_since_last_run < min_interval_s:
        return HarvestDecision(False, "min interval since last run not elapsed")
    return HarvestDecision(True, f"headroom {int(remaining_pct)}%, idle, within policy")


# --- config --------------------------------------------------------------------

@dataclass(frozen=True)
class HarvestTask:
    """One queued task: a prompt run headless via ``claude -p`` in ``cwd``."""

    name: str
    prompt: str
    cwd: str


@dataclass(frozen=True)
class HarvestConfig:
    """Harvester policy. ``enabled`` is the master switch and defaults OFF."""

    enabled: bool = False
    min_remaining: float = 40.0        # only run when ≥ this % 5h quota remains
    allow_start_hour: int | None = None
    allow_end_hour: int | None = None
    min_interval_min: float = 30.0
    max_runs_per_window: int = 4
    account: str | None = None         # email to harvest on; None = active account
    tasks: tuple[HarvestTask, ...] = ()


def _config_path(backup_root: Path) -> Path:
    return backup_root / CONFIG_FILENAME


def load_config(backup_root: Path) -> HarvestConfig:
    """Load the harvester config; defaults on any missing/broken file."""
    try:
        raw = json.loads(_config_path(backup_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return HarvestConfig()
    if not isinstance(raw, dict):
        return HarvestConfig()
    tasks = tuple(
        HarvestTask(name=str(t.get("name", "")), prompt=str(t.get("prompt", "")),
                    cwd=str(t.get("cwd", "")))
        for t in raw.get("tasks", []) if isinstance(t, dict)
    )
    defaults = HarvestConfig()

    def _get(key, cast, default):
        val = raw.get(key, default)
        try:
            return cast(val) if val is not None else default
        except (TypeError, ValueError):
            return default

    return HarvestConfig(
        enabled=bool(raw.get("enabled", defaults.enabled)),
        min_remaining=_get("min_remaining", float, defaults.min_remaining),
        allow_start_hour=_get("allow_start_hour", int, defaults.allow_start_hour)
        if raw.get("allow_start_hour") is not None else None,
        allow_end_hour=_get("allow_end_hour", int, defaults.allow_end_hour)
        if raw.get("allow_end_hour") is not None else None,
        min_interval_min=_get("min_interval_min", float, defaults.min_interval_min),
        max_runs_per_window=_get("max_runs_per_window", int, defaults.max_runs_per_window),
        account=raw.get("account") if isinstance(raw.get("account"), str) else None,
        tasks=tasks,
    )


def save_config(backup_root: Path, cfg: HarvestConfig) -> None:
    """Persist the harvester config atomically."""
    data = {
        "enabled": cfg.enabled,
        "min_remaining": cfg.min_remaining,
        "allow_start_hour": cfg.allow_start_hour,
        "allow_end_hour": cfg.allow_end_hour,
        "min_interval_min": cfg.min_interval_min,
        "max_runs_per_window": cfg.max_runs_per_window,
        "account": cfg.account,
        "tasks": [{"name": t.name, "prompt": t.prompt, "cwd": t.cwd} for t in cfg.tasks],
    }
    atomic_write_json(_config_path(backup_root), data)


# --- run state (written by the runner; read here for the decision) -------------

def load_state(backup_root: Path) -> dict:
    """Read harvester run state (``last_run_ts``, ``runs_this_window``,
    ``running``); ``{}`` on any missing/broken file."""
    try:
        data = json.loads((backup_root / STATE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
