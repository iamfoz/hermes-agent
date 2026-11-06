"""``hermes toolset`` subcommand parser."""

from __future__ import annotations


def build_toolset_parser(subparsers) -> None:
    """Attach the ``toolset`` subcommand (toolset presets) to ``subparsers``."""
    from hermes_cli.toolset_cli import toolset_command

    toolset_parser = subparsers.add_parser(
        "toolset",
        help="Manage toolset presets (focused work modes)",
        description=(
            "Toolset presets bundle (enabled toolsets, disabled toolsets,\n"
            "preload skills) under a name you can switch between with\n"
            "/toolset <name> from the CLI, Telegram, Discord, etc.\n\n"
            "Presets are defined under `toolset_presets:` in config.yaml.\n"
            "Use `hermes toolset use <name>` to make a preset sticky across\n"
            "invocations."
        ),
    )
    toolset_sub = toolset_parser.add_subparsers(dest="toolset_action")
    toolset_sub.add_parser("list", help="List configured presets with descriptions")
    use_p = toolset_sub.add_parser("use", help="Activate a preset (sticky, persists in config.yaml)")
    use_p.add_argument("name", help="Preset name from toolset_presets")
    show_p = toolset_sub.add_parser("show", help="Show a preset's resolved fields")
    show_p.add_argument("name", help="Preset name to inspect")
    toolset_sub.add_parser("clear", help="Deactivate the sticky preset (revert to default toolset)")
    toolset_parser.set_defaults(func=toolset_command)
