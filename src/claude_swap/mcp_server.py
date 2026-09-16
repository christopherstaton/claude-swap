"""An MCP server exposing cswap's usage view to Claude Desktop (`cswap mcp`).

Claude Desktop has no always-on statusline for third parties, so the way to
surface cswap's usage there is a Model Context Protocol server: Desktop launches
``cswap mcp`` over stdio and can then call its tools on demand.

The tool *handlers* (`usage_report`, `accounts_report`) are plain functions over a
switcher — pure of the ``mcp`` SDK — so they unit-test with a fake switcher and
without the optional dependency installed. The ``mcp`` SDK is imported lazily,
only inside ``build_server`` / ``run_server``, so importing this module (and the
rest of cswap) never requires it.

All tools are **read-only** — no account switching from Desktop.
"""

from __future__ import annotations


def usage_report(switcher, account: str | None = None) -> dict:
    """Current usage for an account (the active one by default), same shape as
    ``cswap usage --json``. ``account`` may be a number, email, or alias."""
    from claude_swap.json_output import usage_fields

    snap = switcher.accounts_snapshot()  # paced read (may fetch if due)
    if account:
        target = next(
            (a for a in snap.accounts
             if account in (str(a.number), a.email) or a.alias == account),
            None,
        )
    else:
        target = next((a for a in snap.accounts if getattr(a, "is_active", False)), None)

    if target is None:
        return {"error": f"account not found: {account}" if account else "no active account"}

    entry = target.usage
    collected = entry.sentinel if entry.sentinel else entry.last_good
    status, usage = usage_fields(collected, entry.fetched_at)
    return {
        "account": {"number": int(target.number), "email": target.email,
                    "alias": target.alias or None},
        "usageStatus": status,
        "usage": usage,
        "ageSeconds": round(entry.age_s, 1) if entry.age_s is not None else None,
    }


def accounts_report(switcher) -> list[dict]:
    """Every managed account: which is active, and its 5h **remaining** quota %
    (draining 100→0, matching the other views). Store-only, no network."""
    from claude_swap import statusline as sl

    snap = switcher.accounts_snapshot(fetch=set())  # store-only
    out: list[dict] = []
    for a in snap.accounts:
        lg = a.usage.last_good if isinstance(a.usage.last_good, dict) else {}
        five = lg.get("five_hour") if isinstance(lg, dict) else None
        remaining = None
        if isinstance(five, dict) and isinstance(five.get("pct"), (int, float)):
            remaining = sl.remaining_from_used(float(five["pct"]))
        out.append({
            "number": int(a.number),
            "email": a.email,
            "alias": a.alias or None,
            "active": bool(getattr(a, "is_active", False)),
            "fiveHourRemainingPct": remaining,
        })
    return out


def build_server():
    """Construct the MCPServer with cswap's read-only tools. Imports the ``mcp``
    SDK lazily, so callers without it fail here with a clear ModuleNotFoundError."""
    from mcp.server.mcpserver import MCPServer

    from claude_swap.switcher import ClaudeAccountSwitcher

    server = MCPServer("cswap")

    @server.tool()
    def get_usage(account: str | None = None) -> dict:
        """Current Claude usage for a cswap-managed account (the active one by
        default). Pass an account number, email, or alias. Read-only."""
        return usage_report(ClaudeAccountSwitcher(), account)

    @server.tool()
    def list_accounts() -> list:
        """All cswap-managed accounts, which one is active, and each account's
        remaining 5-hour quota %. Read-only."""
        return accounts_report(ClaudeAccountSwitcher())

    return server


def run_server() -> None:
    """Run the MCP server over stdio (how Claude Desktop launches it)."""
    build_server().run(transport="stdio")
