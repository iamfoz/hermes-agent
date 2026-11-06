"""The ``/toolset`` (alias ``/preset``) slash command: switch toolset presets from a chat.

Presets live under ``toolset_presets:`` in the profile's config.yaml; this handler only flips the
sticky top-level ``active_preset`` key (``hermes_cli.tools_config`` resolves it on every agent build).
Switching starts a fresh session by default so the new tool surface never lands mid-conversation
(prompt cache); ``--no-new`` defers it to the user's next ``/new``.
"""

from __future__ import annotations

from gateway.platforms.event import MessageEvent


def _config_hint() -> str:
    from hermes_constants import display_hermes_home
    return f"`{display_hermes_home()}/config.yaml`"


def _unknown_preset(name: str, presets: dict) -> str:
    available = ", ".join(presets) if presets else "(none defined)"
    return f"Unknown preset `{name}`. Available: {available}"


def _format_status(cfg: dict, presets: dict) -> str:
    from hermes_cli.tools_config import get_active_preset
    active = get_active_preset(cfg)
    lines = [f"**Active preset:** {active}" if active else "**Active preset:** (none - default toolset)"]
    if not presets:
        lines += ["", f"No presets defined. Add entries under `toolset_presets:` in {_config_hint()}."]
        return "\n".join(lines)
    lines.append("")
    for name, preset in presets.items():
        if not isinstance(preset, dict):
            continue
        marker = "●" if name == active else "○"
        desc = preset.get("description", "")
        tools_str = ", ".join(preset.get("toolsets") or []) or "default"
        lines.append(f"{marker} **{name}** - {desc} ({tools_str})" if desc else f"{marker} **{name}** ({tools_str})")
    lines += ["", "`/toolset <name>` to switch (auto-starts new session)",
              "`/toolset <name> --no-new` to switch without new session", "`/toolset clear` to deactivate"]
    return "\n".join(lines)


def _format_show(name: str, preset: dict, cfg: dict) -> str:
    from hermes_cli.tools_config import resolve_preset
    tools = preset.get("toolsets") or []
    disabled = preset.get("disabled_toolsets") or []
    extras = preset.get("preload_skills") or []
    # The central resolver, so what we display matches what session activation will load.
    _, _, effective_skills = resolve_preset(name, cfg)
    lines = [f"**Preset:** {name}"]
    if preset.get("description"):
        lines.append(f"**Description:** {preset['description']}")
    lines.append(f"**Toolsets:** {', '.join(tools) if tools else 'default'}")
    lines.append(f"**Disabled:** {', '.join(disabled) if disabled else '(none)'}")
    lines.append(f"**Bundle:** {str(preset.get('bundle') or '').strip() or '(none)'}")
    if extras:
        lines.append(f"**Extra skills:** {', '.join(extras)}")
    lines.append(f"**Effective skills:** {', '.join(effective_skills) if effective_skills else '(none)'}")
    return "\n".join(lines)


def _activation_detail(name: str, preset: dict, cfg: dict) -> str:
    from hermes_cli.tools_config import resolve_preset
    # Effective skill set (bundle.skills + extras), exactly what the next session will load.
    _, _, skills = resolve_preset(name, cfg)
    rows = (("Toolsets", preset.get("toolsets") or []), ("Disabled", preset.get("disabled_toolsets") or []),
            ("Skills", skills))
    return "\n".join(f"{label}: {', '.join(values)}" for label, values in rows if values)


class GatewayToolsetCommandsMixin:
    """``/toolset [list|clear|show <name>|<name> [--no-new]]`` for GatewayRunner."""

    async def _handle_toolset_command(self, event: MessageEvent) -> str:
        from gateway.run import _load_gateway_config
        from hermes_cli.tools_config import list_presets
        args = (event.get_command_args() or "").split()
        cfg = _load_gateway_config()
        presets = list_presets(cfg)
        sub = args[0] if args else "list"
        if sub == "list":
            return _format_status(cfg, presets)
        if sub == "show":
            if len(args) < 2:
                return "Usage: `/toolset show <name>`"
            return _format_show(args[1], presets[args[1]], cfg) if args[1] in presets else _unknown_preset(args[1], presets)
        if sub == "clear":
            return await self._toolset_clear(event, cfg)
        return await self._toolset_activate(event, cfg, presets, sub, no_new="--no-new" in args[1:])

    async def _toolset_clear(self, event: MessageEvent, cfg: dict) -> str:
        from hermes_cli.tools_config import get_active_preset
        if not get_active_preset(cfg):
            return "No preset active."
        if not self._save_gateway_config_key("active_preset", ""):
            return "Failed to update config.yaml."
        # Evict the cached agent and rotate the session (as /new) so the next message rebuilds
        # with the default toolset in a clean cache state.
        await self._handle_reset_command(event)
        return "Preset cleared - default toolset active. Fresh session started."

    async def _toolset_activate(self, event: MessageEvent, cfg: dict, presets: dict, name: str, *, no_new: bool) -> str:
        if name not in presets:
            return f"{_unknown_preset(name, presets)}\nDefine presets under `toolset_presets:` in {_config_hint()}."
        if not self._save_gateway_config_key("active_preset", name):
            return "Failed to update config.yaml."
        detail = _activation_detail(name, presets[name], cfg)
        if no_new:
            head = f"Preset **{name}** saved. Run /new to activate it."
            return f"{head}\n{detail}" if detail else head
        # Reset so the next message picks up the preset's toolsets in a clean cache state; the
        # reset's own reply is dropped in favour of ours.
        await self._handle_reset_command(event)
        head = f"Preset **{name}** activated - fresh session started."
        body = f"{head}\n{detail}" if detail else head
        return f"{body}\n\nType /toolset clear to return to the default toolset."
