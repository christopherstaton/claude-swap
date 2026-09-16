"""Coverage for `cswap doctor`'s pure report core (`doctor.build_report`)."""

from __future__ import annotations

from claude_swap import doctor as dr


def _report(**over):
    base = dict(
        distribution="claude-swap-cs", version="0.27.0b1",
        account_count=1, active_profile="personal", error_accounts=0,
        store_age_s=30.0, statusline_installed=True, badge_installed=True,
        harvest_enabled=False, harvest_tasks=0, mcp_available=True, is_macos=True,
        menubar_service_installed=True, menubar_service_loaded=True,
        claude_login_keychain_count=1, cswap_backup_keychain_count=1,
    )
    base.update(over)
    return dr.build_report(**base)


def _by_name(checks, name):
    return next(c for c in checks if c.name == name)


def test_all_ok_has_no_warnings():
    checks = _report()
    assert dr.summarize(checks) == (0, 0)   # 'off' (harvester disarmed) is not a warning
    assert _by_name(checks, "package").detail == "claude-swap-cs 0.27.0b1"


def test_no_accounts_warns():
    assert _by_name(_report(account_count=0), "accounts").status == "warn"


def test_error_accounts_warn():
    c = _by_name(_report(account_count=2, error_accounts=1), "accounts")
    assert c.status == "warn" and "1 with errors" in c.detail


def test_stale_and_never_fetched_store_warn():
    assert _by_name(_report(store_age_s=2000.0), "usage store").status == "warn"
    assert _by_name(_report(store_age_s=None), "usage store").status == "warn"
    assert _by_name(_report(store_age_s=30.0), "usage store").status == "ok"


def test_statusline_and_badge_off():
    checks = _report(statusline_installed=False, badge_installed=False)
    assert _by_name(checks, "statusline").status == "off"
    assert _by_name(checks, "usage badge").status == "off"


def test_menubar_service_states():
    assert _by_name(_report(menubar_service_loaded=True), "menu-bar service").status == "ok"
    assert _by_name(_report(menubar_service_loaded=False, menubar_service_installed=True),
                    "menu-bar service").status == "warn"
    assert _by_name(_report(menubar_service_loaded=False, menubar_service_installed=False),
                    "menu-bar service").status == "off"


def test_menubar_service_absent_on_non_macos():
    assert all(c.name != "menu-bar service" for c in _report(is_macos=False))


def test_harvester_states():
    assert _by_name(_report(harvest_enabled=True, harvest_tasks=2), "harvester").status == "ok"
    assert _by_name(_report(harvest_enabled=True, harvest_tasks=0), "harvester").status == "warn"
    assert _by_name(_report(harvest_enabled=False), "harvester").status == "off"


def test_mcp_off_when_unavailable():
    assert _by_name(_report(mcp_available=False), "mcp server").status == "off"


# --- keychain: the pre-fork "multiple entries" issue ---------------------------

def test_keychain_duplicate_login_warns():
    c = _by_name(_report(claude_login_keychain_count=3), "keychain")
    assert c.status == "warn" and "3 Claude Code login entries" in c.detail


def test_keychain_orphaned_backups_warn():
    c = _by_name(_report(account_count=1, cswap_backup_keychain_count=2), "keychain")
    assert c.status == "warn" and "2 cswap backups for 1 account" in c.detail


def test_keychain_clean_ok():
    c = _by_name(_report(claude_login_keychain_count=1, cswap_backup_keychain_count=1,
                         account_count=1), "keychain")
    assert c.status == "ok" and "1 login" in c.detail


def test_keychain_zero_login_is_off():
    assert _by_name(_report(claude_login_keychain_count=0, cswap_backup_keychain_count=0),
                    "keychain").status == "off"


def test_keychain_skipped_when_unavailable_or_non_macos():
    assert all(c.name != "keychain" for c in _report(claude_login_keychain_count=None))
    assert all(c.name != "keychain" for c in _report(is_macos=False))


def test_summarize_counts_warnings():
    # accounts + store warn; keep keychain clean (0 backups so no orphan warn)
    checks = _report(account_count=0, store_age_s=None, cswap_backup_keychain_count=0)
    assert dr.summarize(checks) == (2, 0)


def test_glyphs():
    assert dr.Check("x", "ok", "").glyph == "✓"
    assert dr.Check("x", "warn", "").glyph == "⚠"
    assert dr.Check("x", "off", "").glyph == "○"
