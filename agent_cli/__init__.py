#!/usr/bin/env python3
"""
agent-cli: A one-shot command-line helper with scoped execution tasks.

The agent starts with a minimal tool set and uses those tools (whatis, apropos,
man, pydoc, cat, head, tail, ls) to discover what else is available on the system.
Tools are always observable symlinks in .config/agent-cli/tools/{task}/bin.

Agent flow:
1. Analyze context and define success condition
2. Check for applicable skills
3. Apply skill if found, verify against success condition
4. Create a plan using current tools
5. Step through the plan assessing each step before execution
6. If more tools needed, ask user to allow/install them; otherwise execute
"""

import argparse
import json
import os
import re
import shutil
import site
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional
import yaml


# Default configuration — lives under site.USER_BASE (~/.local on Linux,
# ~/Library/Python/<ver> on macOS) so it's always in a stable user location,
# not relative to the current working directory.
DEFAULT_CONFIG_DIR = Path(site.USER_BASE) / "agent-cli"
DEFAULT_TOOLS_DIR = DEFAULT_CONFIG_DIR / "tools"
DEFAULT_SKILLS_DIR = DEFAULT_CONFIG_DIR / "skills"
DEFAULT_CONFIG_FILE = DEFAULT_CONFIG_DIR / "config.json"

# Minimal default tools by task category
DEFAULT_TOOLS = {
    "doc": ["whatis", "apropos", "man", "pydoc"],
    "find": ["cat", "head", "tail", "ls"],
}

# Tool name → task category classification.
# Used to decide where a newly discovered tool gets symlinked.
TOOL_TASK_CLASSIFIER = {
    # version control
    "git": "vcs",
    "gh": "vcs",
    "hub": "vcs",
    "svn": "vcs",
    "hg": "vcs",
    # build / package
    "make": "build",
    "cmake": "build",
    "cargo": "build",
    "npm": "build",
    "pip": "build",
    "yarn": "build",
    "go": "build",
    "maven": "build",
    "gradle": "build",
    # text / data processing
    "grep": "find",
    "awk": "text",
    "sed": "text",
    "jq": "text",
    "sort": "text",
    "cut": "text",
    "wc": "text",
    # network / download
    "curl": "net",
    "wget": "net",
    "ssh": "net",
    "scp": "net",
    "rsync": "net",
    # system / info
    "uname": "sys",
    "df": "sys",
    "du": "sys",
    "ps": "sys",
    "top": "sys",
    "htop": "sys",
    # docs (expand beyond the defaults)
    "info": "doc",
    "help": "doc",
}


def classify_tool(name: str) -> str:
    """Return the task category for a tool name. Falls back to 'general'."""
    return TOOL_TASK_CLASSIFIER.get(name, "general")


# ANSI escape codes — module-level for use by Spinner animation.
_ANSI_BOLD   = "\033[1m"
_ANSI_DIM    = "\033[2m"
_ANSI_RESET  = "\033[0m"
_ANSI_GREEN  = "\033[32m"
_ANSI_RED    = "\033[31m"
_ANSI_YELLOW = "\033[33m"
_ANSI_CYAN   = "\033[36m"
_ANSI_BLUE   = "\033[34m"

# Command words and flags that should NOT be turned into skill parameters.
# These are static command sub-words (like "clone" in "git clone") that are
# part of the tool's interface, not variable inputs.
STATIC_COMMAND_WORDS = {
    "clone", "init", "add", "commit", "push", "pull", "fetch", "checkout",
    "ls", "cat", "head", "tail", "man", "cp", "mv", "rm", "mkdir", "rmdir",
    "install", "build", "test", "run", "start", "stop", "status", "log",
    "diff", "branch", "merge", "rebase", "create", "delete", "update",
    "list", "show", "read", "write", "generate", "new", "and", "or",
}


# ------------------------------------------------------------------
# Markdown helpers
# ------------------------------------------------------------------

def _md_escape(text: str) -> str:
    """Escape backtick characters for safe use inside markdown code spans."""
    return text.replace('`', r'\`')


# ------------------------------------------------------------------
# Output primitives — semantic abstractions for CLI display
# ------------------------------------------------------------------

class Output:
    """Semantic output layer for CLI display.

    All output is rendered as markdown via streamdown for polished
    terminal presentation.  Each semantic primitive (headline, section,
    success, fatal, etc.) maps to a small markdown fragment rendered
    through a persistent Streamdown instance.

    When streamdown is unavailable, a plain-text fallback is used.
    Tool output lines and interactive prompts bypass markdown rendering
    since their content is arbitrary / needs stdin interaction.
    """

    def __init__(self):
        self._sd = None
        try:
            from streamdown import Streamdown
            self._sd = Streamdown()
            import atexit
            atexit.register(self._tidy)
        except ImportError:
            pass

    def _tidy(self):
        """Clean up the Streamdown instance at program exit."""
        if self._sd:
            self._sd.tidyup()

    def _render(self, text: str) -> None:
        """Render a markdown fragment through streamdown, or fall back to print."""
        if self._sd:
            self._sd.render(text)
        else:
            # Fallback: strip basic markdown for plain-text display
            clean = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
            clean = re.sub(r'\*(.+?)\*', r'\1', clean)
            clean = re.sub(r'`(.+?)`', r'\1', clean)
            # Collapse runs of 3+ blank lines into 1, but preserve
            # single blank lines so sections stay visually separated.
            clean = re.sub(r'\n{3,}', '\n\n', clean)
            print(clean, end='')

    # ---- semantic primitives ----

    def headline(self, text: str) -> None:
        """Top-level task title — the main thing happening."""
        self._render(f"# {text}\n\n")

    def section(self, text: str) -> None:
        """Sub-step / phase header — groups related output."""
        self._render(f"\n### {text}\n")

    def subsection(self, text: str) -> None:
        self._render(f"\n#### {text}\n")

    def success(self, text: str) -> None:
        """Positive outcome: approved, completed, confirmed."""
        self._render(f"✓ {text}\n")

    def warning(self, text: str) -> None:
        """Non-critical issue: degraded path, fallback, something to note."""
        self._render(f"⚠ *{text}*\n\n")

    def fatal(self, text: str) -> None:
        """Critical failure: cannot proceed."""
        self._render(f"✗ **{text}**\n")

    def info(self, text: str) -> None:
        """Neutral informational note."""
        self._render(f"* {text}\n")

    def command(self, text: str) -> None:
        """A command about to be executed."""
        self._render(f"- {_md_escape(text)}\n")

    def output(self, text: str) -> None:
        """Captured output line from a tool."""
        self._render(f"> {_md_escape(text)}\n")

    def prompt(self, text: str, end: str = "\n") -> None:
        """User interaction prompt."""
        # Interactive prompts need direct stdout — don't render as markdown.
        print(f"\n  ? {text}", end=end)

    def separator(self) -> None:
        """Horizontal rule separating major sections."""
        self._render("---\n\n")

    # ---- compound helpers ----

    def kv(self, key: str, value: str) -> None:
        """Key-value pair in a detail view (left-aligned label + value)."""
        self._render(f"* {key}: `{_md_escape(value)}`\n")

    # ---- markdown rendering (streamdown) ----

    def markdown(self, text: str) -> None:
        """Render a bulk markdown document through streamdown."""
        self._render(text)

    # ---- spinner ----

    def spinner(self, label: str = "Thinking"):
        """Return a Spinner context manager animating while waiting."""
        return Spinner(label, no_color=not sys.stdout.isatty())


class Spinner:
    """Context-manager spinner that animates on the terminal while waiting.

    Usage::

        with spinner("Thinking"):
            result = slow_operation()

    The spinner clears itself on exit and restores the cursor.
    """

    _FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    _INTERVAL = 0.08  # seconds per frame

    def __init__(self, label: str = "Thinking", no_color: bool = False):
        self._label = label
        self._no_tty = no_color or not sys.stdout.isatty()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ---- colour helpers (uses module-level ANSI codes) ----

    def _c(self, code: str, text: str) -> str:
        if self._no_tty:
            return text
        return f"{code}{text}{_ANSI_RESET}"

    def _spin(self):
        idx = 0
        while not self._stop.is_set():
            frame = self._FRAMES[idx % len(self._FRAMES)]
            dim_label = self._c(_ANSI_DIM, self._label)
            colored_frame = self._c(_ANSI_CYAN, frame)
            # \r returns to column 0; \033[?25l hides cursor
            sys.stdout.write(f"\r  {colored_frame} {dim_label}…\033[?25l")
            sys.stdout.flush()
            self._stop.wait(self._INTERVAL)
            idx += 1

    # ---- context-manager protocol ----

    def __enter__(self):
        if self._no_tty:
            # Complete no-op when stdout is not a TTY — avoids garbled piped output
            return self
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        if self._thread:
            self._thread.join()
        if not self._no_tty:
            # Clear the spinner line and restore cursor
            sys.stdout.write("\r" + " " * (len(self._label) + 10) + "\r\033[?25h")
            sys.stdout.flush()


class AgentCLI:
    """Main agent-cli class."""

    def __init__(self, config_dir: Optional[Path] = None, auto_yes: bool = False):
        self.config_dir = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
        self.auto_yes = auto_yes
        self.tools_dir = self.config_dir / "tools"
        self.skills_dir = self.config_dir / "skills"
        self.config_file = self.config_dir / "config.json"

        self.model_config = {
            "base_url": None,
            "model": None,
            "key": None,
        }

        self.out = Output()
        self._skills_migrated = False
        self._curlify_mode = False

        self._load_config()
        self._setup_directories()
        self._setup_default_tools()

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    def _load_config(self):
        if self.config_file.exists():
            try:
                with open(self.config_file) as f:
                    config = json.load(f)
                    self.model_config.update(config.get("model", {}))
            except (json.JSONDecodeError, IOError):
                pass

    def _save_config(self):
        self.config_dir.mkdir(parents=True, exist_ok=True)
        with open(self.config_file, "w") as f:
            json.dump({"model": self.model_config}, f, indent=2)

    def set_model_config(self, key: str, value: str):
        if key in self.model_config:
            self.model_config[key] = value
            self._save_config()
        else:
            print(
                f"Error: Unknown config key '{key}'. "
                f"Valid keys: {', '.join(self.model_config.keys())}"
            )
            sys.exit(1)

    def show_model_config(self):
        """Display current model configuration values."""
        self.out.section("Model Config")
        for key, value in self.model_config.items():
            self.out.kv(key, value or "(not set)")

    # ------------------------------------------------------------------
    # Directory & symlink setup
    # ------------------------------------------------------------------
    def _setup_directories(self):
        self.tools_dir.mkdir(parents=True, exist_ok=True)
        self.skills_dir.mkdir(parents=True, exist_ok=True)

    def _setup_default_tools(self):
        """Create symlinks for the minimal default tool set."""
        for task, tools in DEFAULT_TOOLS.items():
            task_bin = self.tools_dir / task / "bin"
            task_bin.mkdir(parents=True, exist_ok=True)
            for tool in tools:
                link = task_bin / tool
                if not link.exists() and not link.is_symlink():
                    target = shutil.which(tool)
                    if target:
                        link.symlink_to(target)

    # ------------------------------------------------------------------
    # Tool discovery
    # ------------------------------------------------------------------
    def get_all_symlinked_tools(self) -> dict:
        """Return every tool currently symlinked under tools/{task}/bin."""
        result = {}
        if not self.tools_dir.exists():
            return result
        for task_dir in sorted(self.tools_dir.iterdir()):
            if not task_dir.is_dir():
                continue
            bin_dir = task_dir / "bin"
            if not bin_dir.exists():
                continue
            names = sorted(
                t.name
                for t in bin_dir.iterdir()
                if t.is_symlink() or t.is_file()
            )
            if names:
                result[task_dir.name] = names
        return result

    def all_tool_names(self) -> set:
        """Flat set of every tool name currently available."""
        names = set()
        for task_tools in self.get_all_symlinked_tools().values():
            names.update(task_tools)
        return names

    # ------------------------------------------------------------------
    # Discovery via the minimal tool set
    # ------------------------------------------------------------------
    def _run_symlinked(self, tool: str, args: list[str] | None = None, timeout: int = 120,
                       show_cmd: bool = True) -> tuple[int, str, str]:
        """Run a tool that is already symlinked in our tools directory."""
        args = args or []
        for task_dir in self.tools_dir.iterdir():
            bin_dir = task_dir / "bin"
            candidate = bin_dir / tool
            if candidate.exists() or candidate.is_symlink():
                cmd_display = f"{tool} {' '.join(args)}" if args else tool
                if show_cmd:
                    self.out.command(cmd_display)
                try:
                    r = subprocess.run(
                        [str(candidate)] + args,
                        capture_output=True, text=True, timeout=timeout,
                    )
                    return r.returncode, r.stdout, r.stderr
                except subprocess.TimeoutExpired:
                    return 1, "", f"Tool '{tool}' timed out after {timeout}s"
                except Exception as e:
                    return 1, "", str(e)
        return 1, "", f"Tool '{tool}' not found in symlinked tools"

    def discover_tool(self, name: str) -> bool:
        """
        Use the minimal discovery tools (whatis, apropos) to check whether
        *name* is available on the system.  Does NOT search the filesystem
        beyond what these unix utilities do.
        """
        # 1. Check if already symlinked
        if name in self.all_tool_names():
            return True

        # 2. Try whatis
        rc, out, _ = self._run_symlinked("whatis", [name])
        if rc == 0 and out.strip() and "nothing appropriate" not in out.lower():
            return True

        # 3. Try apropos (partial match)
        rc, out, _ = self._run_symlinked("apropos", [name])
        if rc == 0 and name in out.lower():
            return True

        # 4. Fallback: check PATH directly
        return shutil.which(name) is not None

    def find_tool_path(self, name: str) -> Optional[str]:
        """Return the absolute PATH location of *name* if it exists."""
        return shutil.which(name)

    # ------------------------------------------------------------------
    # Symlink management
    # ------------------------------------------------------------------
    def symlink_tool(self, name: str, task: str | None = None, auto_yes: bool = False) -> bool:
        """
        Symlink a system binary into our tools directory after user
        confirmation.  Returns True on success.

        If *task* is None the tool is auto-classified via TOOL_TASK_CLASSIFIER.
        """
        if name in self.all_tool_names():
            return True

        path = self.find_tool_path(name)
        if not path:
            self.out.fatal(f"'{name}' not found on PATH — may need to be installed")
            return False

        if task is None:
            task = classify_tool(name)

        task_bin = self.tools_dir / task / "bin"
        task_bin.mkdir(parents=True, exist_ok=True)
        link = task_bin / name

        if auto_yes:
            self.out.success(f"symlink  {name} → {path}  [{task}]")
        else:
            self.out.prompt(f"Allow '{name}' ({path})?  category: [{task}]  (y/n): ", end="")
            resp = sys.stdin.readline().strip().lower()
            if resp != "y":
                self.out.info("skipped")
                return False

        link.symlink_to(path)
        return True

    # ------------------------------------------------------------------
    # Skills  (Anthropic SKILL.md directory format)
    # ------------------------------------------------------------------
    #
    # Each skill is stored as a directory:
    #   skills/<skill-name>/
    #     SKILL.md   — YAML frontmatter (name, description) + markdown
    #                   body with instructions / context for LLM use
    #     plan.json  — machine-readable execution data:
    #                   plan, params_map, task_regex, success_condition,
    #                   success_count, tools_used
    #
    # This follows the Agent Skills standard (agentskills.io) and is
    # compatible with Anthropic's Claude skill ecosystem.

    def _migrate_legacy_skills(self):
        """One-time migration: convert flat .json skill files to SKILL.md directories,
        and clean up any skills previously marked as 'invalidated' (now deleted)."""
        if self._skills_migrated:
            return
        self._skills_migrated = True
        if not self.skills_dir.exists():
            return
        for f in list(self.skills_dir.iterdir()):
            if not (f.is_file() and f.suffix == ".json"):
                continue
            try:
                with open(f) as fh:
                    data = json.load(fh)
            except (json.JSONDecodeError, IOError):
                continue

            skill_name = data.get("name", f.stem)
            skill_dir = self.skills_dir / skill_name
            if skill_dir.exists():
                # Already migrated — remove orphaned JSON
                f.unlink()
                continue

            skill_dir.mkdir(parents=True, exist_ok=True)

            # Write plan.json
            plan_data = {k: data[k] for k in data
                         if k not in ("name", "description")}
            with open(skill_dir / "plan.json", "w") as fh:
                json.dump(plan_data, fh, indent=2)

            # Synthesize a SKILL.md
            desc = data.get("description", f"Skill: {skill_name}")
            tools = data.get("tools_used", [])
            allowed = " ".join(tools) if tools else ""
            frontmatter = {
                "name": skill_name,
                "description": desc,
            }
            if allowed:
                frontmatter["allowed-tools"] = allowed

            body = (
                f"# {skill_name}\n\n"
                f"{desc}\n\n"
                f"### Parameters\n"
                "This skill is parameterized.  Values are extracted from the\n"
                "user's task string via `task_regex` and substituted into the\n"
                "plan before execution.\n"
            )
            skill_md = f"---\n{yaml.dump(frontmatter, default_flow_style=False).strip()}\n---\n\n{body}"
            with open(skill_dir / "SKILL.md", "w") as fh:
                fh.write(skill_md)

            # Remove the old JSON file
            f.unlink()

        # Also clean up skills that were previously 'invalidated' —
        # invalidate is now delete, so legacy invalidated dirs are stale.
        for d in list(self.skills_dir.iterdir()):
            if not d.is_dir():
                continue
            plan_file = d / "plan.json"
            if plan_file.exists():
                try:
                    with open(plan_file) as fh:
                        if json.load(fh).get("invalidated"):
                            shutil.rmtree(d, ignore_errors=True)
                except (json.JSONDecodeError, IOError):
                    pass

    def _parse_skill_md(self, skill_dir: Path) -> Optional[dict]:
        """Read SKILL.md frontmatter and return the YAML metadata dict."""
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            return None
        text = skill_md.read_text()
        # Extract YAML frontmatter between --- delimiters
        m = re.match(r'^---\s*\n(.*?)\n---', text, re.DOTALL)
        if not m:
            return None
        try:
            return yaml.safe_load(m.group(1))
        except yaml.YAMLError:
            return None

    def get_available_skills(self) -> list[dict]:
        """Return list of skill metadata from SKILL.md directories."""
        self._migrate_legacy_skills()
        skills = []
        if not self.skills_dir.exists():
            return skills
        for d in sorted(self.skills_dir.iterdir()):
            if not d.is_dir():
                continue
            skill_md_meta = self._parse_skill_md(d)
            plan_file = d / "plan.json"
            plan_data = {}
            if plan_file.exists():
                try:
                    with open(plan_file) as fh:
                        plan_data = json.load(fh)
                except (json.JSONDecodeError, IOError):
                    pass

            name = (skill_md_meta or {}).get("name", d.name)
            skills.append({
                "name": name,
                "dir": str(d),
                "description": (skill_md_meta or {}).get("description", ""),
                "task_regex": plan_data.get("task_regex", ""),
                "params_map": plan_data.get("params_map", {}),
                "success_count": plan_data.get("success_count", 1),
                "tools_used": plan_data.get("tools_used", []),
            })
        return skills

    def _find_applicable_skill(self, task: str) -> Optional[dict]:
        """Find a skill that matches the task using the stored task_regex pattern.

        When a match is found, the extracted parameter values are stored
        in skill["_extracted_params"] for use by apply_skill().
        """
        for skill in self.get_available_skills():
            regex = skill.get("task_regex", "")
            if not regex:
                continue
            try:
                match = re.search(regex, task, re.IGNORECASE)
                if match:
                    # Store extracted parameters for use by apply_skill
                    skill["_extracted_params"] = match.groupdict()
                    return skill
            except re.error:
                # Invalid regex in skill file — skip it
                continue
        return None

    def _load_skill(self, name: str) -> dict:
        """Load a skill by name. Returns merged SKILL.md + plan.json data."""
        self._migrate_legacy_skills()
        # Try directory match by name
        skill_dir = self.skills_dir / name
        if skill_dir.is_dir():
            skill_md_meta = self._parse_skill_md(skill_dir) or {}
            plan_data = {}
            plan_file = skill_dir / "plan.json"
            if plan_file.exists():
                try:
                    with open(plan_file) as fh:
                        plan_data = json.load(fh)
                except (json.JSONDecodeError, IOError):
                    pass
            # Merge: SKILL.md frontmatter provides name/description,
            # plan.json provides everything else
            result = {"name": skill_md_meta.get("name", name),
                      "description": skill_md_meta.get("description", "")}
            result.update(plan_data)
            return result

        # Try matching against the 'name' field inside each directory's SKILL.md
        for d in self.skills_dir.iterdir():
            if not d.is_dir():
                continue
            meta = self._parse_skill_md(d)
            if meta and meta.get("name") == name:
                plan_data = {}
                plan_file = d / "plan.json"
                if plan_file.exists():
                    try:
                        with open(plan_file) as fh:
                            plan_data = json.load(fh)
                    except (json.JSONDecodeError, IOError):
                        pass
                result = {"name": meta.get("name", name),
                          "description": meta.get("description", "")}
                result.update(plan_data)
                return result
        return {}

    def _generalize_skill_name(self, plan: list[dict]) -> str:
        """Derive a generalized skill name from the plan's primary action.

        E.g. plan with action 'clone_repository' → 'clone-repository'

        The action should be a generic verb (the function), not the subject.
        As a safety net, if the action is over-specific (3+ underscore
        parts like 'clone_the_akash_network'), only the verb part is
        kept ('clone').
        """
        for step in plan:
            action = step.get("action", "")
            if not action:
                continue
            name = action.replace("-", "_").lower()
            parts = name.split("_")
            if len(parts) <= 2:
                # Short, generic action like 'clone_repository' — use as-is
                return name.replace("_", "-")
            # Over-specific — keep verb + first noun (e.g. install_python)
            return "_".join(parts[:2])
        return "generic-task"

    def _infer_param_name(self, value: str) -> str:
        """Generate a semantic parameter name based on the value type."""
        if re.match(r'https?://', value):
            return "repo_url"
        if re.match(r'^\d+\.\d+', value):
            return "version"
        if '/' in value and not value.startswith('/') and not value.startswith('-'):
            return "repo_path"
        return "target"

    def _parameterize_plan(self, task: str, plan: list[dict]) -> tuple[list[dict], dict[str, str], str]:
        """Replace specific values in plan with placeholders.

        Returns (param_plan, params_map, task_regex) where:
          param_plan: plan with {{param_name}} placeholders in args
          params_map: {original_value: param_name}
          task_regex: regex to extract params from future task strings
        """
        # 1. Collect variable values from the plan that appear in the task
        params_map = {}  # value → param_name
        name_counts = {}  # param_name → count (for dedup)

        for step in plan:
            for arg in step.get("args", []):
                # Strip URL scheme for comparison — the task string may
                # say "github.com/..." while the plan arg has
                # "https://github.com/...".  We want to match either.
                stripped = re.sub(r'^https?://', '', arg)
                match_in_task = (
                    re.search(rf'(?:^|\s){re.escape(arg)}(?:\s|$)', task)
                    or re.search(rf'(?:^|\s){re.escape(stripped)}(?:\s|$)', task)
                )
                if (match_in_task
                        and arg not in STATIC_COMMAND_WORDS
                        and not arg.startswith("-")
                        and len(arg) > 1
                        and arg not in params_map):
                    name = self._infer_param_name(arg)
                    # Handle name collisions (e.g. multiple URLs → repo_url, repo_url_2)
                    if name in name_counts:
                        name_counts[name] += 1
                        name = f"{name}_{name_counts[name]}"
                    else:
                        name_counts[name] = 1
                    params_map[arg] = name

        # 2. Build parameterized plan
        param_plan = []
        for step in plan:
            new_step = dict(step)
            new_args = []
            for arg in step.get("args", []):
                if arg in params_map:
                    new_args.append(f"{{{{{params_map[arg]}}}}}")
                else:
                    new_args.append(arg)
            new_step["args"] = new_args
            param_plan.append(new_step)

        # 3. Build task_regex from task string
        task_regex = self._build_task_regex(task, params_map)

        return param_plan, params_map, task_regex

    def _build_task_regex(self, task: str, params_map: dict[str, str]) -> str:
        """Build a regex that captures parameter values from future task strings.

        Static parts of the task are re.escape()'d; variable parts become
        named capture groups like (?P<repo_url>https?://\\S+).
        """
        if not params_map:
            return re.escape(task)

        # Find positions of all parameter values in the task string
        # URL values like https://github.com/org/repo may not appear directly
        # in the task string (which might say just github.com/org/repo),
        # so we try both the full value and the scheme-stripped version.
        positions = []
        for value, name in params_map.items():
            idx = task.find(value)
            stripped = re.sub(r'^https?://', '', value)
            match_len = len(value)
            if idx < 0 and stripped != value:
                # Value not in task — try the stripped form
                idx = task.find(stripped)
                if idx >= 0:
                    # The stripped form is what's actually in the task;
                    # its length determines the static-text positions.
                    match_len = len(stripped)
            if idx >= 0:
                positions.append((idx, match_len, name, value))

        # Sort by position (left to right)
        positions.sort(key=lambda x: x[0])

        # Build regex by alternating escaped static text with capture groups
        parts = []
        last_end = 0
        for idx, length, name, value in positions:
            # Skip overlapping positions
            if idx < last_end:
                continue
            # Escape static part before this parameter
            if idx > last_end:
                parts.append(re.escape(task[last_end:idx]))
            # Choose capture pattern based on value type
            if re.match(r'https?://', value):
                # Make the scheme optional so the regex matches tasks
                # with or without the https:// prefix
                parts.append(rf"(?P<{name}>(?:https?://)?[\w.-]+(?:/[\w.-]+)+)")
            elif re.match(r'^\d+\.\d+', value):
                parts.append(rf"(?P<{name}>\d+\.\d+(?:\.\d+)*)")
            else:
                parts.append(rf"(?P<{name}>\S+)")
            last_end = idx + length

        # Remaining static part after last parameter
        if last_end < len(task):
            parts.append(re.escape(task[last_end:]))

        return "".join(parts)

    def _parameterize_success_condition(self, condition: str, params_map: dict[str, str]) -> str:
        """Replace concrete values in the success condition string with {{param}} placeholders.

        Only replaces values that appear in params_map (i.e., were also
        parameterized in the plan), so the condition stays consistent
        with the parameterized plan at apply-time.
        """
        result = condition
        for orig, param_name in params_map.items():
            result = result.replace(orig, f"{{{{{param_name}}}}}")
        return result

    def _save_skill(self, task: str, plan: list[dict], success_condition: str,
                    tools_used: list[str], success: bool = True) -> Optional[str]:
        """Persist the completed task as a reusable, parameterized skill.

        Skills are stored in Anthropic SKILL.md directory format:
          skills/<name>/SKILL.md  — frontmatter + instructions
          skills/<name>/plan.json  — machine-readable plan data
        """
        # Don't save skills that don't actually do anything
        if not tools_used:
            self.out.info("no tools used — skill not saved (empty plans are useless)")
            return None

        self.skills_dir.mkdir(parents=True, exist_ok=True)

        # Generate a generalized skill name from the plan's action type
        skill_name = self._generalize_skill_name(plan)
        skill_dir = self.skills_dir / skill_name

        # If skill directory already exists, check plan compatibility
        plan_file = skill_dir / "plan.json"
        existing_plan_data = {}
        if plan_file.exists():
            try:
                with open(plan_file) as fh:
                    existing_plan_data = json.load(fh)
                existing_plan = existing_plan_data.get("plan", [])
                # Compare plan structure: same length AND same action+tool per step
                same_structure = (
                    len(existing_plan) == len(plan)
                    and all(
                        existing_plan[i].get("action") == plan[i].get("action")
                        and existing_plan[i].get("tool") == plan[i].get("tool")
                        for i in range(len(plan))
                    )
                )
                if not same_structure:
                    # Different plan structure — save as a variant
                    variant = len(plan)
                    skill_name = f"{skill_name}-{variant}"
                    skill_dir = self.skills_dir / skill_name
                    existing_plan_data = {}
                else:
                    # Same structure — bump success count, keep existing parameterization
                    existing_plan_data["success_count"] = existing_plan_data.get("success_count", 0) + (1 if success else 0)
                    with open(plan_file, "w") as fh:
                        json.dump(existing_plan_data, fh, indent=2)
                    return str(skill_dir)
            except (json.JSONDecodeError, IOError):
                pass

        # New skill — parameterize the plan
        param_plan, params_map, task_regex = self._parameterize_plan(task, plan)

        # Parameterize the success_condition too
        param_condition = self._parameterize_success_condition(success_condition, params_map)

        # Build a human-readable description from the plan
        # Deduplicate actions: clone_repository ×2 instead of clone_repository, clone_repository
        action_counts: dict[str, int] = {}
        for s in param_plan:
            a = s.get("action", "?")
            action_counts[a] = action_counts.get(a, 0) + 1
        desc_parts = []
        for a, count in action_counts.items():
            if count > 1:
                desc_parts.append(f"{a} (×{count})")
            else:
                desc_parts.append(a)
        desc = f"Auto-learned skill: {', '.join(desc_parts)}"

        # Write SKILL.md (Anthropic format)
        skill_dir.mkdir(parents=True, exist_ok=True)
        tools_str = " ".join(sorted(set(tools_used)))
        frontmatter = {
            "name": skill_name,
            "description": desc,
        }
        if tools_str:
            frontmatter["allowed-tools"] = tools_str

        # Build instruction body
        param_lines = []
        for orig_val, param_name in params_map.items():
            param_lines.append(f"- `{{{{{param_name}}}}}` — extracted from task string (was `{orig_val}`)")
        params_section = ""
        if param_lines:
            params_section = (
                "\n### Parameters\n"
                "Values are extracted from the task string via `task_regex` "
                "and substituted into the plan before execution.\n\n"
                + "\n".join(param_lines) + "\n"
            )

        body = (
            f"# {skill_name}\n\n"
            f"{desc}\n"
            f"{params_section}"
        )
        skill_md = f"---\n{yaml.dump(frontmatter, default_flow_style=False).strip()}\n---\n\n{body}"
        with open(skill_dir / "SKILL.md", "w") as fh:
            fh.write(skill_md)

        # Write plan.json (machine-readable execution data)
        plan_data = {
            "task_regex": task_regex,
            "params_map": params_map,
            "plan": param_plan,
            "tools_used": sorted(set(tools_used)),
            "success_condition": param_condition,
            "success_count": 1 if success else 0,
        }
        with open(plan_file, "w") as fh:
            json.dump(plan_data, fh, indent=2)
        return str(skill_dir)

    def _find_skill_dir(self, skill_name: str) -> Optional[Path]:
        """Find a skill directory by name. Returns the Path or None."""
        self._migrate_legacy_skills()
        skill_dir = self.skills_dir / skill_name
        if skill_dir.is_dir():
            return skill_dir
        # Try matching against the 'name' field inside each directory's SKILL.md
        for d in self.skills_dir.iterdir():
            if not d.is_dir():
                continue
            meta = self._parse_skill_md(d)
            if meta and meta.get("name") == skill_name:
                return d
        return None

    def delete_skill(self, skill_name: str) -> bool:
        """Permanently remove a skill directory."""
        skill_dir = self._find_skill_dir(skill_name)
        if not skill_dir:
            self.out.fatal(f"skill '{skill_name}' not found")
            return False

        shutil.rmtree(skill_dir)
        self.out.success(f"skill '{skill_name}' deleted")
        return True

    def apply_skill(self, skill_meta: dict) -> tuple[bool, str]:
        """Execute a saved skill's plan with parameter substitution.

        Extracts parameter values from the current task (stored in
        skill_meta["_extracted_params"] by _find_applicable_skill), substitutes
        them into the parameterized plan, then executes.

        Returns (success, captured_output).
        """
        # Load full skill data from file
        full = self._load_skill(skill_meta["name"])
        if not full:
            self.out.info(f"skill file not found for '{skill_meta['name']}'")
            return False, ""

        plan = full.get("plan", [])
        if not plan:
            self.out.info("skill has no saved plan")
            return False, ""

        # Reject skills where no step actually does anything
        if not any(step.get("tool") for step in plan):
            self.out.warning(f"skill '{skill_meta['name']}' has no executable steps — skipping")
            return False, ""

        # Get extracted parameters (set by _find_applicable_skill)
        extracted_params = skill_meta.get("_extracted_params", {})

        # Resolve parameter placeholders in plan args
        resolved_plan = []
        for step in plan:
            resolved_step = dict(step)
            resolved_args = []
            for arg in step.get("args", []):
                resolved_arg = arg
                for param_name, param_value in extracted_params.items():
                    resolved_arg = resolved_arg.replace(f"{{{{{param_name}}}}}", param_value)
                resolved_args.append(resolved_arg)
            resolved_step["args"] = resolved_args
            resolved_plan.append(resolved_step)

        # Check for unresolved parameters
        unresolved = []
        for step in resolved_plan:
            for arg in step.get("args", []):
                unresolved.extend(re.findall(r'\{\{(\w+)\}\}', arg))
        if unresolved:
            self.out.fatal(f"unresolved parameters: {', '.join(set(unresolved))}")
            return False, ""

        self.out.success(f"executing skill: {full.get('name', skill_meta['name'])}")
        if extracted_params:
            param_str = ", ".join(f"{k}={v}" for k, v in extracted_params.items())
            self.out.info(f"params: {param_str}")
        if full.get("tools_used"):
            self.out.info(f"tools: {', '.join(full['tools_used'])}")

        # Validate each step
        for i, step in enumerate(resolved_plan):
            tool = step.get("tool")
            if tool and tool not in self.all_tool_names():
                if self.discover_tool(tool):
                    if not self.symlink_tool(tool, auto_yes=self.auto_yes):
                        self.out.fatal(f"cannot proceed without '{tool}'")
                        return False, ""
                else:
                    self.out.fatal(f"tool '{tool}' not available on this system")
                    return False, ""

        # Execute — capture output for verification
        captured = []
        for i, step in enumerate(resolved_plan):
            self.out.subsection(f"skill step {i + 1}: {step['action']}")
            tool = step.get("tool")
            args = step.get("args", [])

            if tool:
                rc, out, err = self._run_symlinked(tool, args, timeout=300, show_cmd=True)
                if out.strip():
                    for line in out.strip().splitlines():
                        self.out.output(line)
                    captured.append(out.strip())
                if err.strip():
                    for line in err.strip().splitlines():
                        self.out.output(line)
                    captured.append(err.strip())
            else:
                self.out.fatal(f"no tool for step '{step['action']}' — cannot execute")
                return False, ""

        return True, "\n".join(captured)

    # ------------------------------------------------------------------
    # Success condition
    # ------------------------------------------------------------------
    def _verify_success(self, task: str, success_condition: str, captured_output: str) -> bool:
        """Ask the LLM to verify whether the tool output satisfies the success condition."""
        if not captured_output.strip():
            self.out.fatal("no output produced — cannot verify success")
            return False

        # Truncate output to avoid excessive token usage
        output_for_llm = captured_output[:4000]

        system_prompt = (
            "You are a verification assistant. Determine if the given task was "
            "successfully completed based strictly on the provided success condition "
            "and tool output. Respond ONLY with valid JSON."
        )
        user_prompt = (
            f"Task: {task}\n"
            f"Success condition: {success_condition}\n\n"
            "Tool output:\n"
            f"```\n{output_for_llm}\n```\n\n"
            'Respond in this JSON format:\n'
            '{"satisfied": true/false, "reason": "brief explanation"}'
        )

        result = self._call_model([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ])

        if result is None:
            self.out.warning("verification call failed — assuming success")
            return True

        result = result.strip()
        if result.startswith("```"):
            result = re.sub(r'^```(?:json)?\n?', '', result)
            result = re.sub(r'\n?```$', '', result)
            result = result.strip()

        try:
            parsed = json.loads(result)
            satisfied = parsed.get("satisfied", False)
            reason = parsed.get("reason", "")
            if satisfied:
                self.out.success(f"verified: {reason}")
                return True
            else:
                self.out.fatal(f"verification failed: {reason}")
                return False
        except (json.JSONDecodeError, ValueError):
            self.out.warning("verification response unparseable — assuming success")
            return True

    # ------------------------------------------------------------------
    # LLM inference
    # ------------------------------------------------------------------
    def _resolve_base_url(self) -> str:
        """Return a base URL that includes the /v1 (or /vN) API version prefix.

        Most OpenAI-compatible providers expect paths like /v1/chat/completions.
        If the user's base_url already ends with a version segment (e.g. /v1,
        /v2, /v1beta), it's kept as-is.  Otherwise /v1 is appended automatically.
        """
        base_url = self.model_config.get("base_url") or "https://api.openai.com/v1"
        base_url = base_url.rstrip('/')
        # Already has a versioned path segment like /v1, /v2, /v1beta, etc.
        if re.search(r'/v\d+(?:[a-z]*)?$', base_url):
            return base_url
        return f"{base_url}/v1"

    def list_models(self):
        """Query the /models endpoint and display available models."""
        base_url = self._resolve_base_url()
        url = f"{base_url}/models"

        import urllib.error
        import urllib.request
        headers = {}
        if self.model_config.get("key"):
            headers["Authorization"] = f"Bearer {self.model_config['key']}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with self.out.spinner(f"Fetching models from {base_url}"):
                with urllib.request.urlopen(req, timeout=30) as resp:
                    result = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()[:500]
            except Exception:
                pass
            self.out.fatal(f"HTTP {e.code}: {e.reason}")
            self._print_api_diagnostics(url)
            if body:
                self.out.kv("response", body)
            return
        except Exception as e:
            self.out.fatal(f"request failed: {e}")
            self._print_api_diagnostics(url)
            return

        models = result.get("data", [])
        if not models:
            self.out.info(f"no models returned from {url}")
            return

        md = f"### Models at {base_url}\n"
        for m in sorted(models, key=lambda x: x.get("id", "")):
            mid = m.get("id", "?")
            owner = m.get("owned_by", "")
            if owner:
                md += f"- **{mid}**  ({owner})\n"
            else:
                md += f"- **{mid}**\n"
        self.out.markdown(md)

    def _print_api_diagnostics(self, url: str):
        """Print url, model, and masked key after an API failure."""
        key_masked = self.model_config["key"][:4] + "****" if self.model_config.get("key") else "(none)"
        self.out.kv("url", url)
        self.out.kv("model", self.model_config.get("model", "(none)"))
        self.out.kv("key", key_masked)
        self.out.info(f"debug with: ac --curlify -m {self.model_config.get('model', '')} '<task>'")

    @staticmethod
    def _curlify(url: str, headers: dict, data: bytes) -> str:
        """Build a curl(1) command equivalent to the given HTTP request.

        Useful for debugging — paste the output into a terminal to
        reproduce the exact request.
        """
        parts = ["curl", url]
        for k, v in headers.items():
            # Mask the Authorization header
            if k.lower() == "authorization" and v.startswith("Bearer "):
                token = v[7:]
                masked = token[:4] + "****" + token[-4:] if len(token) > 8 else "****"
                parts.append(f"-H '{k}: Bearer {masked}'")
            else:
                parts.append(f"-H '{k}: {v}'")
        if data:
            # Escape single quotes for shell safety
            escaped = data.decode().replace("'", "'\\''")
            parts.append(f"-d '{escaped}'")
        return " \\\n  ".join(parts)

    def _call_model(self, messages: list[dict]) -> Optional[str]:
        """Call the configured model via OpenAI-compatible API. Returns response text or None."""

        import urllib.error
        import urllib.request

        base_url = self._resolve_base_url()
        url = f"{base_url}/chat/completions"

        model_name = self.model_config.get("model", "")
        payload = {
            "messages": messages,
            "temperature": 0.1,
        }
        if model_name:
            payload["model"] = model_name
        data = json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json",
        }
        if self.model_config.get("key"):
            headers["Authorization"] = f"Bearer {self.model_config['key']}"
        req = urllib.request.Request(url, data=data, headers=headers)

        # If --curlify was requested, print the curl equivalent and exit cleanly
        if self._curlify_mode:
            self.out.section("curl equivalent")
            print(self._curlify(url, headers, data))
            sys.exit(0)

        try:
            with self.out.spinner("Thinking"):
                with urllib.request.urlopen(req, timeout=60) as resp:
                    result = json.loads(resp.read())
                    return result["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()[:500]
            except Exception:
                pass
            self.out.fatal(f"model call failed: HTTP {e.code} {e.reason}")
            self._print_api_diagnostics(url)
            if body:
                self.out.kv("response", body)
            return None
        except Exception as e:
            self.out.fatal(f"model call failed: {e}")
            self._print_api_diagnostics(url)
            return None

    # ------------------------------------------------------------------
    # Plan creation & validation
    # ------------------------------------------------------------------
    def _create_plan(self, task: str) -> tuple[list[dict], str]:
        """
        Build a step-by-step plan and success condition using the LLM.

        Returns (plan_steps, success_condition) where success_condition
        is a specific, checkable description of what 'done' looks like.
        """
        tools = self.all_tool_names()
        cwd = Path.cwd()

        system_prompt = (
            "You are a task planner for a command-line agent. "
            "Given a user task, available system tools, and directory context, "
            "produce a JSON object with a plan and a success condition.\n\n"
            "The plan is an array of steps, each with:\n"
            '  - "action": a SHORT verb-only description (e.g. "read_cpu", "list_dir", "clone_repo")\n'
            '  - "tool": MUST be one of the available tools listed below. Do NOT invent tool names.\n'
            '  - "args": list of string arguments for that tool\n\n'
            'The success_condition is a string describing what the tool output must contain '
            'for the task to be considered done. Be specific. '
            'Bad: "task completed successfully". '
            'Good: "output contains the CPU model name and total memory in MB".\n\n'
            "Return ONLY valid JSON in this format:\n"
            '{\n'
            '  "plan": [{"action": "...", "tool": "...", "args": [...]}],\n'
            '  "success_condition": "..."\n'
            '}\n\n'
            "IMPORTANT: The 'tool' field MUST be exactly one of the available tools. "
            "Do NOT use the task description as a tool name. Do NOT invent new tool names. "
            "If no available tool can accomplish the task, use the closest one with appropriate args.\n"
            "For example, to read /proc/cpuinfo, use tool 'cat' with args ['/proc/cpuinfo']. "
            "To list directory contents, use tool 'ls' with args ['-la']. "
            "To read the first lines of a file, use tool 'head' with args ['-n', '20', '/path/to/file'].\n\n"
            "If a task changes system state (like creating a file), include a final step "
            "to output the result (e.g. ls or cat) so it can be verified."
        )

        user_prompt = (
            f"Task: {task}\n"
            f"Available tools: {', '.join(sorted(tools))}\n"
            f"Current directory: {cwd}\n"
            f"Files: {', '.join(f.name for f in cwd.iterdir() if f.is_file())[:200]}\n"
            f"Dirs: {', '.join(d.name for d in cwd.iterdir() if d.is_dir())[:200]}"
        )

        result = self._call_model([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ])

        if result is None:
            self.out.fatal("cannot proceed without a working model connection")
            sys.exit(1)

        # Try to extract JSON from the response
        result = result.strip()
        # Strip markdown code fences if present
        if result.startswith("```"):
            result = re.sub(r'^```(?:json)?\n?', '', result)
            result = re.sub(r'\n?```$', '', result)
            result = result.strip()
        try:
            parsed = json.loads(result)
            # Accept either the new object format or legacy array format
            if isinstance(parsed, list):
                plan = parsed
                success_condition = f"Task '{task}' completed successfully"
            elif isinstance(parsed, dict):
                plan = parsed.get("plan", [])
                success_condition = parsed.get("success_condition", f"Task '{task}' completed successfully")
            else:
                raise ValueError("bad response type")

            # Validate plan structure
            if not isinstance(plan, list):
                raise ValueError("plan is not an array")
            for step in plan:
                if not isinstance(step, dict) or "action" not in step:
                    raise ValueError("bad step")
            self.out.success("plan generated by model")
            return plan, success_condition
        except (json.JSONDecodeError, ValueError, KeyError):
            self.out.fatal("model response was not valid JSON — cannot proceed")
            sys.exit(1)


    def _validate_plan(self, plan: list[dict]) -> bool:
        """Validate that every step uses a tool from the available list."""
        available = self.all_tool_names()
        for i, step in enumerate(plan):
            tool = step.get("tool")
            if not tool:
                self.out.fatal(f"step {i + 1} ('{step.get('action', '?')}') has no tool — cannot execute")
                return False

            self.out.section(f"validate  step {i + 1}: {step['action']}")

            if tool in available:
                self.out.success(f"tool '{tool}' available")
            elif self.discover_tool(tool):
                # Known system tool not yet symlinked — offer to add it
                if self.symlink_tool(tool, auto_yes=self.auto_yes):
                    self.out.success(f"tool '{tool}' linked and ready")
                    available.add(tool)  # update for subsequent checks
                else:
                    self.out.fatal(f"cannot proceed without '{tool}'")
                    return False
            else:
                self.out.fatal(f"model returned unknown tool '{tool}' — not in available tools and not found on system")
                return False
        return True

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def _execute_plan(self, plan: list[dict], task: str) -> tuple[bool, str]:
        """Execute each plan step, returning (success, captured_output)."""
        captured = []
        for i, step in enumerate(plan):
            self.out.section(f"execute  step {i + 1}: {step['action']}")
            tool = step.get("tool")
            args = step.get("args", [])

            if tool:
                rc, out, err = self._run_symlinked(tool, args, timeout=300, show_cmd=True)
                if out.strip():
                    for line in out.strip().splitlines():
                        self.out.output(line)
                    captured.append(out.strip())
                if err.strip():
                    for line in err.strip().splitlines():
                        self.out.output(line)
                    captured.append(err.strip())
            else:
                self.out.fatal(f"no tool for step '{step['action']}' — cannot execute")
                return False, ""

        return True, "\n".join(captured)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------
    def execute_task(self, task: str):
        self.out.headline(task)
        self.out.separator()

        # 1. Check for applicable skills
        self.out.section("Skills")
        skill = self._find_applicable_skill(task)
        skill_success_condition = None
        if skill:
            self.out.success(f"match: {skill['name']}  ({skill.get('success_count', 0)}× success)")

            # 2. Apply skill — capture output for verification
            self.out.section("applying skill")
            ok, captured = self.apply_skill(skill)
            if ok:
                # Load the skill's saved success condition for verification
                full_skill = self._load_skill(skill['name'])
                skill_success_condition = full_skill.get("success_condition", "")
                # Backwards compat: old skills stored success_condition as a dict
                if isinstance(skill_success_condition, dict):
                    skill_success_condition = skill_success_condition.get("description", "")
                # Substitute any parameter placeholders
                extracted = skill.get("_extracted_params", {})
                for pname, pval in extracted.items():
                    skill_success_condition = skill_success_condition.replace(f"{{{{{pname}}}}}", pval)

                if skill_success_condition:
                    self.out.section("verify")
                    if self._verify_success(task, skill_success_condition, captured):
                        self.out.separator()
                        self.out.success(f"SUCCESS  {skill_success_condition}")
                        return
                else:
                    # No saved condition — can't verify, assume ok
                    self.out.separator()
                    self.out.success(f"skill completed: {skill['name']}")
                    return
            self.out.warning("skill did not satisfy — falling back to plan")
        else:
            self.out.info("none found")

        # 3. Create plan + success condition via LLM
        self.out.section("plan")
        plan, success_condition = self._create_plan(task)
        tools_in_plan = sorted({s["tool"] for s in plan if s.get("tool")})
        for i, step in enumerate(plan):
            tool = step.get("tool", "(none)")
            self.out.info(f"step {i + 1}: {step['action']}  ({tool})")
        self.out.info(f"success condition: {success_condition}")

        # 4. Validate plan
        self.out.section("validate")
        if not self._validate_plan(plan):
            self.out.fatal("validation failed")
            sys.exit(1)
        self.out.success("all steps valid")

        # 5. Execute — capture output
        self.out.section("execute")
        _, captured = self._execute_plan(plan, task)

        # 6. Verify against success condition & save skill
        self.out.section("verify")
        if self._verify_success(task, success_condition, captured):
            self.out.separator()
            skill_path = self._save_skill(task, plan, success_condition, tools_in_plan, success=True)
            self.out.success(f"SUCCESS  {success_condition}")
            if skill_path:
                self.out.success(f"skill saved → {Path(skill_path).name}")
        else:
            self._save_skill(task, plan, success_condition, tools_in_plan, success=False)
            self.out.separator()
            self.out.fatal(f"FAILED  {success_condition}")
            sys.exit(1)

    def show_status(self):
        tools = self.get_all_symlinked_tools()
        md = "# agent-cli\n\n"
        md += "### Tools\n"
        for task, names in sorted(tools.items()):
            md += f"- **{task}/bin/** {'  '.join(names)}\n"
        md += "\n"
        md += self._skills_markdown()
        md += "\n---\n\n"
        md += "* `ac '<task>'`\n* `ac --skills [name]`\n* `ac -d <skill>`\n* `ac -s model 'model-name'`\n"
        self.out.markdown(md)

    def _skills_markdown(self) -> str:
        """Build a markdown skills section string."""
        skills = self.get_available_skills()
        md = "### Skills\n"
        if not skills:
            md += "*none yet — they are saved automatically on success*\n"
            return md
        for s in skills:
            tools_str = f" [`{'`, `'.join(_md_escape(t) for t in s['tools_used'])}`]" if s.get("tools_used") else ""
            md += f"- **{s['name']}**  {s['success_count']}×{tools_str}\n"
            if s.get("description"):
                md += f"  {s['description']}\n"
        return md

    def _print_skills(self):
        """Print a formatted list of skills."""
        self.out.markdown(self._skills_markdown())

    def _print_skill_detail(self, skill_name: str):
        """Print raw skill data for debugging — no formatting."""
        skill_dir = self._find_skill_dir(skill_name)
        if not skill_dir:
            self.out.fatal(f"skill '{skill_name}' not found")
            return

        # SKILL.md
        skill_md_path = skill_dir / "SKILL.md"
        if skill_md_path.exists():
            print(skill_md_path.read_text())

        # plan.json
        plan_path = skill_dir / "plan.json"
        if plan_path.exists():
            with open(plan_path) as f:
                print(json.dumps(json.load(f), indent=2))


def main():
    parser = argparse.ArgumentParser(
        description="agent-cli: A one-shot command-line helper "
                    "with scoped execution tasks and observable tool symlinks."
    )
    parser.add_argument("task", nargs="?", help="Task description to execute")
    parser.add_argument(
        "-s", "--set", nargs="*", metavar=("KEY", "VALUE"),
        help="Set a model config value (model, base_url, key), or show current values with no args",
    )
    parser.add_argument("-m", "--model", nargs="?", const="__list__", help="Override model for this run; with no value, list available models")
    parser.add_argument("-b", "--base-url", help="Override base_url for this run")
    parser.add_argument("-k", "--key", help="Override API key for this run")
    parser.add_argument(
        "-c", "--config-dir", type=Path, default=DEFAULT_CONFIG_DIR,
        help="Config directory (default: .local/agent-cli)",
    )
    parser.add_argument(
        "-y", "--yes", action="store_true",
        help="Auto-approve tool symlinks (non-interactive mode)",
    )
    parser.add_argument(
        "-l", "--skills", nargs="?", const="__all__", metavar="SKILL",
        help="List skills, or show detail for a specific skill",
    )
    parser.add_argument(
        "-d", "--delete", metavar="SKILL",
        help="Delete a skill so it won't be used and must be re-learned",
    )
    parser.add_argument(
        "--curlify", action="store_true",
        help="Print the curl equivalent of the model API call for debugging",
    )

    args = parser.parse_args()
    agent = AgentCLI(config_dir=args.config_dir, auto_yes=args.yes)
    agent._curlify_mode = args.curlify

    # --set
    if args.set is not None:
        if len(args.set) == 0:
            # Show current config values
            agent.show_model_config()
        elif len(args.set) == 2:
            agent.set_model_config(args.set[0], args.set[1])
            print(f"Set {args.set[0]} = {args.set[1]}")
        else:
            print("Usage: ac -s [KEY VALUE]")
            print("  (no args)  show current values")
            print("  KEY VALUE  set a value (model, base_url, key)")
        return

    # one-shot overrides
    if args.model:
        if args.model == "__list__":
            agent.list_models()
            return
        agent.model_config["model"] = args.model
    if args.base_url:
        agent.model_config["base_url"] = args.base_url
    if args.key:
        agent.model_config["key"] = args.key

    if args.task:
        agent.execute_task(args.task)
    elif args.skills:
        if args.skills == "__all__":
            agent._print_skills()
        else:
            agent._print_skill_detail(args.skills)
    elif args.delete:
        agent.delete_skill(args.delete)
    else:
        agent.show_status()


if __name__ == "__main__":
    main()
