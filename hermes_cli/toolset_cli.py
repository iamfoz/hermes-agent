"""`hermes toolset` subcommand - manage toolset presets from the shell.

Subcommands:
    hermes toolset list - show configured presets with active marker
    hermes toolset use <name> - make a preset sticky in config.yaml
    hermes toolset show <name> - print a preset's resolved fields
    hermes toolset clear - deactivate the sticky preset

Presets themselves live under ``toolset_presets:`` in the profile's
config.yaml. This CLI surface only manipulates the top-level
``active_preset`` key - preset definitions are edited in YAML.
"""

from __future__ import annotations

import sys

from hermes_cli.config import get_config_path, load_config
from hermes_cli.config_backend import write_config_key
from hermes_cli.tools_config import (
    get_active_preset,
    list_presets,
    preset_bundle_instruction,
    resolve_preset,
)


def toolset_command(args) -> None:
    action = getattr(args, "toolset_action", None) or "list"
    handler = _ACTIONS.get(action)
    if handler is None:
        print(f"Unknown subcommand: {action}", file=sys.stderr)
        sys.exit(2)
    handler(args)


def _cmd_list(_args) -> None:
    cfg = load_config()
    presets = list_presets(cfg)
    active = get_active_preset(cfg)

    if active:
        print(f"Active preset: {active}")
    else:
        print("Active preset: (none - default toolset)")

    if not presets:
        print()
        print("No presets defined. Add entries under `toolset_presets:` in")
        print("config.yaml - see the user docs for examples.")
        return

    print()
    for name, preset in presets.items():
        if not isinstance(preset, dict):
            continue
        marker = "*" if name == active else " "
        desc = preset.get("description", "")
        tools = preset.get("toolsets") or []
        tools_str = ", ".join(tools) if tools else "default toolset"
        head = f" {marker} {name}"
        if desc:
            print(f"{head} - {desc}")
            print(f"     toolsets: {tools_str}")
        else:
            print(f"{head} ({tools_str})")


def _require_preset(cfg: dict, name: str) -> dict:
    presets = list_presets(cfg)
    if name not in presets:
        available = ", ".join(presets) if presets else "(none defined)"
        print(f"Unknown preset '{name}'. Available: {available}", file=sys.stderr)
        sys.exit(1)
    return presets[name]


def _cmd_use(args) -> None:
    _require_preset(load_config(), args.name)
    write_config_key(get_config_path(), "active_preset", args.name)
    print(f"Preset '{args.name}' is now active.")
    print("New CLI / gateway sessions will use this preset's toolsets.")


def _cmd_show(args) -> None:
    cfg = load_config()
    name = args.name
    preset = _require_preset(cfg, name)
    desc = preset.get("description", "")
    tools = preset.get("toolsets") or []
    disabled = preset.get("disabled_toolsets") or []
    bundle = str(preset.get("bundle") or "").strip()
    extras = preset.get("preload_skills") or []
    # Same resolver the runtime uses, so the list matches what session activation loads.
    _, _, effective_skills = resolve_preset(name, cfg)
    instruction = preset_bundle_instruction(name, cfg)
    print(f"Preset: {name}")
    if desc:
        print(f"Description: {desc}")
    print(f"Toolsets: {', '.join(tools) if tools else 'default'}")
    print(f"Disabled: {', '.join(disabled) if disabled else '(none)'}")
    print(f"Bundle: {bundle or '(none)'}")
    if extras:
        print(f"Extra skills: {', '.join(extras)}")
    print(f"Effective skills: {', '.join(effective_skills) if effective_skills else '(none)'}")
    if instruction:
        first_line = instruction.splitlines()[0]
        suffix = "" if first_line == instruction else " [...]"
        print(f"Bundle instruction: {first_line}{suffix}")


def _cmd_clear(_args) -> None:
    if not get_active_preset(load_config()):
        print("No preset active.")
        return
    write_config_key(get_config_path(), "active_preset", "")
    print("Preset cleared - default toolset will be used on next session.")


def apply_toolset_flag(name, cfg: dict, toolsets, skills):
    """``--toolset NAME`` for one invocation, folded into the launch's ``(toolsets, skills)``.

    The gateway owns the session's tool surface and resolves only the sticky ``active_preset``, so a
    per-run preset rides the existing launch flags: its whitelist becomes the ``-t`` pick (a typed
    ``-t`` still wins), its ``disabled_toolsets`` are dropped from that pick, and its skills join
    ``-s``. An unknown name warns and leaves the launch unchanged.
    """
    name = (name or "").strip()
    if not name:
        return toolsets, skills
    if name not in list_presets(cfg):
        print(f"Warning: preset '{name}' not found - using default toolset.", file=sys.stderr)
        return toolsets, skills
    enabled, disabled, preload = resolve_preset(name, cfg)
    typed = [toolsets] if isinstance(toolsets, str) else list(toolsets or [])
    picked = [t.strip() for item in (typed or enabled or []) for t in str(item).split(",") if t.strip()]
    if picked:
        toolsets = [t for t in picked if t not in disabled]
    if preload:
        existing = [skills] if isinstance(skills, str) else list(skills or [])
        skills = [*existing, *preload]
    return toolsets, skills


_ACTIONS = {"list": _cmd_list, "use": _cmd_use, "show": _cmd_show, "clear": _cmd_clear}
