---
sidebar_position: 50
title: "Toolset Presets"
description: "Switch focused tool configurations per task - email, research, writing, coding - from any platform with one command"
---

# Toolset Presets

Toolset presets let you define named **work modes** you can switch between with a single command (`/toolset email`, `/toolset research`). Each preset narrows the agent's **tool surface** to a task type, optionally references a **skill bundle** for the skill set, and (by default) opens a fresh session so the LLM provider's prompt cache stays warm.

Presets layer on top of two pieces of upstream config:

* the per-platform `hermes tools` configuration (which they can whitelist and/or subtract from), and
* the [skill-bundles feature](./skills.md#skill-bundles) (PR #28373) - presets don't own skill grouping; they reference an existing bundle by name and inherit its skill list.

They do **not** isolate memory, config, or process state the way profiles do - they're a lightweight focus mechanism, not a separate environment.

## What this branch adds (PR summary)

This branch ships **`toolset_presets:`** as a layer on top of upstream's skill-bundles feature, adding three things bundles don't cover:

1. **Tool-surface gating** - each preset can set `toolsets:` (whitelist) and `disabled_toolsets:` (always subtracted). Bundles only touch skills.
2. **Sticky / active mode** - `active_preset:` in `config.yaml` plus a `--toolset` flag with a clear precedence order (`--toolset` > `active_preset` > platform config). Activating a preset persists across sessions and gateway restarts. Bundles are invocation-time skill loads, not a mode.
3. **Auto-`/new` session on switch** - because changing tool schemas mid-conversation invalidates the provider's prompt cache (~2× per-turn token cost), `/toolset` opens a fresh session by default. `--no-new` defers.

Skill grouping itself is **delegated** to bundles: each preset references one bundle via `bundle: <name>`, and `resolve_preset()` expands the bundle's `skills` list. Preset-level `preload_skills:` is preserved as an optional **extras** list, merged on top of the bundle (deduped, first-seen order preserved). See [Relationship to skill bundles](#relationship-to-skill-bundles) below for the full design rationale.

## Why presets?

- **Focus.** When you're triaging email, you don't need terminal or browser tools. When you're researching, you don't need messaging. Restricting the tool surface keeps the agent on-task.
- **Cache-friendly.** A preset switch starts a fresh session, so the system prompt + tool schemas cache cleanly for the rest of the conversation.
- **Cross-platform.** The same `/toolset` command works from the CLI, Telegram, Discord, WhatsApp, or any other gateway adapter - your phone gets the same preset switcher as your terminal.
- **Profile-scoped.** Presets live in `~/.hermes/config.yaml`, which is profile-aware, so each profile can have its own preset set.

## Defining presets

Add a `toolset_presets:` block to `~/.hermes/config.yaml`. Each preset names a skill bundle (defined via `hermes bundles create` - see the [skills guide](./skills.md#skill-bundles)) for its skill set, and gates the tool surface on top:

```yaml
toolset_presets:
  email:
    description: "Inbox triage - email, file ops, memory"
    toolsets: ["messaging", "file", "memory", "todo", "session_search"]
    bundle: "email-stack"           # ← skills live in the bundle

  research:
    description: "Deep research - web, browser, skills"
    toolsets: ["web", "browser", "file", "skills", "memory", "session_search", "terminal"]
    bundle: "research-stack"
    preload_skills: ["arxiv"]       # ← optional extras on top of the bundle

  writing:
    description: "Focused writing - minimal distractions"
    toolsets: ["file", "memory", "skills"]
    disabled_toolsets: ["terminal", "browser", "web", "delegation", "cronjob"]
    # No bundle - this preset only gates tools, no auto-loaded skills.

  development:
    description: "Full coding - everything except messaging"
    toolsets: ["terminal", "file", "code_execution", "browser", "skills", "memory", "delegation", "session_search"]
    disabled_toolsets: ["messaging"]

# Currently active preset - set by /toolset, cleared by /toolset clear
active_preset: ""
```

### Field reference

| Field | Type | Effect |
|-------|------|--------|
| `description` | string | Shown in `/toolset list` and `hermes toolset list` |
| `toolsets` | list[str] | Whitelist. Empty or omitted → no restriction (full default toolset) |
| `disabled_toolsets` | list[str] | Always subtracted, regardless of `toolsets` state |
| `bundle` | string | Name of a skill bundle (see [bundles](./skills.md#skill-bundles)) - its `skills:` and `instruction:` are inherited by this preset |
| `preload_skills` | list[str] | **Extras** loaded on top of the bundle (deduped, first-seen wins). Use for one-off additions you don't want to put in the bundle itself. Works without `bundle:` too. |

Toolset names match those shown in `hermes tools list` (e.g. `web`, `terminal`, `file`, `messaging`, `memory`, `skills`, `browser`, `delegation`).

The **effective skill list** for a preset is `bundle.skills + preload_skills`, deduplicated (first occurrence wins, so bundle skills appear before extras). `hermes toolset show <name>` displays the bundle, extras, and effective list separately so you can see the composition at a glance.

## Switching presets

### From any platform (CLI, Telegram, Discord, ...)

```
/toolset email             # activate + auto-start fresh session
/toolset email --no-new    # activate sticky, defer to manual /new
/toolset list              # show available presets, with active marker
/toolset show email        # show resolved fields for a preset
/toolset clear             # deactivate, revert to default toolset
```

`/toolset <name>` is sticky - it writes `active_preset:` to `config.yaml`, so new sessions (and the gateway across restarts) keep using the preset until you `/toolset clear` it.

### From the shell

```bash
hermes toolset list            # show configured presets with active marker
hermes toolset use email       # make 'email' the sticky active preset
hermes toolset show research   # print a preset's resolved fields
hermes toolset clear           # deactivate the sticky preset
```

### Per-invocation override

The `--toolset` CLI flag activates a preset for a single invocation without persisting it:

```bash
hermes --toolset research                  # interactive session with research preset
hermes --toolset writing -q "draft post"   # one-shot with writing preset
hermes chat --toolset email                # via chat subcommand
```

The `--toolset` flag wins over `active_preset` in `config.yaml` for that run.

## How resolution works

When the agent starts a session, the toolset resolver walks this order:

1. **`--toolset <name>`** flag on the CLI invocation - wins for this run.
2. **`active_preset:`** in `config.yaml` - sticky preset, applies to every new session until cleared.
3. **`platform_toolsets.<platform>`** from `hermes tools` - your per-platform tool config.

If a preset's `toolsets:` is non-empty, the agent's `enabled_toolsets` is set to that whitelist (the platform tool config is not consulted). If `toolsets:` is empty or omitted, the preset only contributes `disabled_toolsets` plus its skill list - the platform config still drives the enabled set.

`disabled_toolsets` is always merged in, regardless of whether `toolsets:` is set.

## Relationship to skill bundles

Upstream's [**skill bundles**](./skills.md#skill-bundles) feature (PR #28373) and **toolset presets** answer **different questions** but compose cleanly. Presets do not duplicate bundles; they reference them.

| | **Skill bundles** | **Toolset presets** |
|---|---|---|
| **Question answered** | *What skills go together?* | *What work mode am I in?* |
| **Owns** | Skill grouping + optional shared instruction text | Tool surface, sticky mode, auto-`/new` UX |
| **Storage** | One YAML per bundle, `~/.hermes/skill-bundles/<slug>.yaml` | One block in `~/.hermes/config.yaml`, `toolset_presets:` |
| **Activation** | `/<bundle-name>` (invocation-time skill load) | `/toolset <name>` (sticky mode persisted to config) |
| **Touches `enabled_toolsets` / `disabled_toolsets`?** | No | Yes |
| **Persistent across sessions / restarts?** | No (per-invocation) | Yes (via `active_preset:`) |

A preset names its bundle:

```yaml
toolset_presets:
  email:
    toolsets: ["messaging", "file", "memory"]
    bundle: "email-stack"        # ← upstream bundle name
```

...and the bundle owns its skill list and (optional) instruction text:

```yaml
# ~/.hermes/skill-bundles/email-stack.yaml
name: email-stack
skills: ["himalaya", "inbox-summariser"]
instruction: |
  Triage email by importance. Summarise threads before drafting replies.
```

This composition means:

* **Editing skills is a one-place operation.** Updating `email-stack.yaml` updates every preset that references it. No copy-paste between presets that share a skill set.
* **Bundles stay invokable on their own.** `/email-stack` still works as a one-shot skill load when you don't want the full preset (sticky mode, tool gating, auto-`/new`).
* **Presets stay focused on the work-mode concerns** - tool surface, persistence, session lifecycle - without re-implementing skill composition.
* **Missing bundle ≠ broken preset.** A preset whose `bundle:` references a nonexistent bundle still activates: the tool surface gating and any `preload_skills` extras apply, and a warning is logged naming both the preset and the missing bundle so you can fix it (`hermes bundles create <name> ...`).

### When to use what

| Need | Use |
|---|---|
| "Load these skills right now, this turn" | `/<bundle>` (bundle) |
| "Stay in this work mode across sessions, with tool gating" | `/toolset <preset>` (preset referencing a bundle) |
| "Define a skill set I'll reuse across several presets" | Create one bundle, reference it from each preset's `bundle:` |
| "Add an extra skill to one preset that doesn't belong in the bundle" | Set `preload_skills:` extras on the preset |

## Auto-new session

`/toolset <name>` automatically starts a fresh session. This is intentional - the LLM provider caches the system prompt and tool schemas, and changing them mid-conversation invalidates the cache and roughly doubles per-turn token cost. By rotating to a clean session on switch, the cache stays warm for the rest of the conversation.

If you'd rather hold off, `/toolset <name> --no-new` saves the preset to config but defers activation until you manually run `/new`.

## Edge cases

- **Unknown preset.** The agent logs a warning and falls back to the platform tool config. The CLI prints the available preset names.
- **Preset references a toolset that doesn't exist.** `get_tool_definitions()` ignores it with a warning - same behaviour as a stale entry in `hermes tools`.
- **Preset references a missing bundle.** The preset still activates - its `toolsets:` / `disabled_toolsets:` gating applies, and any `preload_skills:` extras still load. A warning names both the preset and the missing bundle so you can run `hermes bundles create <name> ...` to fix it.
- **Preset references a missing skill** (either from the bundle or from extras). The `--skills` preload fails gracefully with "skill not found" - the session still starts.
- **Cron jobs.** Per-job `enabled_toolsets` already takes precedence in cron resolution; active presets do not override per-job toolsets. Cron always disables `cronjob`, `messaging`, and `clarify` regardless of presets.
- **Restarts.** The active preset survives gateway restarts (it lives in `config.yaml`).

## Non-goals

Presets are not a replacement for:

- **`hermes tools`** - your per-platform tool config is the baseline; presets layer on top.
- **Profiles** - profiles isolate config, memory, skills, and history. Presets only narrow the tool surface within a profile.
- **Skill bundles** - bundles own skill grouping (see [Relationship to skill bundles](#relationship-to-skill-bundles) above). Presets reference a bundle by name; they don't re-implement composition.
- **Per-message tool control** - presets apply at session granularity, not per-turn, to keep prompt caching working.
