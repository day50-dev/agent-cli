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


# ANSI escape codes — module-level so Output and Spinner share them.
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
# Output primitives — semantic abstractions for CLI display
# ------------------------------------------------------------------

class Output:
    """Semantic output layer for CLI display.

    Instead of scattering marker constants and raw print() calls everywhere,
    code expresses intent through semantic primitives like headline(),
    section(), success(), fatal(), etc.  This centralises styling,
    ensures visual consistency, and makes it trivial to retarget output
    (e.g. JSON, logfmt, no-color mode) later.
    """

    def __init__(self, no_color: bool = False):
        self._no_color = no_color or not sys.stdout.isatty()

    def _c(self, code: str, text: str) -> str:
        """Wrap *text* in ANSI *code* unless colour is disabled."""
        if self._no_color:
            return text
        return f"{code}{text}{_ANSI_RESET}"

    # ---- semantic primitives ----

    def headline(self, text: str) -> None:
        """Top-level task title — the main thing happening."""
        marker = self._c(_ANSI_BOLD, "▸▸▸")
        print(f"{marker} {self._c(_ANSI_BOLD, text)}")

    def section(self, text: str) -> None:
        """Sub-step / phase header — groups related output."""
        marker = self._c(_ANSI_CYAN, "▹")
        print(f"\n{marker} {self._c(_ANSI_BOLD, text)}")

    def success(self, text: str) -> None:
        """Positive outcome: approved, completed, confirmed."""
        marker = self._c(_ANSI_GREEN, "✓")
        print(f"  {marker} {text}")

    def warning(self, text: str) -> None:
        """Non-critical issue: degraded path, fallback, something to note."""
        marker = self._c(_ANSI_YELLOW, "⚠")
        print(f"  {marker} {self._c(_ANSI_YELLOW, text)}")

    def fatal(self, text: str) -> None:
        """Critical failure: cannot proceed."""
        marker = self._c(_ANSI_RED, "✗")
        print(f"\n  {marker} {self._c(_ANSI_RED, text)}")

    def info(self, text: str) -> None:
        """Neutral informational note."""
        marker = self._c(_ANSI_DIM, "·")
        print(f"  {marker} {self._c(_ANSI_DIM, text)}")

    def command(self, text: str) -> None:
        """A command about to be executed."""
        marker = self._c(_ANSI_BLUE, "➜")
        print(f"\n  {marker} {self._c(_ANSI_BOLD, text)}")

    def output(self, text: str) -> None:
        """Captured output line from a tool."""
        print(f"  │ {self._c(_ANSI_DIM, text)}")

    def prompt(self, text: str, end: str = "\n") -> None:
        """User interaction prompt."""
        marker = self._c(_ANSI_YELLOW, "?")
        print(f"\n  {marker} {text}", end=end)

    def separator(self) -> None:
        """Horizontal rule separating major sections."""
        line = "─" * 60
        print(self._c(_ANSI_DIM, line))

    # ---- compound helpers ----

    def kv(self, key: str, value: str) -> None:
        """Key-value pair in a detail view (left-aligned label + value)."""
        label = self._c(_ANSI_DIM, f"{key}:")
        print(f"  · {label:14s} {value}")

    # ---- spinner ----

    def spinner(self, label: str = "Thinking"):
        """Return a Spinner context manager animating while waiting."""
        return Spinner(label, no_color=self._no_color)


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
        self.custom_tools_dir = self.tools_dir / "custom" / "bin"
        self.config_file = self.config_dir / "config.json"

        self.model_config = {
            "base_url": None,
            "model": None,
            "key": None,
        }

        self.out = Output()
        self._skills_migrated = False

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
        self.out.section("model config")
        for key, value in self.model_config.items():
            if key == "key" and value:
                # Mask the API key for security
                masked = value[:4] + "****" + value[-4:] if len(value) > 8 else "****"
                self.out.kv(key, masked)
            else:
                self.out.kv(key, value or "(not set)")

    # ------------------------------------------------------------------
    # Directory & symlink setup
    # ------------------------------------------------------------------
    def _setup_directories(self):
        self.tools_dir.mkdir(parents=True, exist_ok=True)
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.custom_tools_dir.mkdir(parents=True, exist_ok=True)

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
                f"## Parameters\n\n"
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
                "task_pattern": plan_data.get("task_pattern", ""),
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
                # Fallback: try legacy task_pattern substring matching
                pattern = skill.get("task_pattern", "").lower()
                if pattern and (pattern in task.lower() or task.lower() in pattern):
                    return skill
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
        """
        for step in plan:
            action = step.get("action", "")
            if action:
                return action.replace("_", "-").lower()
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

    def _parameterize_success_condition(self, condition: dict, params_map: dict[str, str]) -> dict:
        """Replace concrete values in the success condition with {{param}} placeholders.

        Only replaces values that appear in params_map (i.e., were also
        parameterized in the plan), so the condition stays consistent
        with the parameterized plan at apply-time.
        """
        param_condition = {}
        for key, value in condition.items():
            if isinstance(value, str):
                # Replace any known parameter value with its placeholder
                new_val = value
                for orig, param_name in params_map.items():
                    new_val = new_val.replace(orig, f"{{{{{param_name}}}}}")
                param_condition[key] = new_val
            elif isinstance(value, list):
                param_condition[key] = [
                    self._parameterize_success_condition_value(item, params_map)
                    for item in value
                ]
            else:
                param_condition[key] = value
        return param_condition

    def _parameterize_success_condition_value(self, value, params_map: dict[str, str]):
        """Replace parameter values inside a single success-condition value."""
        if isinstance(value, str):
            new_val = value
            for orig, param_name in params_map.items():
                new_val = new_val.replace(orig, f"{{{{{param_name}}}}}")
            return new_val
        if isinstance(value, list):
            return [self._parameterize_success_condition_value(v, params_map) for v in value]
        if isinstance(value, dict):
            return self._parameterize_success_condition(value, params_map)
        return value

    def _save_skill(self, task: str, plan: list[dict], success_condition: dict,
                    tools_used: list[str], success: bool = True) -> Optional[str]:
        """Persist the completed task as a reusable, parameterized skill.

        Skills are stored in Anthropic SKILL.md directory format:
          skills/<name>/SKILL.md  — frontmatter + instructions
          skills/<name>/plan.json  — machine-readable plan data
        """
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
        actions = [s.get("action", "?") for s in param_plan]
        desc = f"Auto-learned skill: {', '.join(actions)}"

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
                "\n## Parameters\n\n"
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
            "task_pattern": task.lower(),  # legacy compat
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

    def apply_skill(self, skill_meta: dict) -> bool:
        """Execute a saved skill's plan with parameter substitution.

        Extracts parameter values from the current task (stored in
        skill_meta["_extracted_params"] by _find_applicable_skill), substitutes
        them into the parameterized plan, then executes.

        Returns True if all steps succeeded.
        """
        # Load full skill data from file
        full = self._load_skill(skill_meta["name"])
        if not full:
            self.out.info(f"skill file not found for '{skill_meta['name']}'")
            return False

        plan = full.get("plan", [])
        if not plan:
            self.out.info("skill has no saved plan")
            return False

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
            return False

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
                        return False
                else:
                    self.out.fatal(f"tool '{tool}' not available on this system")
                    return False

        # Execute
        for i, step in enumerate(resolved_plan):
            self.out.section(f"skill step {i + 1}: {step['action']}")
            tool = step.get("tool")
            args = step.get("args", [])

            if tool:
                rc, out, err = self._run_symlinked(tool, args, timeout=300, show_cmd=True)
                if out.strip():
                    for line in out.strip().splitlines():
                        self.out.output(line)
                if err.strip():
                    for line in err.strip().splitlines():
                        self.out.output(line)
            else:
                self.out.info("no tool for this step")

        return True

    # ------------------------------------------------------------------
    # Success condition
    # ------------------------------------------------------------------
    def _define_success_condition(self, task: str) -> dict:
        """
        Look at the current directory context and produce a concrete,
        checkable success condition for the given task.
        """
        cwd = Path.cwd()
        files = [f.name for f in cwd.iterdir() if f.is_file()]
        dirs = [d.name for d in cwd.iterdir() if d.is_dir()]

        condition = {
            "cwd": str(cwd),
            "files_before": files,
            "dirs_before": dirs,
            "description": "",
        }

        tl = task.lower()

        # Clone / repository tasks — handle multiple repos
        if any(kw in tl for kw in ("clone", "repo", "repository", "git clone")):
            expected_dirs = []
            # Find "as <name>" patterns
            as_matches = re.findall(r'\bas\s+(\S+)', tl)
            for am in as_matches:
                expected_dirs.append(am.replace(".git", ""))

            # Only use "as" names if present; otherwise fall back to repo names
            if not expected_dirs:
                repo_matches = re.findall(r'github\.com/[\w.-]+/([\w.-]+)', tl)
                for rm in repo_matches:
                    expected_dirs.append(rm.replace(".git", ""))

            if expected_dirs:
                dir_list = "', '".join(expected_dirs)
                condition["description"] = f"Directories '{dir_list}' exist in {cwd}"
                condition["expect_dirs"] = expected_dirs
                condition["type"] = "dirs_exist"
            else:
                condition["description"] = f"Repository directory exists in {cwd}"
                condition["type"] = "dir_exists"

        # File creation / write tasks
        elif any(kw in tl for kw in ("create", "write", "generate", "new file")):
            condition["description"] = f"New file(s) created in {cwd}"
            condition["type"] = "new_file"

        # Generic
        else:
            condition["description"] = f"Task '{task}' completed successfully"
            condition["type"] = "generic"

        return condition

    def _check_success(self, condition: dict) -> bool:
        cwd = Path.cwd()
        now_files = {f.name for f in cwd.iterdir() if f.is_file()}
        now_dirs = {d.name for d in cwd.iterdir() if d.is_dir()}

        ctype = condition.get("type", "generic")

        if ctype == "dirs_exist":
            targets = condition.get("expect_dirs", [])
            return all((cwd / d).is_dir() for d in targets)

        if ctype == "dir_exists":
            target = condition.get("expect_dir", "")
            return (cwd / target).is_dir()

        if ctype == "new_file":
            before = set(condition.get("files_before", []))
            return len(now_files - before) > 0

        # Generic: always return True (we trust execution)
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
        if not self.model_config.get("key"):
            self.out.fatal("no API key configured — run: ac -s key <your-key>")
            return

        base_url = self._resolve_base_url()
        url = f"{base_url}/models"

        import urllib.error
        import urllib.request
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.model_config['key']}",
            },
        )
        try:
            with self.out.spinner("Fetching models"):
                with urllib.request.urlopen(req, timeout=30) as resp:
                    result = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            self.out.fatal(f"HTTP {e.code}: {e.reason}  ({url})")
            return
        except Exception as e:
            self.out.fatal(f"request failed: {e}")
            return

        models = result.get("data", [])
        if not models:
            self.out.info(f"no models returned from {url}")
            return

        self.out.success(f"models available at {base_url}:")
        for m in sorted(models, key=lambda x: x.get("id", "")):
            mid = m.get("id", "?")
            owner = m.get("owned_by", "")
            line = mid
            if owner:
                line += f"  ({owner})"
            self.out.info(line)

    def _call_model(self, messages: list[dict]) -> Optional[str]:
        """Call the configured model via OpenAI-compatible API. Returns response text or None."""
        if not self.model_config.get("model") or not self.model_config.get("key"):
            return None

        base_url = self._resolve_base_url()
        url = f"{base_url}/chat/completions"
        import urllib.request

        payload = {
            "model": self.model_config["model"],
            "messages": messages,
            "temperature": 0.1,
        }
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.model_config['key']}",
            },
        )
        try:
            with self.out.spinner("Thinking"):
                with urllib.request.urlopen(req, timeout=60) as resp:
                    result = json.loads(resp.read())
                    return result["choices"][0]["message"]["content"]
        except Exception as e:
            self.out.fatal(f"model call failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Plan creation & validation
    # ------------------------------------------------------------------
    def _create_plan(self, task: str) -> list[dict]:
        """
        Build a step-by-step plan using the LLM when configured,
        otherwise fall back to heuristic parsing.

        The agent has two choices at each step:
        a) Use an existing symlinked tool, or
        b) Write a custom program (stored in tools/custom/bin).
        """
        tools = self.all_tool_names()
        cwd = Path.cwd()

        system_prompt = (
            "You are a task planner for a command-line agent. "
            "Given a user task, available system tools, and directory context, "
            "produce a JSON array of plan steps. Each step is an object with:\n"
            '  - "action": short description string\n'
            '  - "tool": tool name (from the available list), or null to write a custom program\n'
            '  - "args": list of string arguments for that tool\n'
            "Return ONLY valid JSON, no markdown, no explanation.\n"
            "If the task involves cloning repos, create one step per repo. "
            "If a directory name is specified (e.g., 'as dir_name'), use it as the last argument."
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

        if result:
            # Try to extract JSON from the response
            result = result.strip()
            # Strip markdown code fences if present
            if result.startswith("```"):
                result = re.sub(r'^```(?:json)?\n?', '', result)
                result = re.sub(r'\n?```$', '', result)
                result = result.strip()
            try:
                plan = json.loads(result)
                if isinstance(plan, list):
                    # Validate structure
                    for step in plan:
                        if not isinstance(step, dict) or "action" not in step:
                            raise ValueError("bad step")
                    self.out.success("plan generated by model")
                    return plan
            except (json.JSONDecodeError, ValueError, KeyError):
                self.out.warning("model response unparsable — falling back to heuristics")

        # --- heuristic fallback ---
        self.out.warning("using heuristic plan (no model configured)")
        return self._heuristic_plan(task)

    def _heuristic_plan(self, task: str) -> list[dict]:
        """Regex-based plan creation as fallback when no LLM is available."""
        task_lower = task.lower()
        plan = []

        # --- clone / repo tasks: support multiple repos ---
        if any(kw in task_lower for kw in ("clone", "repo", "repository")):
            # Find all github.com/... patterns, each possibly with "as name"
            segments = re.split(r'\b(clone|and)\b', task_lower)
            for seg in segments:
                seg = seg.strip()
                if not seg:
                    continue
                # Extract repo URL
                url = None
                url_match = re.search(
                    r'(https?://[\w.-]+/[\w.-]+/[\w.-]+?)(?:\.git)?\b', seg,
                )
                if url_match:
                    url = url_match.group(1)
                else:
                    gh = re.search(r'github\.com/([\w.-]+/[\w.-]+)', seg)
                    if gh:
                        url = f"https://github.com/{gh.group(1)}"
                    else:
                        for host in ("gitlab.com", "bitbucket.org"):
                            m = re.search(rf'{host}/([\w.-]+/[\w.-]+)', seg)
                            if m:
                                url = f"https://{host}/{m.group(1)}"
                                break

                if not url:
                    continue

                # Check for "as <dir_name>"
                args = ["clone", url]
                as_match = re.search(r'\bas\s+(\S+)', seg)
                if as_match:
                    args.append(as_match.group(1))

                plan.append({"action": "clone_repository", "tool": "git", "args": args})

            if not plan:
                plan.append({"action": "clone_repository", "tool": "git", "args": ["clone", task_lower.split()[-1]]})
            return plan

        # --- file listing ---
        if any(kw in task_lower for kw in ("list", "show files", "what files")):
            plan.append({"action": "list_directory", "tool": "ls", "args": ["-la"]})
            return plan

        # --- read / cat ---
        if any(kw in task_lower for kw in ("read", "cat ", "show content")):
            parts = task_lower.split()
            target = parts[-1] if parts else "."
            plan.append({"action": "read_file", "tool": "cat", "args": [target]})
            return plan

        # --- help / docs ---
        if any(kw in task_lower for kw in ("help", "docs", "man", "documentation")):
            parts = task_lower.split()
            topic = parts[-1] if parts else ""
            plan.append({"action": "read_manual", "tool": "man", "args": [topic]})
            return plan

        # --- generic ---
        plan.append({"action": "execute_task", "tool": None, "write_program": True})
        return plan

    def _validate_plan(self, plan: list[dict]) -> bool:
        """Walk through each step, check tool availability, ask to install missing."""
        for i, step in enumerate(plan):
            tool = step.get("tool")
            if not tool:
                continue  # write_program path — always valid

            self.out.section(f"validate  step {i + 1}: {step['action']}")

            if tool in self.all_tool_names():
                self.out.success(f"tool '{tool}' already available")
            else:
                # Tool not symlinked — try to discover it
                if self.discover_tool(tool):
                    if self.symlink_tool(tool, auto_yes=self.auto_yes):
                        self.out.success(f"tool '{tool}' linked and ready")
                    else:
                        self.out.fatal(f"cannot proceed without '{tool}'")
                        return False
                else:
                    self.out.fatal(f"'{tool}' not found on this system")
                    self.out.prompt(f"Install '{tool}' and retry? (y/n): ", end="")
                    resp = sys.stdin.readline().strip().lower()
                    if resp == "y":
                        self.out.info(f"please install '{tool}' then re-run the task")
                    return False
        return True

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def _write_custom_program(self, task: str) -> Optional[str]:
        """
        Write a small Python script into tools/custom/bin that attempts
        to accomplish the task.  Returns the script path or None.
        """
        self.custom_tools_dir.mkdir(parents=True, exist_ok=True)
        script_name = re.sub(r'[^a-z0-9]+', '_', task.lower().strip())[:40]
        script_path = self.custom_tools_dir / f"{script_name}.py"

        body = f'''#!/usr/bin/env python3
"""Auto-generated helper for: {task}"""
import subprocess, sys, os
from pathlib import Path

def main():
    # TODO: implement logic for "{task}"
    print("Executing: {task}")

if __name__ == "__main__":
    main()
'''
        script_path.write_text(body)
        script_path.chmod(0o755)
        # Symlink into our tool list
        link = self.custom_tools_dir / script_name
        if not link.exists():
            link.symlink_to(script_path)
        return str(script_path)

    def _execute_plan(self, plan: list[dict], task: str) -> bool:
        for i, step in enumerate(plan):
            self.out.section(f"execute  step {i + 1}: {step['action']}")
            tool = step.get("tool")
            args = step.get("args", [])

            if tool:
                rc, out, err = self._run_symlinked(tool, args, timeout=300, show_cmd=True)
                if out.strip():
                    for line in out.strip().splitlines():
                        self.out.output(line)
                if err.strip():
                    for line in err.strip().splitlines():
                        self.out.output(line)
            elif step.get("write_program"):
                script = self._write_custom_program(task)
                if script:
                    self.out.success(f"custom program written to {script}")
                    self.out.info("review and run manually")
                else:
                    self.out.fatal("failed to write custom program")
                    return False
            else:
                self.out.info("no specific tool — executing task directly")

        return True

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------
    def execute_task(self, task: str):
        self.out.headline(task)
        self.out.separator()

        # 1. Define success condition
        self.out.section("success condition")
        success = self._define_success_condition(task)
        self.out.info(success['description'])

        # 2. Check for applicable skills
        self.out.section("skills")
        skill = self._find_applicable_skill(task)
        if skill:
            self.out.success(f"match: {skill['name']}  ({skill.get('success_count', 0)}× success)")

            # 3. Apply skill
            self.out.section("applying skill")
            if self.apply_skill(skill):
                if self._check_success(success):
                    self.out.separator()
                    self.out.success(f"SUCCESS  {success['description']}")
                    return
            self.out.warning("skill did not satisfy — falling back to plan")
        else:
            self.out.info("none found")

        # 4. Create plan
        self.out.section("plan")
        plan = self._create_plan(task)
        tools_in_plan = sorted({s["tool"] for s in plan if s.get("tool")})
        for i, step in enumerate(plan):
            tool = step.get("tool", "custom")
            self.out.info(f"step {i + 1}: {step['action']}  ({tool})")

        # 5. Validate plan
        self.out.section("validate")
        if not self._validate_plan(plan):
            self.out.fatal("validation failed")
            sys.exit(1)
        self.out.success("all steps valid")

        # 6. Execute
        self.out.section("execute")
        self._execute_plan(plan, task)

        # Verify & save skill
        self.out.separator()
        if self._check_success(success):
            skill_path = self._save_skill(task, plan, success, tools_in_plan, success=True)
            self.out.success(f"SUCCESS  {success['description']}")
            if skill_path:
                self.out.success(f"skill saved → {Path(skill_path).name}")
        else:
            self._save_skill(task, plan, success, tools_in_plan, success=False)
            self.out.fatal(f"FAILED  {success['description']}")
            sys.exit(1)

    def show_status(self):
        self.out.headline("agent-cli")
        self.out.separator()

        tools = self.get_all_symlinked_tools()
        self.out.section("tools")
        for task, names in sorted(tools.items()):
            self.out.info(f"{task}/bin/  {' '.join(names)}")

        self._print_skills()

        self.out.separator()
        self.out.info("ac '<task>'")
        self.out.info("ac --skills [name]")
        self.out.info("ac -d <skill>  (delete)")
        self.out.info("ac -s model 'model-name'")

    def _print_skills(self):
        """Print a formatted list of skills."""
        skills = self.get_available_skills()
        self.out.section("skills")
        if not skills:
            self.out.info("none yet — they are saved automatically on success")
            return
        for s in skills:
            status = f"{s['success_count']}×"
            tools_str = f" [{', '.join(s['tools_used'])}]" if s.get("tools_used") else ""
            self.out.info(f"{s['name']:30s}  {status:12s}{tools_str}")
            if s.get("description"):
                self.out.info(s['description'])

    def _print_skill_detail(self, skill_name: str):
        """Print detailed information about a single skill."""
        skill_dir = self._find_skill_dir(skill_name)
        if not skill_dir:
            self.out.fatal(f"skill '{skill_name}' not found")
            return

        data = self._load_skill(skill_dir.name)
        if not data:
            self.out.fatal(f"skill '{skill_name}' has no data")
            return

        self.out.headline(data.get('name', skill_name))
        self.out.separator()

        # Directory path
        self.out.kv("dir", str(skill_dir))

        # Show SKILL.md body (instructions)
        skill_md_path = skill_dir / "SKILL.md"
        if skill_md_path.exists():
            text = skill_md_path.read_text()
            # Strip frontmatter, show body only
            body = re.sub(r'^---\s*\n.*?\n---\s*\n', '', text, flags=re.DOTALL).strip()
            if body:
                self.out.section("SKILL.md")
                for line in body.splitlines()[:20]:
                    self.out.info(line)

        # Overview from plan.json
        desc = data.get("description", "")
        if desc:
            self.out.kv("description", desc)
        self.out.kv("success_count", f"{data.get('success_count', 0)}×")
        tools = data.get("tools_used", [])
        if tools:
            self.out.kv("tools", ', '.join(tools))

        # Pattern / regex
        regex = data.get("task_regex", "")
        pattern = data.get("task_pattern", "")
        self.out.section("matching")
        if regex:
            self.out.kv("task_regex", regex)
        if pattern:
            self.out.kv("task_pattern", pattern)

        # Parameters
        params = data.get("params_map", {})
        if params:
            self.out.section("parameters")
            for orig, pname in params.items():
                self.out.kv(pname, orig)

        # Plan
        plan = data.get("plan", [])
        if plan:
            self.out.section("plan")
            for i, step in enumerate(plan):
                tool = step.get("tool", "custom")
                args = step.get("args", [])
                args_str = ' '.join(args) if args else ''
                self.out.info(f"step {i + 1}: {step.get('action', '?')}  ({tool})  {args_str}")

        # Success condition
        condition = data.get("success_condition", {})
        if condition:
            self.out.section("success condition")
            cond_desc = condition.get("description", "")
            if cond_desc:
                self.out.info(cond_desc)
            ctype = condition.get("type", "")
            if ctype:
                self.out.kv("type", ctype)
            for key in ("expect_dirs", "expect_dir"):
                if key in condition:
                    self.out.kv(key, str(condition[key]))


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
    parser.add_argument("--base-url", help="Override base_url for this run")
    parser.add_argument("--key", help="Override API key for this run")
    parser.add_argument(
        "-c", "--config-dir", type=Path, default=DEFAULT_CONFIG_DIR,
        help="Config directory (default: .local/agent-cli)",
    )
    parser.add_argument(
        "-y", "--yes", action="store_true",
        help="Auto-approve tool symlinks (non-interactive mode)",
    )
    parser.add_argument(
        "--skills", nargs="?", const="__all__", metavar="SKILL",
        help="List skills, or show detail for a specific skill",
    )
    parser.add_argument(
        "-d", "--delete", metavar="SKILL",
        help="Delete a skill so it won't be used and must be re-learned",
    )

    args = parser.parse_args()
    agent = AgentCLI(config_dir=args.config_dir, auto_yes=args.yes)

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
