<p align="center">
<img width="750" height="256" alt="logo_750" src="https://github.com/user-attachments/assets/be17868c-417f-45b4-8804-9da8f812e1c3" />

</p>

**Stop wasting time on busted claws**

Openclaw is janky, insecure, doesn't get shit done, and costs too much skrilla. 

Turn to **MAX AC**: a single shot agent. No complex setups or servers. No weirdo config files. No sitting in memory or orchestrating through systemd. 

 * It can run locally on average laptops.
 * Everything is done in Anthropic skills. Import them, export them, modify them.
 * Command permissions are categorized and symlinked. 
 * Specify the skill you want for deteministic results, or don't. Up to you.

MAX gets shit done and then gets out of the way. 

Use **MAX AC**, the coolest terminal agent.

Watch it on a cheap $500 laptop using a local model, LFM 2.5:

https://github.com/user-attachments/assets/28fb8ffc-6645-4162-9c88-8864def15ba1

You describe what you want. `ac` udoes the rest. Skill matching, tool selection, result verification. 

At default verbosity, you just see the answer. Add `-v` or `-vv` for internals.

## Skills

Every successful task is saved as a **skill** — an [Anthropic-compatible](https://agentskills.io) `SKILL.md` + `plan.json` pair under your config directory:

```
~/.local/maxac/skills/
  clone-repo/
    SKILL.md      # YAML frontmatter + instructions (importable to other tools)
    plan.json     # parameterized plan, params_map, success condition
```

The LLM identifies variable arguments (URLs, repo names, paths, versions) and gives them semantic parameter names (e.g. `repository_url`, `branch_name`) — so the same skill works on new inputs without re-planning. Skill matching is also LLM-driven: the model decides whether a saved skill genuinely applies to your task, rejecting false matches.

---

## Install

```bash
uvx/pipx/uv tool maxac
```

Then point it at any OpenAI-compatible API and turn up the AC:

```bash
ac -s model "gemma4:9b"
ac -s base_url "http://localhost:11434/"
```

---

## Try it now

```bash
# Run a one-shot task
ac "list all files in the current directory"
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
  allow symlink: git → tools/vcs/bin/git  [y/N]
```

Use `-y` / `--yes` to pre-approve all symlinks for non-interactive runs:

```bash
ac -y "clone github.com/user/repo as my-repo"
```

---

### Skill commands

```bash
ac --skills                   # list all saved skills
ac --skills clone-repo        # show detail: instructions, plan, params
ac -d clone-repo              # delete a bad skill so it re-learns from scratch
ac --skill name --task file   # explicitly run a skill with a task from a file
ac --import path/to/skill     # import a skill from a dir, .md, or .skill archive
ac --export skill-name        # export a skill to a .skill archive (zip)
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
| `ac --skill <name>` | Explicitly run a specific skill |
| `ac --task <file>` | Read task description from a file |
| `ac -d <name>` | Delete a skill |
| `ac --import <path>` | Import a skill from a dir, .md, or .skill archive |
| `ac --export <name>` | Export a skill to a .skill archive (zip) |
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
