# agent-cli

> Your terminal. Any task. One command.

```bash
ac "clone github.com/psf/requests as requests && list the top-level files"
```

You describe what you want. `ac` figures out the tools, asks before touching anything new, executes step-by-step, and saves the winning plan as a reusable skill for next time.

---

## Install

```bash
uvx/pipx/uv tool agentcli
```

Then point it at any OpenAI-compatible API:

```bash
ac -s model "gemma4:9b"
ac -s base_url "http://localhost:11434/"
```

---

## Try it now

```bash
# Run a one-shot task
ac "list all files in the current directory"

# No arguments → show current status (tools, skills, config)
ac
```

`ac` and `agent-cli` are interchangeable — `ac` is the short alias.

---

## What happens when you run a task

1. **Defines success** — before touching anything, the agent decides what "done" looks like.
2. **Checks skills** — if you've run something similar before, it reuses the saved plan.
3. **Discovers tools** — needs `git`? It finds it on your system and asks permission to symlink it in.
4. **Executes step-by-step** — each action is visible; nothing runs silently in the background.
5. **Learns** — on success, the plan is saved as a skill so the next similar task is instant.

---

## Tool isolation

Instead of reaching into your full system `PATH`, `ac` works with a minimal set of symlinked tools under its config directory:

```
~/.local/agent-cli/tools/
  doc/bin/    whatis  apropos  man  pydoc
  find/bin/   cat  head  tail  ls
  vcs/bin/    git          ← symlinked on first use, after you say yes
  build/bin/  npm  pip  …
```

When a task needs a new tool, the agent finds it with `whatis`/`apropos` and prompts you:

```
  ? allow symlink: git → tools/vcs/bin/git  [y/N]
```

Use `-y` / `--yes` to pre-approve all symlinks for non-interactive runs:

```bash
ac -y "clone github.com/user/repo as my-repo"
```

---

## Skills

Every successful task is saved as a **skill** — an [Anthropic-compatible](https://agentskills.io) `SKILL.md` + `plan.json` pair under your config directory:

```
~/.local/agent-cli/skills/
  clone-repository/
    SKILL.md      # YAML frontmatter + instructions (importable to other tools)
    plan.json     # parameterized plan, task_regex, params_map, success condition
```

Variables (URLs, names, paths) are extracted automatically so the same skill works on new inputs without re-planning.

### Skill commands

```bash
ac --skills                   # list all saved skills
ac --skills clone-repository  # show detail: instructions, plan, regex, params
ac -d clone-repository        # delete a bad skill so it re-learns from scratch
```

---

## Configuration

Config lives at `~/.local/agent-cli/config.json` (path varies by platform — see table below).

### Set values

```bash
ac -s model "gpt-4o"
ac -s base_url "https://api.openai.com/v1"
ac -s key "sk-..."
ac -s                          # show current values (key is masked)
```

`base_url` is auto-corrected — if it doesn't already end with `/v1` or `/v1beta`, `/v1` is appended:

```bash
ac -s base_url "https://integrate.api.nvidia.com"
# → stored as https://integrate.api.nvidia.com/v1
```

### List available models

Run `-m` with no value to query the `/models` endpoint — useful for verifying your key and `base_url`:

```bash
ac -m
#   ✓ models available at https://api.openai.com/v1:
#   · gpt-4o  (openai)
#   · gpt-4o-mini  (openai)
```

### One-shot overrides

Override model, base URL, or key for a single run without changing saved config:

```bash
ac -m "gpt-4o-mini" "summarise this repo in 10 bullets"
ac -b "https://my-proxy.example.com/v1" -k "sk-..." "list all files"
```

### Debug: see the raw API call

```bash
ac --curlify "say hi"
# prints the equivalent curl command before executing
```

### Config directory

| Platform | Default path |
|---|---|
| Linux | `~/.local/agent-cli/` |
| macOS (framework build) | `~/Library/Python/3.x/agent-cli/` |
| macOS (non-framework) | `~/.local/agent-cli/` |
| Override | `-c <path>` / `--config-dir <path>` |

---

## Quick reference

| Command | What it does |
|---|---|
| `ac "<task>"` | Run a one-shot task |
| `ac` | Show status (tools, skills, config) |
| `ac -s model "gpt-4o"` | Set default model |
| `ac -s base_url "…"` | Set default API base URL |
| `ac -s key "…"` | Set default API key |
| `ac -s` | Show current config values |
| `ac -m` | List models at current base URL |
| `ac -m "model" "<task>"` | Run task with a different model |
| `ac -b "url" "<task>"` | Run task with a different base URL |
| `ac -k "key" "<task>"` | Run task with a different API key |
| `ac --skills` | List saved skills |
| `ac --skills <name>` | Show skill detail |
| `ac -d <name>` | Delete a skill |
| `ac -y "<task>"` | Auto-approve all tool symlinks |
| `ac -c <path> "<task>"` | Use a different config directory |
| `ac --curlify "<task>"` | Print the raw API call as curl |

---

## Contributing

If you've run a task and thought "that should just work" — open an issue with:
- what you typed
- what you expected
- what actually happened

PRs welcome.

---

## License

MIT
