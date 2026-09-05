"""Portable UI-settings bundle for cswap — save/export the *look* and share it
across machines.

Exports the statusline + menu-bar configuration into one JSON bundle:
- global menu-bar display prefs (name/percent/icon/battery/refresh),
- whether the Claude Code statusline is wired in,
- and per-account UI keyed by **email**: custom label (alias), statusline brand
  color, per-account auto-swap threshold, and per-account title override.

**Credentials are never included.** On import, accounts are matched by email:
matched accounts get their label/color/threshold applied; unmatched ones are
reported as *pending* so the first-run setup can add them (log in → `cswap add`)
and a re-import then applies their look. Re-importing is idempotent.

The collect/apply logic is plain functions over a switcher-like object and the
settings files, so it unit-tests against a tmp backup dir + a fake switcher.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from claude_swap import statusline as sl
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.menubar import MenuBarSettings
from claude_swap.settings import (
    load_per_account_thresholds,
    load_statusline_colors,
    set_per_account_threshold,
    set_statusline_color,
)

UI_SCHEMA = "cswap-ui/1"

# The global menu-bar display prefs carried in the bundle (per-account prefs are
# in `accounts[]`; runtime-only fields like auto_switch_enabled are excluded).
_MENUBAR_FIELDS = (
    "show_account_name",
    "title_pct",
    "title_scoped",
    "show_icon",
    "title_battery",
    "refresh_interval",
)


def _menubar_path(backup_root: Path) -> Path:
    return backup_root / "menubar_settings.json"


def statusline_installed(claude_settings_path: Path) -> bool:
    """Whether Claude Code's ``statusLine`` is wired to ``cswap statusline``."""
    try:
        data = json.loads(claude_settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    cmd = data.get("statusLine", {}).get("command", "") if isinstance(data, dict) else ""
    return "cswap statusline" in cmd or "statusline" in cmd.lower()


def collect_ui_settings(switcher, *, claude_settings_path: Path | None = None) -> dict:
    """Gather the current UI settings into a portable, shareable bundle."""
    backup = switcher.backup_dir
    mb = MenuBarSettings.load(_menubar_path(backup))
    colors = load_statusline_colors(backup)
    thresholds = load_per_account_thresholds(backup)
    account_pct = mb.account_pct if isinstance(mb.account_pct, dict) else {}

    accounts: list[dict] = []
    for acc in switcher.accounts_snapshot(fetch=set()).accounts:
        email = acc.email
        entry: dict = {"email": email}
        if acc.alias:
            entry["label"] = acc.alias
        if email in colors:
            entry["color"] = colors[email]
        if email in thresholds:
            entry["threshold"] = thresholds[email]
        if account_pct.get(email):
            entry["titlePct"] = account_pct[email]
        accounts.append(entry)

    csp = claude_settings_path or sl.default_settings_path()
    return {
        "schema": UI_SCHEMA,
        "exportedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "menubar": {f: getattr(mb, f) for f in _MENUBAR_FIELDS},
        "statuslineInstalled": statusline_installed(csp),
        "accounts": accounts,
    }


def apply_ui_settings(switcher, bundle: dict, *, set_labels: bool = True) -> dict:
    """Apply a bundle to this machine; return what applied and what's pending.

    Global menu-bar prefs are applied wholesale. Per-account UI is applied only
    to accounts that already exist here (matched by email); the rest come back in
    ``pending`` so setup can add them. Individual bad values (e.g. an
    out-of-range threshold in a hand-edited bundle) are skipped and reported.
    """
    if not isinstance(bundle, dict) or bundle.get("schema") != UI_SCHEMA:
        raise ClaudeSwitchError(
            f"not a cswap UI bundle (expected schema {UI_SCHEMA!r})"
        )
    backup = switcher.backup_dir
    mb = MenuBarSettings.load(_menubar_path(backup))

    mbdata = bundle.get("menubar") or {}
    for f in _MENUBAR_FIELDS:
        if f in mbdata and isinstance(mbdata[f], type(getattr(mb, f))):
            setattr(mb, f, mbdata[f])

    by_email = {acc.email: acc for acc in switcher.accounts_snapshot(fetch=set()).accounts}
    if not isinstance(mb.account_pct, dict):
        mb.account_pct = {}

    applied: list[str] = []
    pending: list[dict] = []
    skipped: list[str] = []
    for entry in bundle.get("accounts") or []:
        email = entry.get("email")
        if not isinstance(email, str) or not email:
            continue
        if email not in by_email:
            pending.append(entry)
            continue
        did_something = False
        if set_labels and entry.get("label"):
            try:
                switcher.set_alias(email, entry["label"])
                did_something = True
            except ClaudeSwitchError as e:
                skipped.append(f"{email} label: {e}")
        if entry.get("color"):
            try:
                set_statusline_color(backup, email, entry["color"])
                did_something = True
            except ClaudeSwitchError as e:
                skipped.append(f"{email} color: {e}")
        if entry.get("threshold") is not None:
            try:
                set_per_account_threshold(backup, email, entry["threshold"])
                did_something = True
            except ClaudeSwitchError as e:
                skipped.append(f"{email} threshold: {e}")
        if entry.get("titlePct"):
            mb.account_pct[email] = entry["titlePct"]
            did_something = True
        if did_something:
            applied.append(email)

    mb.save(_menubar_path(backup))
    return {"applied": applied, "pending": pending, "skipped": skipped}


def write_bundle(path: Path, bundle: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")


def read_bundle(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ClaudeSwitchError(f"{path} is not a JSON object")
    return data
