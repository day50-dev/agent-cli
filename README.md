# maxac

> Your terminal. Any task. One command.

```bash
ac "how much disk space do I have remaining"
```

You describe what you want. `ac` uses LLM intelligence where it matters — skill matching, tool selection, result verification — so the agent is robust to the variance that breaks classical heuristics. At default verbosity, you just see the answer. Add `-v` or `-vv` for internals.

---

## Install

```bash
uvx/pipx/uv tool maxac
```

Then point it at any OpenAI-compatible API and ask you TA:

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

`maxac` is the full name; `ac` is the short alias.

---

## What happens when you run a task

1. **Defines success** — the LLM writes a concrete, directly verifiable success condition before touching anything (e.g. "output contains filesystem mount points and their total/used/available space" — not vague things like "output contains information about disk usage").
2. **Matches skills** — the LLM checks saved skills against your task. It rejects generic matches (a bare `cat` skill won't fire when you asked about disk space — the LLM knows `df` is the right tool).
3. **Plans** — the LLM picks the best tool for the job (`df` for disk space, `free` for memory, `git` for repos) — not limited to already-linked tools. Any tool on PATH is fair game.
4. **Resolves tools** — needed tools are symlinked automatically (with your approval). If a tool isn't on PATH, the LLM suggests an alternative before falling back to system search.
5. **Executes step-by-step** — each action is visible at `-v`; nothing runs silently.
6. **Verifies** — the LLM checks tool output against the success condition and produces a human-readable answer (e.g. "The CPU is AMD Ryzen 7 and the memory is 16GB").
7. **Learns** — the LLM names the skill, identifies variable arguments, and parameterizes the plan — all via LLM, not regex.

---

## Tool isolation

Instead of reaching into your full system `PATH`, `ac` works with a minimal set of symlinked tools under its config directory:

```
~/.local/maxac/tools/
  doc/bin/    whatis  apropos  man  pydoc
  find/bin/   cat  head  tail  ls
  vcs/bin/    git          ← symlinked on first use, after you say yes
  build/bin/  npm  pip  …
```

When a task needs a new tool, the agent checks PATH first, then asks the LLM for an alternative if needed, then falls back to `whatis`/`apropos` as a last resort. It prompts you before symlinking:

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
~/.local/maxac/skills/
  clone-repo/
    SKILL.md      # YAML frontmatter + instructions (importable to other tools)
    plan.json     # parameterized plan, params_map, success condition
```

The LLM identifies variable arguments (URLs, repo names, paths, versions) and gives them semantic parameter names (e.g. `repository_url`, `branch_name`) — so the same skill works on new inputs without re-planning. Skill matching is also LLM-driven: the model decides whether a saved skill genuinely applies to your task, rejecting false matches like a generic `cat` skill when you asked about disk space.

### Skill commands

```bash
ac --skills                   # list all saved skills
ac --skills clone-repo        # show detail: instructions, plan, params
ac -d clone-repo              # delete a bad skill so it re-learns from scratch
```

---

## Configuration

Config lives at `~/.local/maxac/config.json` (path varies by platform — see table below).

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
| Linux | `~/.local/maxac/` |
| macOS (framework build) | `~/Library/Python/3.x/maxac/` |
| macOS (non-framework) | `~/.local/maxac/` |
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
| `ac -v "<task>"` | Show sections and steps (`-vv` for raw tool output) |
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
