# agent-cli

**agent-cli** is a one-shot command-line helper that automates scoped execution tasks. It runs in the foreground, provides clear visual feedback of its progress, uses isolation via symlinked tools, and learns new capabilities ("skills") through successful task execution.

## Core Concepts

### Tool Isolation & Discovery
Instead of accessing your entire system PATH, the agent uses a minimal set of tools symlinked in a config directory (defaults to `~/.config/agent-cli/tools/{category}/bin`).

- **Default Tools:** Starts with `whatis`, `apropos`, `man`, `pydoc` (for documentation/discovery) and `cat`, `head`, `tail`, `ls` (for basic inspection).
- **Discovery:** When a task requires a new tool (e.g., `git`), the agent uses `whatis` or `apropos` to find it on your system and asks for permission to symlink it into its scoped environment.
- **Classification:** Tools are automatically categorized into groups like `vcs`, `build`, `text`, `net`, `sys`, etc.

### Skills (Learning)
When the agent successfully completes a task, it automatically generalizes the plan and saves it as a **Skill** (stored in `~/.config/agent-cli/skills/`).

- **Parameterization:** Skills automatically extract variables (like URLs or file paths) from the task description using regex.
- **Reuse:** The next time you ask for a similar task, the agent will recognize the pattern and apply the saved skill with new parameters.
- **Reliability:** Skills track their success count, helping you identify the most reliable automated workflows.

### Custom Programs
If a task cannot be achieved with existing system tools, the agent can write a custom Python program (stored in `tools/custom/bin`) to handle the logic.

---

## Agent Flow

1.  **Success Condition:** Analyzes the task and current directory to define what "success" looks like (e.g., "a new directory named X exists").
2.  **Skill Match:** Checks if any existing skill matches the task using regex patterns.
3.  **Apply Skill:** If a match is found, it substitutes parameters and executes the saved plan.
4.  **Plan Generation:** If no skill applies (or the skill fails to satisfy the success condition), the agent generates a new plan using a Large Language Model (LLM) or internal heuristics.
5.  **Validation:** Before execution, it verifies all required tools are available and symlinked.
6.  **Execution:** Steps through the plan, running tools and showing output.
7.  **Verification & Learning:** Checks the success condition. If passed, it saves the plan as a new, reusable skill.

---

## Configuration

**agent-cli** is model and provider agnostic. It supports any OpenAI-compatible API.

### Model Setup
Set your API configuration using the `--set` command:
```bash
$ agent-cli --set model "gpt-4"
$ agent-cli --set base_url "https://api.openai.com/v1"
$ agent-cli --set key "your-api-key"
```

### One-shot Overrides
You can override configuration for a single run:
```bash
$ agent-cli --model "claude-3" "clone this repo: https://github.com/user/repo"
```

---

## Usage

### Examples
- `agent-cli "clone github.com/akash-network/node as akash-node"`
- `agent-cli "list all files in the current directory"`
- `agent-cli "read the content of README.md"`

### Management Commands
- **List Skills:** `agent-cli --skills`
- **Invalidate a Skill:** `agent-cli --invalidate <skill-name>` (prevents the agent from using it)
- **Delete a Skill:** `agent-cli --delete <skill-name>`
- **Auto-approve:** Use `-y` or `--yes` to skip confirmation prompts for symlinking tools.
- **Status:** Running `agent-cli` without arguments shows the current tool and skill status.
