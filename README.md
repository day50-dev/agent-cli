# agent-cli

**agent-cli** (alias: **ac**) is a one-shot command-line helper that automates scoped execution tasks. It runs in the foreground, provides clear visual feedback of its progress, uses isolation via symlinked tools, and learns new capabilities ("skills") through successful task execution.

## Core Concepts

### Tool Isolation & Discovery
Instead of accessing your entire system PATH, the agent uses a minimal set of tools symlinked in a config directory (defaults to `~/.local/agent-cli/tools/{category}/bin` on Linux).

- **Default Tools:** Starts with `whatis`, `apropos`, `man`, `pydoc` (for documentation/discovery) and `cat`, `head`, `tail`, `ls` (for basic inspection).
- **Discovery:** When a task requires a new tool (e.g., `git`), the agent uses `whatis` or `apropos` to find it on your system and asks for permission to symlink it into its scoped environment.
- **Classification:** Tools are automatically categorized into groups like `vcs`, `build`, `text`, `net`, `sys`, etc.

### Skills (Learning)
When the agent successfully completes a task, it automatically generalizes the plan and saves it as a **Skill** — following the [Agent Skills standard](https://agentskills.io) for interoperability with the Anthropic Claude skill ecosystem.

Skills are stored as directories under `~/.local/agent-cli/skills/`:

```
skills/
  clone-repository/
    SKILL.md          # YAML frontmatter (name, description) + instructions
    plan.json         # Machine-readable: plan, params_map, task_regex, etc.
```

- **SKILL.md** — Anthropic-compatible skill definition with YAML frontmatter (`name`, `description`, `allowed-tools`) and a markdown body describing when/how to use the skill. This file can be imported/exported to other skill-compatible tools.
- **plan.json** — Machine-readable execution data: the parameterized plan step list, `task_regex` for matching future tasks, `params_map` for parameter extraction, success conditions, and run statistics.
- **Parameterization:** Skills automatically extract variables (like URLs or directory names) from the task description. URL schemes (`https://`) are handled flexibly — the skill matches tasks with or without the prefix.
- **Reuse:** The next time you ask for a similar task, the agent matches the `task_regex` pattern, extracts parameters, and executes the saved plan with new values.
- **Reliability:** Skills track their success count, helping you identify the most reliable automated workflows.

### Custom Programs
If a task cannot be achieved with existing system tools, the agent can write a custom Python program (stored in `tools/custom/bin`) to handle the logic.

---

## Agent Flow

1.  **Success Condition:** Analyzes the task and current directory to define what "success" looks like (e.g., "a new directory named X exists").
2.  **Skill Match:** Checks if any existing skill matches the task using `task_regex` patterns.
3.  **Apply Skill:** If a match is found, it extracts parameters and executes the saved plan.
4.  **Plan Generation:** If no skill applies (or the skill fails to satisfy the success condition), the agent generates a new plan using a Large Language Model (LLM) or internal heuristics.
5.  **Validation:** Before execution, it verifies all required tools are available and symlinked.
6.  **Execution:** Steps through the plan, running tools and showing output.
7.  **Verification & Learning:** Checks the success condition. If passed, it saves the plan as a new, reusable skill in SKILL.md directory format.

---

## Configuration

**agent-cli** is model and provider agnostic. It supports any OpenAI-compatible API. Configuration is stored at `~/.local/agent-cli/config.json`.

### Model Setup
Set your API configuration using the `-s` / `--set` command:
```bash
$ ac -s model "gpt-4o"
$ ac -s base_url "https://api.openai.com/v1"
$ ac -s key "your-api-key"
```

Run `ac -s` with no arguments to show current values (API key is masked).

The `base_url` is auto-corrected: if it doesn't already end with a versioned path like `/v1` or `/v1beta`, `/v1` is appended automatically. So you can set just the domain:
```bash
$ ac -s base_url "https://integrate.api.nvidia.com"
# → resolves to https://integrate.api.nvidia.com/v1
```

### List Available Models
Run `-m` / `--model` with no value to query the `/models` endpoint and see what's available — useful for verifying your `base_url` and key are correct:
```bash
$ ac -m
  ✓ models available at https://api.openai.com/v1:
  · gpt-4o  (openai)
  · gpt-4o-mini  (openai)
```

### One-shot Overrides
You can override configuration for a single run:
```bash
$ ac -m "claude-3" "clone this repo: https://github.com/user/repo"
```

---

## Usage

### Examples
- `ac "clone github.com/akash-network/node as akash-node"`
- `ac "list all files in the current directory"`
- `ac "read the content of README.md"`
- `agent-cli` and `ac` are interchangeable — `ac` is the short alias.

### Management Commands
- **List Skills:** `ac --skills`
- **Skill Detail:** `ac --skills <skill-name>` — shows SKILL.md instructions, parameters, plan, and matching regex
- **Delete a Skill:** `ac -d <skill-name>` — removes a bad skill so it must be re-learned from scratch
- **Auto-approve:** Use `-y` / `--yes` to skip confirmation prompts for symlinking tools.
- **Status:** Running `ac` without arguments shows the current tool and skill status.

### Config Location
The config directory defaults to `site.USER_BASE/agent-cli`:

| Platform | Path |
|---|---|
| Linux | `~/.local/agent-cli/` |
| macOS (framework build) | `~/Library/Python/3.x/agent-cli/` |
| macOS (non-framework) | `~/.local/agent-cli/` |
| Override | `-c <path>` / `--config-dir <path>` or `PYTHONUSERBASE=<dir>` |
