"""Multi-account switcher for Claude Code (christopherstaton fork)."""

from importlib.metadata import PackageNotFoundError, version

# This fork's distribution is ``claude-swap-cs``; fall back to the upstream name
# for a pre-rename install, and to a placeholder so a bare source tree still
# imports. A version lookup must never crash the import.
try:
    __version__ = version("claude-swap-cs")
except PackageNotFoundError:
    try:
        __version__ = version("claude-swap")
    except PackageNotFoundError:
        __version__ = "0.0.0"

from claude_swap.switcher import ClaudeAccountSwitcher

__all__ = ["ClaudeAccountSwitcher", "__version__"]
