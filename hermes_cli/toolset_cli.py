"""`hermes toolset` subcommand - manage toolset presets from the shell.

Subcommands:
    hermes toolset list - show configured presets with active marker
    hermes toolset use <name> - make a preset sticky in config.yaml
    hermes toolset show <name> - print a preset's resolved fields
    hermes toolset clear - deactivate the sticky preset

Presets themselves live under ``toolset_presets:`` in
``~/.hermes/config.yaml``. This CLI surface only manipulates the
``active_preset`` key - preset definitions are edited in YAML.
"""

from __future__ import annotations

import sys

from hermes_cli.config import load_config, save_config
from hermes_cli.tools_config import (
    get_active_preset,
    list_presets,
    preset_bundle_instruction,
    resolve_preset,
)


def toolset_command(args) -> None:
    action = getattr(args, "toolset_action", None)

    if action is None or action == "list":
        _cmd_list()
        return

    if action == "use":
        _cmd_use(args.name)
        return

    if action == "show":
        _cmd_show(args.name)
        return

    if action == "clear":
        _cmd_clear()
        return

    print(f"Unknown subcommand: {action}", file=sys.stderr)
    sys.exit(2)


def _cmd_list() -> None:
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
        print("~/.hermes/config.yaml - see the user docs for examples.")
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


def _cmd_use(name: str) -> None:
    cfg = load_config()
    presets = list_presets(cfg)
    if name not in presets:
        available = ", ".join(presets) if presets else "(none defined)"
        print(f"Unknown preset '{name}'. Available: {available}", file=sys.stderr)
        sys.exit(1)
    cfg["active_preset"] = name
    save_config(cfg)
    print(f"Preset '{name}' is now active.")
    print("New CLI / gateway sessions will use this preset's toolsets.")


def _cmd_show(name: str) -> None:
    cfg = load_config()
    presets = list_presets(cfg)
    if name not in presets:
        available = ", ".join(presets) if presets else "(none defined)"
        print(f"Unknown preset '{name}'. Available: {available}", file=sys.stderr)
        sys.exit(1)
    preset = presets[name]
    desc = preset.get("description", "")
    tools = preset.get("toolsets") or []
    disabled = preset.get("disabled_toolsets") or []
    bundle = str(preset.get("bundle") or "").strip()
    extras = preset.get("preload_skills") or []
    # Effective skills = bundle.skills + extras (deduped, first-seen wins).
    # Resolve through the same helper the runtime uses so the displayed
    # list matches exactly what session activation will load.
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
        # Preview the bundle's instruction so users can see what extra
        # guidance the session will receive when this preset activates.
        first_line = instruction.splitlines()[0]
        suffix = "" if first_line == instruction else " [...]"
        print(f"Bundle instruction: {first_line}{suffix}")


def _cmd_clear() -> None:
    cfg = load_config()
    if not get_active_preset(cfg):
        print("No preset active.")
        return
    cfg["active_preset"] = ""
    save_config(cfg)
    print("Preset cleared - default toolset will be used on next session.")
