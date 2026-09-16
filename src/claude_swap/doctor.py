"""`cswap doctor` — one-shot health check of a cswap setup.

Aggregates the state of everything a cswap install can wire up — accounts, usage
freshness, the statusline, the response badge hook, the menu-bar service, the
harvester, and the optional MCP server — into a single pass/warn/off report with
a fix hint per line.

``build_report`` is pure: it takes plain gathered inputs and returns the checks,
so it is exhaustively testable. The CLI glue gathers the inputs (read-only, no
network) and renders them.
"""

from __future__ import annotations

from dataclasses import dataclass

# How old the usage store may be before the badge/statusline read as stale.
STORE_STALE_S = 900.0

_GLYPH = {"ok": "✓", "warn": "⚠", "off": "○", "error": "✗"}


@dataclass(frozen=True)
class Check:
    """One health-check line: a status, what was found, and how to fix it."""

    name: str
    status: str  # "ok" | "warn" | "off" | "error"
    detail: str
    hint: str = ""

    @property
    def glyph(self) -> str:
        return _GLYPH.get(self.status, "?")


def _fmt_age(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    s = int(seconds)
    if s < 90:
        return f"{s}s ago"
    if s < 5400:
        return f"{s // 60}m ago"
    return f"{s // 3600}h ago"


def build_report(
    *,
    distribution: str,
    version: str,
    account_count: int,
    active_profile: str | None,
    error_accounts: int = 0,
    store_age_s: float | None,
    statusline_installed: bool,
    badge_installed: bool,
    harvest_enabled: bool,
    harvest_tasks: int,
    mcp_available: bool,
    is_macos: bool,
    menubar_service_installed: bool = False,
    menubar_service_loaded: bool = False,
    claude_login_keychain_count: int | None = None,
    cswap_backup_keychain_count: int | None = None,
) -> list[Check]:
    """Assemble the health-check report (pure) from gathered inputs."""
    checks: list[Check] = []

    checks.append(Check("package", "ok", f"{distribution} {version}"))

    # Accounts
    if account_count == 0:
        checks.append(Check("accounts", "warn", "no managed accounts",
                            "log in to Claude Code, then `cswap add`"))
    else:
        active = active_profile or "none active"
        detail = f"{account_count} managed · active: {active}"
        if error_accounts:
            checks.append(Check("accounts", "warn",
                                f"{detail} · {error_accounts} with errors",
                                "`cswap list` to inspect"))
        else:
            checks.append(Check("accounts", "ok", detail))

    # Keychain: duplicate Claude Code login entries (the pre-fork bug — the login
    # keychain accumulates several "Claude Code-credentials" items and the wrong
    # one gets read) and orphaned cswap backups (more "claude-swap" items than
    # managed accounts). macOS only.
    if is_macos and claude_login_keychain_count is not None:
        problems: list[str] = []
        if claude_login_keychain_count > 1:
            problems.append(f"{claude_login_keychain_count} Claude Code login entries (duplicates)")
        if (cswap_backup_keychain_count is not None
                and cswap_backup_keychain_count > account_count):
            problems.append(f"{cswap_backup_keychain_count} cswap backups for "
                            f"{account_count} account(s) (orphaned)")
        if problems:
            checks.append(Check("keychain", "warn", " · ".join(problems),
                                "in Keychain Access, delete the stale items "
                                "(search 'Claude Code-credentials' / 'claude-swap')"))
        elif claude_login_keychain_count == 0:
            checks.append(Check("keychain", "off", "no Claude Code login entry",
                                "logged out, or credentials are file-only"))
        else:
            backups = cswap_backup_keychain_count if cswap_backup_keychain_count is not None else 0
            checks.append(Check("keychain", "ok", f"1 login · {backups} cswap backup(s)"))

    # Usage store freshness
    if store_age_s is None:
        checks.append(Check("usage store", "warn", "never fetched",
                            "`cswap list` or run the menu-bar / auto service"))
    elif store_age_s > STORE_STALE_S:
        checks.append(Check("usage store", "warn", f"stale ({_fmt_age(store_age_s)})",
                            "keep `cswap menubar`/`auto` running to refresh it"))
    else:
        checks.append(Check("usage store", "ok", f"fresh ({_fmt_age(store_age_s)})"))

    # Statusline
    checks.append(Check("statusline", "ok", "installed") if statusline_installed
                  else Check("statusline", "off", "not installed",
                             "`cswap statusline --install`"))

    # Response badge hook
    checks.append(Check("usage badge", "ok", "installed") if badge_installed
                  else Check("usage badge", "off", "not installed",
                             "`cswap usage --install-hook`"))

    # Menu-bar background service (macOS only)
    if is_macos:
        if menubar_service_loaded:
            checks.append(Check("menu-bar service", "ok", "running"))
        elif menubar_service_installed:
            checks.append(Check("menu-bar service", "warn", "installed but not loaded",
                                "`launchctl kickstart -k gui/$(id -u)/com.cswap.menubar`"))
        else:
            checks.append(Check("menu-bar service", "off", "not installed",
                                "`cswap menubar --install-service` (keeps usage fresh)"))

    # Harvester
    if harvest_enabled and harvest_tasks:
        checks.append(Check("harvester", "ok", f"armed · {harvest_tasks} task(s)"))
    elif harvest_enabled:
        checks.append(Check("harvester", "warn", "armed but no tasks",
                            "`cswap harvest add-task ...`"))
    else:
        checks.append(Check("harvester", "off", "disarmed",
                            "`cswap harvest enable` (opt-in)"))

    # MCP server dependency
    checks.append(Check("mcp server", "ok", "`mcp` available") if mcp_available
                  else Check("mcp server", "off", "`mcp` not installed",
                             "`pip install 'claude-swap-cs[mcp]'` for Claude Desktop"))

    return checks


def summarize(checks: list[Check]) -> tuple[int, int]:
    """Return ``(warn_count, error_count)`` for an exit-code / headline."""
    warn = sum(1 for c in checks if c.status == "warn")
    err = sum(1 for c in checks if c.status == "error")
    return warn, err
