"""`cswap usage --hook`: surface the active account's remaining Claude quota in
every Claude Code response, across all repos.

A ``UserPromptSubmit`` hook runs ``cswap usage --hook`` before each turn; its one
line (store-only, never fetches, always exits 0) is injected into the model's
context. A managed block in the user's global ``~/.claude/CLAUDE.md`` tells Claude
to echo it as a compact badge at the start and end of the reply. ``--install-hook``
wires both; ``--uninstall-hook`` removes them.

Everything here is pure except the settings / CLAUDE.md file I/O, which takes
explicit paths so it unit-tests against ``tmp_path``.
"""

from __future__ import annotations

import json
from pathlib import Path

HOOK_COMMAND = "cswap usage --hook"
HOOK_PREFIX = "cswap-usage »"

_BEGIN = "<!-- BEGIN cswap usage-badge -->"
_END = "<!-- END cswap usage-badge -->"

CLAUDE_MD_BLOCK = f"""{_BEGIN}
## Claude usage badge (managed by `cswap usage --install-hook`)
Before each turn a hook injects one line beginning `{HOOK_PREFIX}` with the active
account's remaining Claude quota. At the **start** and the **end** of every
response, show a compact one-line badge drawn from it, e.g. `personal · 63% 5h
left`. Use the injected value verbatim — never run a command to fetch it. If no
`{HOOK_PREFIX}` line is present that turn, skip the badge silently.
{_END}"""


# --- hook line (pure) ----------------------------------------------------------

def hook_line(profile: str | None, five_left: float | None, seven_left: float | None) -> str:
    """One-line usage badge for the hook. Unknown values render as ``?`` so the
    model never echoes a made-up number."""
    def pct(value: float | None) -> str:
        return f"{int(value)}%" if isinstance(value, (int, float)) else "?"

    who = profile or "(no active account)"
    return f"{HOOK_PREFIX} {who} · 5h {pct(five_left)} left · 7d {pct(seven_left)} left"


# --- settings.json UserPromptSubmit hook (pure) --------------------------------

def add_prompt_hook(settings: dict, command: str = HOOK_COMMAND) -> dict:
    """Return ``settings`` with a ``UserPromptSubmit`` command hook present.

    Idempotent, and preserves every other setting and hook.
    """
    settings = dict(settings) if isinstance(settings, dict) else {}
    hooks = dict(settings.get("hooks") or {})
    groups = list(hooks.get("UserPromptSubmit") or [])
    for group in groups:
        if not isinstance(group, dict):
            continue
        for entry in group.get("hooks") or []:
            if isinstance(entry, dict) and entry.get("command") == command:
                return settings  # already installed
    groups.append({"hooks": [{"type": "command", "command": command}]})
    hooks["UserPromptSubmit"] = groups
    settings["hooks"] = hooks
    return settings


def remove_prompt_hook(settings: dict, command: str = HOOK_COMMAND) -> dict:
    """Return ``settings`` with our ``UserPromptSubmit`` command hook removed,
    keeping any other hooks and dropping now-empty containers."""
    if not isinstance(settings, dict):
        return {}
    settings = dict(settings)
    hooks = dict(settings.get("hooks") or {})
    groups = hooks.get("UserPromptSubmit")
    if not isinstance(groups, list):
        return settings

    kept: list = []
    for group in groups:
        if not isinstance(group, dict):
            kept.append(group)
            continue
        inner = [e for e in (group.get("hooks") or [])
                 if not (isinstance(e, dict) and e.get("command") == command)]
        if inner:
            group = dict(group)
            group["hooks"] = inner
            kept.append(group)
        # a group left with no hooks is dropped entirely

    if kept:
        hooks["UserPromptSubmit"] = kept
    else:
        hooks.pop("UserPromptSubmit", None)
    if hooks:
        settings["hooks"] = hooks
    else:
        settings.pop("hooks", None)
    return settings


# --- CLAUDE.md managed block (pure) --------------------------------------------

def upsert_block(text: str | None, block: str = CLAUDE_MD_BLOCK) -> str:
    """Insert or replace the managed badge block, leaving all other text intact."""
    text = text or ""
    if _BEGIN in text and _END in text:
        pre = text[: text.index(_BEGIN)]
        post = text[text.index(_END) + len(_END):]
        return pre + block + post
    if not text:
        return block + "\n"
    sep = "\n" if text.endswith("\n") else "\n\n"
    if text.endswith("\n\n"):
        sep = ""
    return text + sep + block + "\n"


def remove_block(text: str | None, block: str = CLAUDE_MD_BLOCK) -> str:
    """Remove the managed badge block; return other text unchanged."""
    text = text or ""
    if _BEGIN not in text or _END not in text:
        return text
    pre = text[: text.index(_BEGIN)].rstrip("\n")
    post = text[text.index(_END) + len(_END):].lstrip("\n")
    if pre and post:
        return pre + "\n\n" + post
    tail = pre or post
    return (tail + "\n") if tail else ""


# --- install / uninstall glue --------------------------------------------------

def default_claude_md_path() -> Path:
    """The user's global Claude Code memory file (``~/.claude/CLAUDE.md``)."""
    return Path.home() / ".claude" / "CLAUDE.md"


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def install(settings_path: Path, claude_md_path: Path, command: str = HOOK_COMMAND) -> None:
    """Wire the ``UserPromptSubmit`` hook into ``settings_path`` and the managed
    instruction block into ``claude_md_path`` (both idempotent)."""
    _write_json(settings_path, add_prompt_hook(_read_json(settings_path), command))
    try:
        existing = claude_md_path.read_text(encoding="utf-8")
    except OSError:
        existing = ""
    claude_md_path.parent.mkdir(parents=True, exist_ok=True)
    claude_md_path.write_text(upsert_block(existing), encoding="utf-8")


def uninstall(settings_path: Path, claude_md_path: Path, command: str = HOOK_COMMAND) -> bool:
    """Remove the hook and the managed block. Returns whether anything was present."""
    settings = _read_json(settings_path)
    cleaned = remove_prompt_hook(settings, command)
    had_hook = cleaned != settings
    if had_hook:
        _write_json(settings_path, cleaned)
    had_block = False
    try:
        existing = claude_md_path.read_text(encoding="utf-8")
        stripped = remove_block(existing)
        if stripped != existing:
            had_block = True
            claude_md_path.write_text(stripped, encoding="utf-8")
    except OSError:
        pass
    return had_hook or had_block
