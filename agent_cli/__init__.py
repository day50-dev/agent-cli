#!/usr/bin/env python3
# -- coding: utf-8 --
"""
maxac: A one-shot command-line helper with scoped execution tasks.

The agent starts with a minimal tool set and uses those tools (whatis, apropos,
man, pydoc, cat, head, tail, ls) to discover what else is available on the system.
Tools are always observable symlinks in .config/maxac/tools/{task}/bin.

Agent flow:
1. Analyze context and define success condition
2. Check for applicable skills
3. Apply skill if found, verify against success condition
4. Create a plan using current tools
5. Step through the plan assessing each step before execution
6. If more tools needed, ask user to allow/install them; otherwise execute
"""

import time
start=time.time()
import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import site
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Dict, Any, List
import yaml

# Configure structured logging for audit/diagnostic trail
def _setup_logging(log_path: Optional[Path] = None) -> logging.Logger:
    """Configure structured logging for diagnostic audit trail - logs go to file only."""
    logger = logging.getLogger("maxac")
    logger.setLevel(logging.DEBUG)
    
    # Clear any existing handlers
    logger.handlers.clear()
    
    # Disable propagate to root - prevent duplicate output
    logger.propagate = False
    
    # File handler for audit trail ONLY - no console output
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Create dated log filename: last_run-2026-04-23.log
        dated_name = f"{log_path.stem}-{time.strftime('%Y-%m-%d')}{log_path.suffix}"
        dated_path = log_path.parent / dated_name
        
        # If there's a previous run without today's date, rename it
        if log_path.exists() and log_path != dated_path:
            old_name = f"{log_path.stem}-old{log_path.suffix}"
            old_path = log_path.parent / old_name
            if old_path.exists():
                old_path.unlink()  # Remove any stale old file
            log_path.rename(old_path)
        
        file_handler = logging.FileHandler(dated_path, mode='w')
        file_handler.setLevel(logging.DEBUG)
        # Simple default format - easy to scan
        file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
        logger.addHandler(file_handler)
    
    return logger


# Default configuration - lives under site.USER_BASE (~/.local on Linux,
# ~/Library/Python/<ver> on macOS) so it's always in a stable user location,
# not relative to the current working directory.
DEFAULT_CONFIG_DIR = Path(site.USER_BASE) / "maxac"
DEFAULT_TOOLS_DIR = DEFAULT_CONFIG_DIR / "tools"
DEFAULT_SKILLS_DIR = DEFAULT_CONFIG_DIR / "skills"
DEFAULT_TASKS_DIR = DEFAULT_CONFIG_DIR / "tasks"
DEFAULT_LOGS_DIR = DEFAULT_CONFIG_DIR / "logs"
DEFAULT_CONFIG_FILE = DEFAULT_CONFIG_DIR / "config.json"

# Minimal default tools by task category
DEFAULT_TOOLS = {
    "doc": ["whatis", "apropos", "man", "pydoc"],
    "find": ["cat", "head", "tail", "ls"],
}


# ------------------------------------------------------------------
# Markdown helpers
# ------------------------------------------------------------------

def _md_escape(text: str) -> str:
    """Escape backtick characters for safe use inside markdown code spans."""
    if text is None:
        return ""
    return text.replace('`', r'\`')


# ------------------------------------------------------------------
# Output primitives - semantic abstractions for CLI display
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

    Log levels control verbosity during task execution:
      WARN  - only the task, result summary, and outcome (default)
      INFO  - plus section headers, step descriptions, and notes  (-v)
      DEBUG - plus tool stdout/stderr, commands, subsections       (-vv)
    """

    # Log levels - higher = more verbose
    WARN = 0
    INFO = 1
    DEBUG = 2

    def __init__(self, level: int = 0, log_path: Optional[Path] = None):
        self.level = level
        self._sd = None
        self._log_buffer = []
        self.log_path = log_path
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

    def _log(self, text: str) -> None:
        """Append text to the internal log buffer."""
        self._log_buffer.append(text)

    def _render(self, text: str) -> None:
        """Render a markdown fragment through streamdown, or fall back to print."""
        self._log(text) # Always log the text
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
        """Top-level task title - shown at INFO level and above."""
        if self.level >= self.INFO:
            self._render(f"# {text}\n\n")

    def section(self, text: str) -> None:
        """Sub-step / phase header - shown at INFO level and above."""
        if self.level >= self.INFO:
            self._render(f"\n### {text}\n")

    def subsection(self, text: str) -> None:
        """Sub-step detail - only shown at DEBUG level."""
        if self.level >= self.DEBUG:
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
        """Neutral informational note - shown at INFO level and above."""
        if self.level >= self.INFO:
            self._render(f"* {text}\n")

    def command(self, text: str) -> None:
        """A command about to be executed - only shown at DEBUG level."""
        if self.level >= self.DEBUG:
            self._render(f"- {_md_escape(text)}\n")

    def output(self, text: str) -> None:
        """Captured output line from a tool - only shown at DEBUG level."""
        if self.level >= self.DEBUG:
            self._render(f"> {_md_escape(text)}\n")

    def prompt(self, text: str, end: str = "\n") -> None:
        """User interaction prompt."""
        # Interactive prompts need direct stdout - don't render as markdown.
        self._log(text) # Log prompt text as well
        print(f"\n  {text}", end=end)

    def separator(self) -> None:
        """Horizontal rule separating major sections - shown at INFO level and above."""
        if self.level >= self.INFO:
            self._render("---\n\n")

    # ---- compound helpers ----

    def result(self, text: str) -> None:
        """Final result / answer to the users task - always shown, no prefix marker."""
        self._log(text) # Always log result text
        self._render(f"{text}\n")

    def kv(self, key: str, value: str) -> None:
        """Key-value pair in a detail view (left-aligned label + value)."""
        self._log(f"{key}: {value}\n") # Always log KV pairs
        self._render(f"* {key}: `{_md_escape(value)}`\n")

    def sublist(self, text: str) -> None:
        """Sub-list item (indented under a parent list item)."""
        self._log(f"  - {text}\n")
        self._render(f"  - {text}\n")

    # ---- markdown rendering (streamdown) ----

    def markdown(self, text: str) -> None:
        """Render a bulk markdown document through streamdown."""
        self._log(text) # Always log markdown text
        self._render(text)

    # ---- spinner ----

    def spinner(self, label: str = "Thinking"):
        """Return a Spinner context manager animating while waiting."""
        return Spinner(label, no_color=not sys.stdout.isatty())

    def get_log_content(self) -> str:
        """Return the entire captured log content."""
        return "".join(self._log_buffer)

    def save_log(self) -> Optional[Path]:
        """Save the captured log content to a file if a log_path is set."""
        if self.log_path and self._log_buffer:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.log_path, "w") as f:
                    f.write("".join(self._log_buffer))
                return self.log_path
            except Exception as e:
                self.fatal(f"Failed to save log to {self.log_path}: {e}")
        return None


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

    # ---- colour helpers (no-op - no ANSI codes) ----

    def _c(self, code: str, text: str) -> str:
        return text

    def _spin(self):
        idx = 0
        while not self._stop.is_set():
            frame = self._FRAMES[idx % len(self._FRAMES)]
            sys.stdout.write(f"\r  {frame} {self._label}…\033[?25l")
            sys.stdout.flush()
            self._stop.wait(self._INTERVAL)
            idx += 1

    # ---- context-manager protocol ----

    def __enter__(self):
        if self._no_tty:
            # Complete no-op when stdout is not a TTY - avoids garbled piped output
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
    """Main maxac class."""

    def __init__(self, config_dir: Optional[Path] = None, auto_yes: bool = False,
                 verbose: int = 0, mcp_file: Optional[Path] = None):
        self.config_dir = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
        self.auto_yes = auto_yes
        self.tools_dir = self.config_dir / "tools"
        self.skills_dir = self.config_dir / "skills"
        self.tasks_dir = self.config_dir / "tasks"
        self.config_file = self.config_dir / "config.json"
        self.mcp_file = mcp_file or (self.config_dir / "mcp_servers.json")

        self.model_config = {
            "url": None,
            "model": None,
            "key": None,
        }

        # Initialize structured logging for audit trail
        self.log = _setup_logging(self.config_dir / "last_run.log")
        self.log.info("=" * 60)
        self.log.info("MAXAC STARTED | config_dir=%s", self.config_dir)
        self.log.info("=" * 60)

        self.out = Output(level=Output.DEBUG if verbose >= 2 else (Output.INFO if verbose >= 1 else Output.WARN),
                          log_path=self.config_dir / "last_run.log") # Log to a file in config dir
        self._skills_migrated = False
        self._curlify_mode = False
        self._timeout = 120

        self.mcp_sessions: Dict[str, Any] = {} # server_name -> session
        self.mcp_tools: Dict[str, Any] = {}    # tool_name -> (server_name, tool_info)
        self._mcp_exit_stack = None

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
        self.out.markdown("### Model Config\n")
        for key, value in self.model_config.items():
            self.out.kv(key, value or "(not set)")

    # ------------------------------------------------------------------
    # Directory & symlink setup
    # ------------------------------------------------------------------
    def _setup_directories(self):
        self.tools_dir.mkdir(parents=True, exist_ok=True)
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)

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
    # MCP (Model Context Protocol)
    # ------------------------------------------------------------------
    @contextlib.asynccontextmanager
    async def _mcp_context(self):
        """Context manager to handle MCP server connections."""
        await self._connect_mcp()
        try:
            yield
        finally:
            await self._disconnect_mcp()

    async def _connect_mcp(self):
        """Connect to MCP servers defined in mcp_file."""
        if not self.mcp_file or not self.mcp_file.exists():
            return

        self._mcp_exit_stack = contextlib.AsyncExitStack()
        
        try:
            with open(self.mcp_file) as f:
                config = json.load(f)
        except Exception as e:
            self.out.warn(f"Failed to load MCP config {self.mcp_file}: {e}")
            return

        servers = config.get("mcpServers", {})
        if not servers:
            return

        for name, cfg in servers.items():
            command = cfg.get("command")
            if not command:
                continue
            args = cfg.get("args", [])
            env = os.environ.copy()
            env.update(cfg.get("env", {}))
            
            params = StdioServerParameters(command=command, args=args, env=env)
            try:
                # We need to manage the lifetimes of these
                read, write = await self._mcp_exit_stack.enter_async_context(stdio_client(params))
                session = await self._mcp_exit_stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self.mcp_sessions[name] = session
                
                tools_resp = await session.list_tools()
                for tool in tools_resp.tools:
                    # Store tool with its server name and full info
                    self.mcp_tools[tool.name] = (name, tool)
                self.out.info(f"Connected to MCP server {name} ({len(tools_resp.tools)} tools)")
            except Exception as e:
                self.out.warn(f"Failed to connect to MCP server {name}: {e}")

    async def _disconnect_mcp(self):
        """Disconnect from all MCP servers."""
        if self._mcp_exit_stack:
            await self._mcp_exit_stack.aclose()
            self._mcp_exit_stack = None
        self.mcp_sessions = {}
        self.mcp_tools = {}

    async def _call_mcp_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """Call an MCP tool and return its output as a string."""
        if tool_name not in self.mcp_tools:
            raise ValueError(f"Unknown MCP tool: {tool_name}")
        
        server_name, _ = self.mcp_tools[tool_name]
        session = self.mcp_sessions[server_name]
        
        try:
            result = await session.call_tool(tool_name, arguments)
            # Combine all content blocks into a single string
            output = []
            for block in result.content:
                if hasattr(block, 'text'):
                    output.append(block.text)
                elif hasattr(block, 'data'):
                    output.append(f"[Binary data: {len(block.data)} bytes]")
            return "\n".join(output)
        except Exception as e:
            return f"Error calling MCP tool {tool_name}: {e}"

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

    def get_existing_categories(self) -> list[str]:
        """Return list of existing tool categories (subdirs in tools/)."""
        if not self.tools_dir.exists():
            return []
        return sorted([
            d.name for d in self.tools_dir.iterdir()
            if d.is_dir() and (d / "bin").exists()
        ])

    def classify_tool(self, name: str) -> str:
        """Ask the LLM to classify a tool into a task category.
        
        Uses existing categories as hints, but allows the LLM to create new ones.
        Returns a category name (filesystem-safe).
        """
        existing = self.get_existing_categories()
        categories_str = ", ".join(existing) if existing else "none yet"
        
        system_prompt = (
            "You are a tool classification expert. Classify a Unix tool into a task category "
            "(e.g. vcs, build, text, net, sys, doc) or create a new category if none fit. "
            "Respond ONLY with a short category name (lowercase, alphanumeric hyphens allowed), "
            "or 'general' if truly unsure.\n\n"
            f"Existing categories: {categories_str}"
        )
        user_prompt = (
            f"Tool to classify: {name}\n"
            "What category best describes this tool's purpose?"
        )
        result = self._call_model([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ])
        if not result:
            return "general"
        
        result = result.strip().lower()
        result = re.sub(r'[^a-z0-9-]', '', result)[:30] or "general"
        return result

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

    def _system_search_tool(self, name: str) -> bool:
        """Search for *name* via whatis/apropos - last-resort RAG fallback.

        Called by _resolve_tool() only after PATH checks and LLM
        consultation have already failed.
        """
        rc, out, _ = self._run_symlinked("whatis", [name])
        if rc == 0 and out.strip() and "nothing appropriate" not in out.lower():
            return True
        rc, out, _ = self._run_symlinked("apropos", [name])
        if rc == 0 and name in out.lower():
            return True
        return False

    def _resolve_tool(self, tool: str, action: str) -> Optional[str]:
        """Resolve a tool needed by a plan step.

        Resolution order:
          1. Already symlinked - return as-is
          2. Found in tasks/ directory - return as-is (Task Tool)
          3. On PATH - symlink and return as-is
          4. Ask the LLM for an alternative - if suggested tool is on PATH, use that
          5. Fall back to apropos/whatis system search (RAG)

        Returns the resolved tool name (may differ from *tool* if the LLM
        suggested an alternative), or None if unresolvable.
        """
        # 1. Already symlinked
        if tool in self.all_tool_names():
            return tool

        # 2. Found in tasks/ directory (Task Tool)
        if (self.tasks_dir / tool).exists():
            return tool
        for ext in [".sh", ".py", ".bash", ".txt"]:
            if (self.tasks_dir / f"{tool}{ext}").exists():
                return f"{tool}{ext}"

        # 3. On PATH - just symlink it
        if shutil.which(tool):
            if self.symlink_tool(tool, auto_yes=self.auto_yes):
                return tool
            return None

        # 3. Ask the LLM - it may know an alternative that IS on this system
        self.out.info(f"tool '{tool}' not on PATH - asking model for alternative")
        resolved = self._llm_resolve_tool(tool, action)
        if resolved and resolved != tool and shutil.which(resolved):
            self.out.info(f"model suggests '{resolved}' instead of '{tool}'")
            if self.symlink_tool(resolved, auto_yes=self.auto_yes):
                return resolved

        # 4. Last resort: system search via whatis/apropos
        self.out.info("falling back to system search (apropos/whatis)")
        if self._system_search_tool(tool):
            if self.symlink_tool(tool, auto_yes=self.auto_yes):
                return tool

        return None

    def _llm_resolve_tool(self, tool: str, action: str) -> Optional[str]:
        """Ask the LLM what tool to use for *action* when *tool* is not available.

        Returns a tool name string, or None if the LLM does not know.
        """
        system_prompt = (
            "You are a Unix system expert. The planned tool is not installed "
            "on this system. Suggest an alternative tool that is commonly "
            "available and can accomplish the same task, or respond 'unknown' "
            "if you cannot think of one. Respond ONLY with the tool name "
            "(no arguments, no explanation) or 'unknown'."
        )
        user_prompt = (
            f"Planned tool: {tool}\n"
            f"Action: {action}\n"
            "What alternative tool should be used?"
        )
        result = self._call_model([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ])
        if result is None:
            return None
        result = result.strip().lower()
        if result == "unknown" or not result:
            return None
        # Sanitize: take only the first word, strip non-alphanumeric
        match = re.match(r'^[a-zA-Z][\w-]*', result)
        return match.group(0) if match else None

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

        If *task* is None the tool is auto-classified by the LLM.
        """
        if name in self.all_tool_names():
            return True

        path = self.find_tool_path(name)
        if not path:
            self.out.fatal(f"'{name}' not found on PATH - may need to be installed")
            return False

        if task is None:
            task = self.classify_tool(name)

        task_bin = self.tools_dir / task / "bin"
        task_bin.mkdir(parents=True, exist_ok=True)
        link = task_bin / name

        if auto_yes:
            self.out.info(f"symlink  {name} -> {path}  [{task}]")
        else:
            self.out.prompt(f"Allow '{name}' ({path}) [{task}] (y/n)? ", end="")
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
    #     SKILL.md   - YAML frontmatter (name, description) + markdown
    #                   body with instructions / context for LLM use
    #     plan.json  - machine-readable execution data:
    #                   plan, params_map, success_condition,
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
                # Already migrated - remove orphaned JSON
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
                "user's task string and substituted into the\n"
                "plan before execution.\n"
            )
            skill_md = f"---\n{yaml.dump(frontmatter, default_flow_style=False).strip()}\n---\n\n{body}"
            with open(skill_dir / "SKILL.md", "w") as fh:
                fh.write(skill_md)

            # Remove the old JSON file
            f.unlink()

        # Also clean up skills that were previously 'invalidated' -
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
                "params_map": plan_data.get("params_map", {}),
                "success_count": plan_data.get("success_count", 1),
                "tools_used": plan_data.get("tools_used", []),
            })
        return skills

    def _find_applicable_skill(self, task: str) -> Optional[dict]:
        """Find a skill that matches the task by consulting the LLM.

        Presents the task and available skill summaries to the model,
        which decides whether any skill applies and extracts parameter
        values.  The extracted values are stored in
        skill["_extracted_params"] for use by apply_skill().

        Returns the matched skill dict, or None if no skill applies.
        """
        skills = self.get_available_skills()
        if not skills:
            return None

        # Build a minimal summary of each skill for the LLM
        skills_info = []
        for s in skills:
            param_names = sorted(set(s.get("params_map", {}).values()))
            skills_info.append({
                "name": s["name"],
                "description": s["description"],
                "tools_used": s.get("tools_used", []),
                "parameters_to_extract": param_names,
            })

        system_prompt = (
            "You are a skill matcher for a command-line agent. "
            "Determine if any of the available skills completely handles the "
            "user's task.  CRITICAL: Do NOT match a generic skill (e.g. one "
            "that just runs 'cat' or 'ls') when the user's task clearly needs "
            "a purpose-built tool (e.g. 'df' for disk space, 'free' for memory). "
            "A skill must be a SPECIFIC, meaningful procedure - not a single "
            "trivial command.  If no skill is a genuine fit, respond with null.\n\n"
            "Respond ONLY with valid JSON in this exact format:\n"
            "{\n"
            '  "matched_skill": "skill_name_or_null",\n'
            '  "extracted_params": { "param_name": "extracted_value" }\n'
            "}"
        )
        user_prompt = (
            f"User task: {task}\n\n"
            f"Available skills:\n{json.dumps(skills_info, indent=2)}"
        )

        result = self._call_model([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ])
        if not result:
            return None

        result = result.strip()
        if result.startswith("```"):
            result = re.sub(r'^```(?:json)?\n?', '', result)
            result = re.sub(r'\n?```$', '', result)
            result = result.strip()

        try:
            parsed = json.loads(result)
            matched_name = parsed.get("matched_skill")
            if not matched_name or matched_name == "null":
                return None
            for s in skills:
                if s["name"] == matched_name:
                    s["_extracted_params"] = parsed.get("extracted_params", {})
                    return s
        except (json.JSONDecodeError, ValueError):
            self.out.warning("skill matching response unparseable")
            return None

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

    def _analyze_plan(self, task: str, plan: list[dict]) -> tuple[str, list[dict], dict[str, str]]:
        """Ask the LLM to name the skill, identify variable args, and parameterize the plan.

        Replaces the former heuristic chain (_generalize_skill_name +
        _infer_param_name + _parameterize_plan) with a single LLM call
        that understands semantics - e.g. that 'clone' in "git clone" is a
        static sub-command while the URL is the variable input.

        Returns (skill_name, parameterized_plan, params_map) where:
          skill_name: short, generic, filesystem-safe (e.g. 'clone-repo')
          parameterized_plan: plan with {{param_name}} in args
          params_map: {original_value: param_name}

        Falls back to ("generic-task", plan, {}) on LLM failure.
        """
        fallback = ("generic-task", plan, {})

        system_prompt = (
            "You are a skill extraction assistant for a command-line agent. "
            "Given a user task and the execution plan that fulfilled it, your job is to:\n"
            "1. Generate a short, generic skill name (e.g. 'clone-repo', 'check-disk-space').\n"
            "2. Identify which arguments in the plan are VARIABLE inputs from the "
            "user's task (URLs, paths, repo names, versions, branch names, etc.).\n"
            "   Do NOT parameterize static command sub-words (clone, ls, install, etc.) "
            "or flags (-v, --all, -h, etc.) - those are part of the tool's interface.\n"
            "3. Give each variable a semantic parameter name (e.g. 'repository_url', "
            "'target_dir', 'branch_name').\n"
            "4. Return the plan with variable args replaced by {{param_name}}.\n\n"
            "Respond ONLY with valid JSON:\n"
            "{\n"
            '  "skill_name": "...",\n'
            '  "params_map": { "original_value": "param_name" },\n'
            '  "parameterized_plan": [ { "action": "...", "tool": "...", "args": ["...", "{{param_name}}"] } ]\n'
            "}"
        )
        user_prompt = (
            f"Task: {task}\n"
            f"Plan:\n{json.dumps(plan, indent=2)}"
        )

        result = self._call_model([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ])
        if not result:
            return fallback

        result = result.strip()
        if result.startswith("```"):
            result = re.sub(r'^```(?:json)?\n?', '', result)
            result = re.sub(r'\n?```$', '', result)
            result = result.strip()

        try:
            parsed = json.loads(result)
            skill_name = parsed.get("skill_name", "generic-task")
            params_map = parsed.get("params_map", {})
            param_plan = parsed.get("parameterized_plan", plan)

            # Basic validation
            if not isinstance(param_plan, list) or not isinstance(params_map, dict):
                return fallback
            if len(param_plan) != len(plan):
                return fallback

            # Make skill name filesystem-safe
            skill_name = re.sub(r'[^\w-]', '-', skill_name).strip('-').lower() or "generic-task"

            return skill_name, param_plan, params_map
        except (json.JSONDecodeError, ValueError):
            return fallback



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

    def _find_similar_skills(self, plan: list[dict], threshold: float = 0.6) -> list[dict]:
        """Find skills with similar plan structure using the LLM.
        
        Returns list of {name, description, similarity} dicts for skills
        with similarity >= threshold (0.0 to 1.0).
        """
        existing = self.get_available_skills()
        if not existing:
            return []
        
        new_plan_summary = ", ".join([
            f"{s.get('action', '?')} using {s.get('tool', '?')}"
            for s in plan
        ])
        
        for s in existing:
            exist_plan = s.get("tools_used", [])
            exist_summary = ", ".join([
                f"{t}" for t in exist_plan
            ])
        
        prompts = []
        for s in existing:
            exist_plan = s.get("tools_used", [])
            exist_summary = ", ".join(exist_plan) if exist_plan else "(no tools)"
            prompts.append({
                "skill_name": s["name"],
                "existing_summary": exist_summary,
            })
        
        if not prompts:
            return []
        
        system_prompt = (
            "You are a skill similarity detector. Compare the new skill plan with each existing skill. "
            "Rate similarity from 0.0 (completely different) to 1.0 (essentially identical). "
            "Consider: same tools, similar sequence, similar purpose. "
            "Respond ONLY with valid JSON array of {skill_name, similarity} objects:\n"
            "[{skill_name: \"name1\", similarity: 0.85}, {skill_name: \"name2\", similarity: 0.2}]"
        )
        user_prompt = (
            f"New skill plan: {new_plan_summary}\n\n"
            f"Existing skills:\n{json.dumps(prompts, indent=2)}"
        )
        
        result = self._call_model([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ])
        if not result:
            return []
        
        result = result.strip()
        if result.startswith("```"):
            result = re.sub(r'^```(?:json)?\n?', '', result)
            result = re.sub(r'\n?```$', '', result)
            result = result.strip()
        
        try:
            parsed = json.loads(result)
            return [p for p in parsed if p.get("similarity", 0) >= threshold]
        except json.JSONDecodeError:
            return []

    def _save_skill(self, task: str, plan: list[dict], success_condition: str,
                    tools_used: list[str], success: bool = True) -> Optional[str]:
        """Persist the completed task as a reusable, parameterized skill.

        Skills are stored in Anthropic SKILL.md directory format:
          skills/<name>/SKILL.md  - frontmatter + instructions
          skills/<name>/plan.json  - machine-readable plan data
        """
        # Don't save skills that don't actually do anything
        if not tools_used:
            self.out.info("no tools used - skill not saved (empty plans are useless)")
            return None

        self.skills_dir.mkdir(parents=True, exist_ok=True)

        # Generate skill name and parameterize plan via LLM
        skill_name, param_plan, params_map = self._analyze_plan(task, plan)
        skill_dir = self.skills_dir / skill_name

        # Check for similar existing skills BEFORE saving (unless auto_yes)
        if not self.auto_yes:
            similar = self._find_similar_skills(plan)
            if similar:
                self.out.warning(f"Similar skills found:")
                for s in similar:
                    self.out.sublist(f"{s['name']} (similarity: {s['similarity']:.0%})")
                self.out.prompt("Save as new skill anyway (y/n), or enter skill name to update? ", end="")
                resp = sys.stdin.readline().strip()
                if resp.lower() == "n":
                    self.out.info("skill not saved")
                    return None
                if resp and resp != skill_name:
                    # User wants to update an existing skill
                    alt_dir = self.skills_dir / resp
                    if alt_dir.exists():
                        skill_dir = alt_dir
                        skill_name = resp

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
                    # Different plan structure - save as a variant
                    variant = len(plan)
                    skill_name = f"{skill_name}-{variant}"
                    skill_dir = self.skills_dir / skill_name
                    existing_plan_data = {}
                else:
                    # Same structure - bump success count, keep existing parameterization
                    existing_plan_data["success_count"] = existing_plan_data.get("success_count", 0) + (1 if success else 0)
                    with open(plan_file, "w") as fh:
                        json.dump(existing_plan_data, fh, indent=2)
                    return str(skill_dir)
            except (json.JSONDecodeError, IOError):
                pass

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
        desc = ', '.join(desc_parts)

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
            param_lines.append(f"- `{{{{{param_name}}}}}` - extracted from task string (was `{orig_val}`)")
        params_section = ""
        if param_lines:
            params_section = (
                "\n### Parameters\n"
                "Values are extracted from the task string by the LLM "
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

    def clean_skills(self) -> None:
        """Review skills, find similar ones, and let user merge/delete interactively."""
        self.log.info("CLEAN_SKILLS: starting")
        skills = self.get_available_skills()
        self.log.info("CLEAN_SKILLS: found %d skills", len(skills))
        
        if len(skills) < 2:
            self.out.info("No review needed - only one skill exists")
            return
        
        self.out.headline("Skill Review")
        
        # Find similar skill pairs using LLM
        skill_names = [s["name"] for s in skills]
        skill_summaries = []
        for s in skills:
            tools = s.get("tools_used", [])
            desc = s.get("description", "(no description)")
            skill_summaries.append({
                "name": s["name"],
                "tools": ", ".join(tools) if tools else "none",
                "description": desc,
            })
        
        self.out.info(f"Analyzing {len(skills)} skills for similarity...")
        self.log.info("CLEAN_SKILLS: calling LLM for similarity analysis")
        
        system_prompt = (
            "You are a skill similarity analyzer. Find all pairs of skills that are similar (≥60% similar). "
            "Similar skills share the same tools, have similar purposes, or could be consolidated. "
            "Respond ONLY with valid JSON array of {skill1, skill2, similarity, reason} objects:\n"
            "[{skill1: \"a\", skill2: \"b\", similarity: 0.85, reason: \"both do web research\"}]"
        )
        user_prompt = (
            f"Skills to analyze:\n{json.dumps(skill_summaries, indent=2)}"
        )
        
        result = self._call_model([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ])
        
        self.log.info("CLEAN_SKILLS: _call_model returned: %s", result[:200] if result else "None")
        
        similar_pairs = []
        if result:
            try:
                if result.startswith("```"):
                    result = re.sub(r'^```(?:json)?\n?', '', result)
                    result = re.sub(r'\n?```$', '', result).strip()
                parsed = json.loads(result)
                similar_pairs = [p for p in parsed if p.get("similarity", 0) >= 0.6]
            except (json.JSONDecodeError, ValueError) as e:
                self.log.error("CLEAN_SKILLS: JSON parse error: %s", e)
                pass
        else:
            self.log.warning("CLEAN_SKILLS: _call_model returned None")
        
        if not similar_pairs:
            self.out.success("No similar skills found - all look distinct!")
            return
        
        self.out.warning(f"Found {len(similar_pairs)} similar skill pairs:")
        for pair in similar_pairs:
            self.out.kv(f"{pair['skill1']} ↔ {pair['skill2']}", f"{pair['similarity']:.0%}: {pair.get('reason', '')}")
        
        # Let user decide what to do with each similar pair
        for pair in similar_pairs:
            s1, s2 = pair["skill1"], pair["skill2"]
            sim = pair["similarity"]
            
            self.out.prompt(f"\n[{s1}] ↔ [{s2}] ({sim:.0%} similar)\n", end="")
            self.out.prompt(f"  [k]eep both, [m]erge into one, [d]elete one, or [s]kip? ", end="")
            resp = sys.stdin.readline().strip().lower()
            
            if resp == "m":
                # Merge: ask which one to keep
                self.out.prompt(f"  Keep which name? [{s1}/{s2}]: ", end="")
                keep = sys.stdin.readline().strip() or s1
                other = s2 if keep == s1 else s1
                
                keep_dir = self.skills_dir / keep
                other_dir = self.skills_dir / other
                
                # Update plan.json to note the merge
                plan_file = keep_dir / "plan.json"
                if plan_file.exists():
                    try:
                        with open(plan_file) as fh:
                            data = json.load(fh)
                        data["merged_from"] = other
                        data["success_count"] = data.get("success_count", 1)
                        with open(plan_file, "w") as fh:
                            json.dump(data, fh, indent=2)
                    except (json.JSONDecodeError, IOError):
                        pass
                
                # Delete the other
                shutil.rmtree(other_dir)
                self.out.success(f"merged '{other}' into '{keep}'")
            elif resp == "d":
                self.out.prompt(f"  Delete which? [{s1}/{s2}]: ", end="")
                delete = sys.stdin.readline().strip()
                if delete in [s1, s2]:
                    self.delete_skill(delete)
            elif resp == "k" or resp == "s" or not resp:
                self.out.info("kept both")
        
        self.out.success("Review complete!")

    def edit_task(self, task_name: str) -> bool:
        """Open a task script in the users default editor."""
        # Find the task file (could have various extensions: .sh, .py, .txt, etc.)
        task_path = None
        if (self.tasks_dir / task_name).exists():
            task_path = self.tasks_dir / task_name
        else:
            # Try with common extensions
            for ext in [".sh", ".py", ".bash", ".txt"]:
                candidate = self.tasks_dir / f"{task_name}{ext}"
                if candidate.exists():
                    task_path = candidate
                    break
        
        if not task_path:
            self.out.fatal(f"task script '{task_name}' not found in {self.tasks_dir}")
            return False

        editor = os.environ.get('EDITOR', 'vi')
        try:
            subprocess.run([editor, str(task_path)])
            return True
        except Exception as e:
            self.out.fatal(f"failed to open editor: {e}")
            return False

    def import_skill(self, input_path: str) -> bool:
        """Import a skill from a directory, SKILL.md file, or .skill archive."""
        path = Path(input_path).resolve()
        if not path.exists():
            self.out.fatal(f"import path '{input_path}' not found")
            return False

        # If it's a directory, assume it contains SKILL.md
        if path.is_dir():
            meta = self._parse_skill_md(path)
            if not meta:
                self.out.fatal(f"no SKILL.md with valid frontmatter found in '{input_path}'")
                return False
            skill_name = meta.get("name") or path.name
            dest_dir = self.skills_dir / skill_name
            if dest_dir.exists():
                self.out.warning(f"skill '{skill_name}' already exists - overwriting")
                shutil.rmtree(dest_dir)
            shutil.copytree(path, dest_dir)
            self.out.success(f"imported skill '{skill_name}' from directory")
            return True

        # If it's a file, check extension
        if path.suffix == ".skill":
            import zipfile
            try:
                with zipfile.ZipFile(path, 'r') as zipf:
                    # Look for SKILL.md in the root of the zip
                    # (The package_skill.py script adds 'arcname = file_path.relative_to(skill_path.parent)',
                    # so the top-level directory is usually preserved inside the zip.)
                    skill_md_name = None
                    for name in zipf.namelist():
                        if name.endswith("SKILL.md"):
                            skill_md_name = name
                            break
                    if not skill_md_name:
                        self.out.fatal((f"no SKILL.md found in '{input_path}'"))
                        return False
                    
                    # Extract to a temp dir to parse name
                    import tempfile
                    with tempfile.TemporaryDirectory() as tmpdir:
                        zipf.extractall(tmpdir)
                        # Find the directory containing SKILL.md
                        for root, dirs, files in os.walk(tmpdir):
                            if "SKILL.md" in files:
                                meta = self._parse_skill_md(Path(root))
                                if meta:
                                    skill_name = meta.get("name")
                                    if skill_name:
                                        dest_dir = self.skills_dir / skill_name
                                        if dest_dir.exists():
                                            self.out.warning(f"skill '{skill_name}' already exists - overwriting")
                                            shutil.rmtree(dest_dir)
                                        shutil.copytree(root, dest_dir)
                                        self.out.success(f"imported skill '{skill_name}' from archive")
                                        return True
                self.out.fatal(f"archive '{input_path}' does not contain a valid skill")
                return False
            except Exception as e:
                self.out.fatal(f"failed to extract skill from archive: {e}")
                return False

        # If it's just a SKILL.md file
        if path.name == "SKILL.md" or path.suffix == ".md":
            # Create a directory for it based on its internal name
            # (Need a dummy dir to pass to _parse_skill_md)
            import tempfile
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp_path = Path(tmpdir) / "SKILL.md"
                shutil.copy2(path, tmp_path)
                meta = self._parse_skill_md(Path(tmpdir))
                if not meta or not meta.get("name"):
                    self.out.fatal(f"'{input_path}' has no 'name' in frontmatter")
                    return False
                skill_name = meta["name"]
                dest_dir = self.skills_dir / skill_name
                dest_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest_dir / "SKILL.md")
                self.out.success(f"imported skill '{skill_name}' from Markdown file")
                return True

        self.out.fatal(f"don't know how to import '{input_path}' (expected dir, .md, or .skill)")
        return False

    def export_skill(self, skill_name: str, output_path: str = None) -> bool:
        """Package a skill into a .skill archive (zip)."""
        skill_dir = self._find_skill_dir(skill_name)
        if not skill_dir:
            self.out.fatal(f"skill '{skill_name}' not found")
            return False

        if output_path:
            dest = Path(output_path).resolve()
            if dest.is_dir():
                dest = dest / f"{skill_name}.skill"
            elif dest.suffix != ".skill":
                dest = dest.with_suffix(".skill")
        else:
            dest = Path.cwd() / f"{skill_name}.skill"

        import zipfile
        try:
            with zipfile.ZipFile(dest, 'w', zipfile.ZIP_DEFLATED) as zipf:
                # Store it under its own directory name in the zip
                # to match how package_skill.py does it (so it has a root folder).
                for file_path in skill_dir.rglob('*'):
                    if not file_path.is_file():
                        continue
                    arcname = Path(skill_name) / file_path.relative_to(skill_dir)
                    zipf.write(file_path, arcname)
            self.out.success(f"exported skill '{skill_name}' to {dest.name}")
            return True
        except Exception as e:
            self.out.fatal(f"failed to export skill '{skill_name}': {e}")
            return False

    async def apply_skill(self, skill_meta: dict, task: str = None) -> tuple[bool, str]:
        """Execute a saved skills plan with parameter substitution.

        Extracts parameter values from the current task (stored in
        skill_meta["_extracted_params"] by _find_applicable_skill), substitutes
        them into the parameterized plan, then executes.

        If no plan exists (e.g. for a newly imported skill), a new plan is
        generated using the skills instructions as context.

        Returns (success, captured_output).
        """
        skill_name = skill_meta.get('name', 'unknown')
        self.log.info("APPLY_SKILL: skill=%s | task=%s", skill_name, task)
        
        # Load full skill data from file
        full = self._load_skill(skill_name)
        if not full:
            # Fallback for just-imported skills that only have metadata
            full = skill_meta

        plan = full.get("plan", [])
        self.log.info("APPLY_SKILL: skill=%s has %d plan steps", skill_name, len(plan))
        
        # If no plan exists, and we have a task, try to create one
        if not plan and task:
            self.out.info(f"generating plan for skill '{skill_name}'...")
            self.log.info("APPLY_SKILL: no existing plan, generating new one")
            # Include skill instructions in the task context
            skill_dir = self._find_skill_dir(skill_name)
            context = task
            if skill_dir:
                skill_md = skill_dir / "SKILL.md"
                if skill_md.exists():
                    context = f"Skill Instructions:\n{skill_md.read_text()}\n\nUser Task: {task}"
            
            plan, success_condition = self._create_plan(context)
            if not plan:
                self.log.error("APPLY_SKILL: plan generation failed")
                return False, ""
            
            # Validate plan
            if not self._validate_plan(plan):
                self.log.error("APPLY_SKILL: plan validation failed")
                return False, ""
            
            # If this is a newly generated plan, we don't need parameter substitution
            # because it was generated for this specific task.
            self.out.section("executing")
            return await self._execute_plan(plan, task)

        if not plan:
            self.log.warning("APPLY_SKILL: skill has no plan and no task provided")
            self.out.info("skill has no saved plan and no task provided to generate one")
            return False, ""

        # Reject skills where no step actually does anything
        if not any(step.get("tool") for step in plan):
            self.out.warning(f"skill '{skill_meta['name']}' has no executable steps - skipping")
            return False, ""

        # Get extracted parameters (set by _find_applicable_skill)
        extracted_params = skill_meta.get("_extracted_params", {})

        # Resolve parameter placeholders in plan args
        resolved_plan = []
        for step in plan:
            resolved_step = dict(step)
            args = step.get("args")
            if isinstance(args, list):
                resolved_args = []
                for arg in args:
                    resolved_arg = arg
                    for param_name, param_value in extracted_params.items():
                        resolved_arg = resolved_arg.replace(f"{{{{{param_name}}}}}", param_value)
                    resolved_args.append(resolved_arg)
                resolved_step["args"] = resolved_args
            elif isinstance(args, dict):
                resolved_args = {}
                for k, v in args.items():
                    if isinstance(v, str):
                        for param_name, param_value in extracted_params.items():
                            v = v.replace(f"{{{{{param_name}}}}}", param_value)
                    resolved_args[k] = v
                resolved_step["args"] = resolved_args
            resolved_plan.append(resolved_step)

        # Check for unresolved parameters
        unresolved = []
        for step in resolved_plan:
            args = step.get("args")
            if isinstance(args, list):
                for arg in args:
                    unresolved.extend(re.findall(r'\{\{(\w+)\}\}', arg))
            elif isinstance(args, dict):
                for v in args.values():
                    if isinstance(v, str):
                        unresolved.extend(re.findall(r'\{\{(\w+)\}\}', v))
        if unresolved:
            self.out.fatal(f"unresolved parameters: {', '.join(set(unresolved))}")
            return False, ""

        self.out.info(f"executing skill: {full.get('name', skill_meta['name'])}")
        if extracted_params:
            param_str = ", ".join(f"{k}={v}" for k, v in extracted_params.items())
            self.out.info(f"params: {param_str}")
        if full.get("tools_used"):
            self.out.info(f"tools: {', '.join(full['tools_used'])}")

        # Validate each step
        for i, step in enumerate(resolved_plan):
            tool = step.get("tool")
            if tool and tool not in self.all_tool_names() and tool not in self.mcp_tools:
                resolved = self._resolve_tool(tool, step.get('action', ''))
                if resolved is None:
                    self.out.fatal(f"tool '{tool}' not available on this system")
                    return False, ""
                if resolved != tool:
                    step["tool"] = resolved

        # Execute - capture output for verification
        self.out.section("executing")
        return await self._execute_plan(resolved_plan, task)

    # ------------------------------------------------------------------
    # Success condition
    # ------------------------------------------------------------------
    async def _verify_success(self, task: str, success_condition: str, captured_output: str) -> tuple[bool, str]:
        """Ask the LLM to verify whether the tool output satisfies the success condition.

        Returns (satisfied, result_summary) where result_summary is a
        concise human-readable answer derived from the tool output
        (e.g. "The CPU is AMD Ryzen 7 and the memory is 16GB").
        """
        if not captured_output.strip():
            self.out.fatal("no output produced - cannot verify success")
            return False, ""

        # Truncate output to avoid excessive token usage.
        # When there are multiple steps, cap each step's contribution so
        # all steps are represented in the verification prompt.
        MAX_VERIFY_CHARS = 8000
        PER_STEP_CAP = 3000
        if len(captured_output) <= MAX_VERIFY_CHARS:
            output_for_llm = captured_output
        else:
            # Split by step boundaries ("---" separators between step outputs)
            # and cap each chunk so all steps are represented
            chunks = captured_output.split("\n---\n")
            trimmed = []
            for chunk in chunks:
                if len(chunk) > PER_STEP_CAP:
                    trimmed.append(chunk[:PER_STEP_CAP] + "\n... (truncated)")
                else:
                    trimmed.append(chunk)
            output_for_llm = "\n---\n".join(trimmed)[:MAX_VERIFY_CHARS]

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
            '{"satisfied": true/false, "reason": "brief explanation", "result": "concise human-readable answer to the task"}'
        )

        result = self._call_model([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ])

        if result is None:
            self.out.warning("verification call failed - cannot confirm success")
            return False, ""

        result = result.strip()
        if result.startswith("```"):
            result = re.sub(r'^```(?:json)?\n?', '', result)
            result = re.sub(r'\n?```$', '', result)
            result = result.strip()

        try:
            parsed = json.loads(result)
            satisfied = parsed.get("satisfied", False)
            reason = parsed.get("reason", "")
            result_summary = parsed.get("result", "")
            if satisfied:
                self.out.info(f"verified: {reason}")
                return True, result_summary
            else:
                self.out.fatal(f"verification failed: {reason}")
                return False, result_summary
        except (json.JSONDecodeError, ValueError):
            self.out.warning("verification response unparseable - cannot confirm success")
            return False, ""

    # ------------------------------------------------------------------
    # LLM inference
    # ------------------------------------------------------------------
    def _resolve_url(self) -> str:
        """Return a base URL that includes the /v1 (or /vN) API version prefix.

        Most OpenAI-compatible providers expect paths like /v1/chat/completions.
        If the users url already ends with a version segment (e.g. /v1,
        /v2, /v1beta), its kept as-is.  Otherwise /v1 is appended automatically.
        """
        url = self.model_config.get("url") or "https://api.openai.com/v1"
        url = url.rstrip('/')
        # Already has a versioned path segment like /v1, /v2, /v1beta, etc.
        if re.search(r'/v\d+(?:[a-z]*)?$', url):
            return url
        return f"{url}/v1"

    def list_models(self):
        """Query the /models endpoint and display available models."""
        url = self._resolve_url()
        url = f"{url}/models"

        import urllib.error
        import urllib.request
        headers = {}
        if self.model_config.get("key"):
            headers["Authorization"] = f"Bearer {self.model_config['key']}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with self.out.spinner(f"Fetching models from {url}"):
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
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

        md = f"### Models at {url}\n"
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
        self.out.markdown("### API Diagnostics\n")
        self.out.kv("url", url)
        self.out.kv("model", self.model_config.get("model") or "(none)")
        self.out.kv("key", key_masked)
        self.out.info(f"debug with: ac --curlify -m {self.model_config.get('model') or ''} '<task>'")

    @staticmethod
    def _curlify(url: str, headers: dict, data: bytes) -> str:
        """Build a curl(1) command equivalent to the given HTTP request.

        Useful for debugging - paste the output into a terminal to
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

        url = self._resolve_url()
        url = f"{url}/chat/completions"

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
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    result = json.loads(resp.read())
                    return result["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()[:500]
            except Exception:
                pass
            self.log.error("CLEAN_SKILLS: HTTPError %s %s", e.code, e.reason)
            self.out.fatal(f"model call failed: HTTP {e.code} {e.reason}")
            self._print_api_diagnostics(url)
            if body:
                self.out.kv("response", body)
            return None
        except Exception as e:
            self.log.error("CLEAN_SKILLS: Exception: %s", e)
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

        mcp_desc = ""
        if self.mcp_tools:
            mcp_desc = "\nYou also have access to these MCP tools:\n"
            for tname, (sname, tinfo) in self.mcp_tools.items():
                mcp_desc += f"- {tname}: {tinfo.description}\n"
            mcp_desc += "\nFor MCP tools, provide 'args' as a dictionary of keyword arguments.\n"

        system_prompt = (
            "You are a task planner for a command-line agent. "
            "Given a user task, the currently linked tools, and directory context, "
            "produce a JSON object with a plan and a success condition.\n\n"
            "The plan is an array of steps, each with:\n"
            '  - "action": a SHORT verb-only description (e.g. "read_cpu", "list_dir", "clone_repo")\n'
            '  - "tool": the best system command, MCP tool, or a NEW SCRIPT you create for the job. Prefer purpose-built tools '
            '(e.g. "df" for disk space, "free" for memory, "git" for repos) over '
            'reading raw files with cat/head. Any system tool you name will be symlinked '
            'automatically if it exists on PATH.\n'
            '  - "args": list of string arguments for system tools, or a dictionary of arguments for MCP tools\n\n'
            "EXTENSION & TOOL CREATION:\n"
            "If a standard system tool is likely to be brittle or insufficient for a modern requirement "
            "(e.g., using 'curl' for a modern JS-heavy website is almost always the WRONG choice), "
            "you SHOULD propose creating a dedicated high-level script in the 'tasks/' directory. "
            "Use 'cat' to write the script (e.g., a Python script using 'lightpanda' or 'markitdown'). "
            "This script then becomes a reusable 'Task Tool' that you can call in subsequent steps. "
            "This makes your capabilities explicit and easy for the user to refine later.\n\n"
            'The success_condition must describe CONCRETE, DIRECTLY VERIFIABLE output. '
            'It must specify exactly what facts or values the tool output must contain.\n'
            'Bad: "output contains information about disk usage" - too vague, could be anything.\n'
            'Bad: "output can be used to infer disk space" - inferential, not directly verifiable.\n'
            'Good: "output contains filesystem mount points and their total/used/available space".\n'
            'Good: "output contains the CPU model name and total memory in kB".\n\n'
            "Return ONLY valid JSON in this format:\n"
            '{\n'
            '  "plan": [{"action": "...", "tool": "...", "args": [...]}],\n'
            '  "success_condition": "..."\n'
            '}\n\n'
            "Examples:\n"
            "To check disk space, use tool 'df' with args ['-h'].\n"
            "To read CPU info, use tool 'cat' with args ['/proc/cpuinfo'].\n"
            "To list directory contents, use tool 'ls' with args ['-la'].\n"
            "To check memory, use tool 'free' with args ['-h'].\n\n"
            "If a task changes system state (like creating a file), include a final step "
            "to output the result (e.g. ls or cat) so it can be verified."
            + mcp_desc
        )

        user_prompt = (
            f"Task: {task}\n"
            f"Currently linked tools: {', '.join(sorted(tools))}\n"
            "(You may request any system tool - it will be symlinked automatically.)\n"
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
            # Must be the new object format with plan + success_condition
            if isinstance(parsed, dict):
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
            self.out.info("plan generated by model")
            return plan, success_condition
        except (json.JSONDecodeError, ValueError, KeyError):
            self.out.fatal("model response was not valid JSON - cannot proceed")
            sys.exit(1)


    def _validate_plan(self, plan: list[dict]) -> bool:
        for i, step in enumerate(plan):
            tool = step.get("tool")
            if not tool:
                self.out.fatal(f"step {i + 1} ('{step.get('action', '?')}') has no tool - cannot execute")
                return False

            self.out.section(f"validate  step {i + 1}: {step['action']}")

            if tool in self.mcp_tools:
                self.out.info(f"tool '{tool}' available via MCP")
                continue

            resolved = self._resolve_tool(tool, step.get('action', ''))
            if resolved is None:
                self.out.fatal(f"tool '{tool}' not found on system - install it or check the name")
                return False

            if resolved != tool:
                # LLM suggested an alternative - rewrite the step
                self.out.info(f"using '{resolved}' instead of '{tool}'")
                step["tool"] = resolved
            else:
                self.out.info(f"tool '{tool}' available")
        return True

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    async def _execute_plan(self, plan: list[dict], task: str) -> tuple[bool, str]:
        # Execute each plan step, returning (success, captured_output).
        # Steps can reference previous step results using {{step_N}} or {{results}}
        # Steps can also CALL OTHER SKILLS by using "skill:skill_name" as the tool
        self.log.info("EXECUTE_PLAN: starting %d steps", len(plan))
        captured = []
        step_outputs = {}  # Map step index -> output
        
        for i, step in enumerate(plan):
            action = step.get('action', '?')
            tool = step.get("tool")
            args = step.get("args", [])
            
            # Support composable steps - substitute previous results
            if isinstance(args, dict):
                resolved_args = {}
                for k, v in args.items():
                    if isinstance(v, str):
                        for step_idx in range(i):
                            placeholder = f"{{{{step_{step_idx}}}}}"
                            if placeholder in v:
                                v = v.replace(placeholder, step_outputs.get(step_idx, ""))
                        if "{{results}}" in v:
                            v = v.replace("{{results}}", "\n".join(captured))
                        if "{{last_output}}" in v:
                            v = v.replace("{{last_output}}", captured[-1] if captured else "")
                    resolved_args[k] = v
                args = resolved_args
            elif isinstance(args, list):
                resolved_args = []
                for arg in args:
                    if isinstance(arg, str):
                        for step_idx in range(i):
                            placeholder = f"{{{{step_{step_idx}}}}}"
                            if placeholder in arg:
                                arg = arg.replace(placeholder, step_outputs.get(step_idx, ""))
                            if "{{results}}" in arg:
                                arg = arg.replace("{{results}}", "\n".join(captured))
                            if "{{last_output}}" in arg:
                                arg = arg.replace("{{last_output}}", captured[-1] if captured else "")
                        resolved_args.append(arg)
                    else:
                        resolved_args.append(arg)
                args = resolved_args
            
            self.log.info("EXECUTE_STEP %d: action=%s tool=%s args=%s", i+1, action, tool, args)
            self.out.section(f"execute  step {i + 1}: {step['action']}")

            if tool:
                step_output = ""
                
                # Check if tool is actually calling another skill (prefix "skill:" or tool name matches a skill)
                is_skill_call = False
                skill_to_call = None
                
                if tool.startswith("skill:"):
                    skill_to_call = tool[6:]  # Remove "skill:" prefix
                    is_skill_call = True
                elif self._find_skill_dir(tool):
                    skill_to_call = tool
                    is_skill_call = True
                
                if is_skill_call:
                    # Call another skill as a function
                    self.log.info("TOOL_TYPE: skill_call | skill=%s", skill_to_call)
                    self.log.info("CALLING_SKILL: %s with args=%s", skill_to_call, args)
                    
                    skill_meta = self._load_skill(skill_to_call)
                    if not skill_meta:
                        self.log.error("SKILL_NOT_FOUND: %s", skill_to_call)
                        self.out.fatal(f"skill '{skill_to_call}' not found")
                        return False, ""
                    
                    ok, skill_output = await self.apply_skill(skill_meta, task=None)
                    if not ok:
                        self.log.error("SKILL_FAILED: %s", skill_to_call)
                        return False, ""
                    
                    step_output = skill_output
                    captured.append(step_output.strip())
                else:
                    task_path = self.tasks_dir / tool
                    if task_path.exists():
                        # Call Task Tool (saved script)
                        self.out.info(f"calling Task script: {tool}")
                        self.log.info("TOOL_TYPE: task_script | path=%s", task_path)
                        # Ensure executable
                        if os.name != 'nt':
                            mode = os.stat(task_path).st_mode
                            os.chmod(task_path, mode | 0o111)
                        
                        cmd = [str(task_path)] + args if isinstance(args, list) else [str(task_path)]
                        try:
                            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                            step_output = r.stdout + r.stderr
                            self.log.info("TOOL_EXIT_CODE: %d", r.returncode)
                            for line in step_output.strip().splitlines():
                                self.out.output(line)
                            captured.append(step_output.strip())
                        except Exception as e:
                            self.log.error("TOOL_ERROR: %s", e)
                            self.out.fatal(f"failed to run Task script '{tool}': {e}")
                            return False, ""
                    elif tool in self.mcp_tools:
                        # Call MCP tool
                        if not isinstance(args, dict):
                            self.out.warn(f"MCP tool '{tool}' expects dict args, but got {type(args)}")
                            if isinstance(args, list) and not args:
                                args = {}
                        
                        self.out.info(f"calling MCP tool: {tool}")
                        self.log.info("TOOL_TYPE: mcp | tool=%s", tool)
                        step_output = await self._call_mcp_tool(tool, args)
                        for line in step_output.strip().splitlines():
                            self.out.output(line)
                        captured.append(step_output.strip())
                    else:
                        # System tool
                        self.log.info("TOOL_TYPE: system | tool=%s", tool)
                        rc, out, err = self._run_symlinked(tool, args, timeout=300, show_cmd=True)
                        self.log.info("TOOL_EXIT_CODE: %d", rc)
                        if out.strip():
                            for line in out.strip().splitlines():
                                self.out.output(line)
                            captured.append(out.strip())
                            step_output = out.strip()
                        if err.strip():
                            for line in err.strip().splitlines():
                                self.out.output(line)
                            captured.append(err.strip())
                # Store step output for composability
                step_outputs[i] = step_output
            else:
                self.log.error("EXECUTE_STEP %d: NO_TOOL for action=%s", i+1, action)
                self.out.fatal(f"no tool for step '{step['action']}' - cannot execute")
                return False, ""

        return True, "\n---\n".join(captured)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------
    async def execute_task(self, task: str, explicit_skill: str = None, preview: bool = False):
        async with self._mcp_context():
            # Log the incoming task for audit
            self.log.info("TASK RECEIVED: %s", task)
            self.log.info("-" * 60)
            
            # 1. Check for applicable skills
            self.out.section("Skills")
            self.log.info("PHASE: SKILL_MATCH | action=checking_available_skills")
            
            skill = None
            if explicit_skill:
                self.log.info("PHASE: SKILL_MATCH | explicit_skill=%s", explicit_skill)
                skill_dir = self._find_skill_dir(explicit_skill)
                if not skill_dir:
                    self.log.error("SKILL NOT FOUND: explicit_skill=%s", explicit_skill)
                    self.out.fatal(f"explicit skill '{explicit_skill}' not found")
                    sys.exit(1)
                meta = self._parse_skill_md(skill_dir)
                if not meta:
                    self.log.error("SKILL INVALID: missing SKILL.md for %s", explicit_skill)
                    self.out.fatal(f"explicit skill '{explicit_skill}' missing SKILL.md metadata")
                    sys.exit(1)
                skill = meta
                self.out.info(f"using explicit skill: {skill['name']}")
                self.log.info("PHASE: SKILL_MATCH | result=matched explicit_skill=%s", skill['name'])
            else:
                skill = self._find_applicable_skill(task)
                if skill:
                    self.log.info("PHASE: SKILL_MATCH | result=matched skill=%s", skill['name'])
                    self.out.info(f"skill match: {skill['name']}")
                else:
                    self.log.info("PHASE: SKILL_MATCH | result=no_matching_skill")

            skill_success_condition = None
            if skill:
                self.log.info("PHASE: APPLY_SKILL | skill=%s", skill['name'])
                # 2. Apply skill - capture output for verification
                self.out.section("Applying")
                ok, captured = await self.apply_skill(skill, task=task)
                
                if ok:
                    self.log.info("PHASE: APPLY_SKILL | result=executed_successfully")
                    # Load the skills saved success condition for verification
                    full_skill = self._load_skill(skill['name'])
                    skill_success_condition = full_skill.get("success_condition", "")
                    # Backwards compat: old skills stored success_condition as a dict
                    if isinstance(skill_success_condition, dict):
                        skill_success_condition = skill_success_condition.get("description", "")
                    # Substitute any parameter placeholders
                    extracted = skill.get("_extracted_params", {})
                    for pname, pval in extracted.items():
                        skill_success_condition = skill_success_condition.replace(f"{{{{{pname}}}}}", pval)

                    self.log.info("PHASE: VERIFY | success_condition=%s", skill_success_condition)

                    if skill_success_condition:
                        self.out.section("Verifying")
                        ok, result_summary = await self._verify_success(task, skill_success_condition, captured)
                        if ok:
                            self.log.info("PHASE: VERIFY | result=satisfied")
                            self.out.separator()
                            if result_summary:
                                self.out.result(result_summary)
                            self.out.info(skill_success_condition)
                            self.log.info("=" * 60)
                            self.log.info("TASK COMPLETED SUCCESSFULLY")
                            self.log.info("=" * 60)
                            return
                        else:
                            self.log.warning("PHASE: VERIFY | result=fAILED verification_failed")
                else:
                    self.log.warning("PHASE: APPLY_SKILL | result=execution_failed")
                
                if explicit_skill:
                    self.log.error("PHASE: SKILL_FAILED | explicit_skill=%s failed_verification", explicit_skill)
                    # If explicit skill fails verification, we stop here.
                    self.out.fatal(f"explicit skill '{explicit_skill}' failed verification")
                    sys.exit(1)

                # Log what failed for audit
                self.log.warning("PHASE: FALLBACK | reason=skill_did_not_satisfy | skill_name=%s | skill_success_condition=%s", 
                               skill.get('name', 'unknown'), skill_success_condition or '(none)')
                self.log.info("PHASE: CREATE_PLAN | action=generating_new_plan")
                self.out.warning("skill did not satisfy - falling back to plan")
            else:
                self.log.info("PHASE: SKILL_MATCH | result=no_skill_found")
                self.out.info("none found")
                self.log.info("PHASE: CREATE_PLAN | action=generating_plan_from_scratch")

            # 3. Create plan + success condition via LLM
            self.out.section("plan")
            self.log.info("PHASE: CREATE_PLAN | action=calling_llm_for_plan")
            plan, success_condition = self._create_plan(task)
            self.log.info("PHASE: CREATE_PLAN | plan_steps=%d | success_condition=%s", len(plan), success_condition)
            
            # Show plan to user in markdown format
            print("\n" + "="*60)
            print("PLAN PREVIEW")
            print("="*60)
            print(f"**Task:** {task}\n")
            print(f"**Success condition:** {success_condition}\n")
            print("**Steps:**")
            for i, step in enumerate(plan):
                tool = step.get("tool", "(none)")
                args = step.get("args", [])
                print(f"- Step {i+1}: **{step.get('action')}**")
                print(f"  - tool: `{tool}`")
                print(f"  - args: {args}")
            print("="*60)
            
            if preview:
                while True:
                    print("\n(c)ontinue, (e)dit plan, (r)etry, (a)dd step, (q)uit? ", end="")
                    try:
                        choice = input().strip().lower()
                    except KeyboardInterrupt:
                        print("\nAborted.")
                        self.log.info("PREVIEW: user aborted (Ctrl+C)")
                        sys.exit(0)
                    if choice == 'q':
                        print("Aborted.")
                        self.log.info("PREVIEW: user aborted")
                        sys.exit(0)
                    elif choice == 'c':
                        print("Executing...")
                        break
                    elif choice == 'r':
                        print("Retrying...")
                        self.log.info("PREVIEW: user chose retry")
                        # Retry = create new plan
                        plan, success_condition = self._create_plan(task)
                        self.log.info("PHASE: CREATE_PLAN | plan_steps=%d | success_condition=%s", len(plan), success_condition)
                        print("\nNew plan:")
                        for i, step in enumerate(plan):
                            tool = step.get("tool", "(none)")
                            print(f"  {i+1}. {step.get('action')} ({tool})")
                    elif choice == 'e':
                        print("Edit plan - this will open your editor with the YAML plan.")
                        print("After editing, the plan will be re-validated.")
                        import tempfile
                        # Use YAML for editing - more readable
                        edited_content = yaml.dump({"plan": plan, "success_condition": success_condition}, default_flow_style=False)
                        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
                            f.write(edited_content)
                            temp_path = f.name
                        editor = os.environ.get('EDITOR', 'vi')
                        subprocess.run([editor, temp_path])
                        with open(temp_path) as f:
                            edited = yaml.safe_load(f)
                        os.unlink(temp_path)
                        plan = edited.get("plan", plan)
                        success_condition = edited.get("success_condition", success_condition)
                        print("Plan updated.")
                        self.log.info("PREVIEW: user edited plan")
                    elif choice == 'a' or choice == 'add':
                        # Add a new step
                        print("\nAdd a new step:")
                        action = input("  action (e.g. fetch_url): ").strip()
                        tool = input("  tool (e.g. curl, web_search skill): ").strip()
                        args_str = input("  args (JSON format, e.g. {'url': 'example.com'}): ").strip()
                        try:
                            args = json.loads(args_str) if args_str else {}
                        except json.JSONDecodeError:
                            args = {"note": args_str}
                        plan.append({"action": action, "tool": tool, "args": args})
                        print(f"Added: {action} -> {tool}")
                        self.log.info("PREVIEW: user added step: %s -> %s", action, tool)
                        print("\nUpdated plan:")
                        for i, step in enumerate(plan):
                            t = step.get("tool", "(none)")
                            print(f"  {i+1}. {step.get('action')} ({t})")
                    else:
                        print("Invalid choice. c/e/r/a/q")
            
            # Continue with normal flow after preview (or skip if not preview)
            for i, step in enumerate(plan):
                tool = step.get("tool", "(none)")
                self.out.info(f"step {i + 1}: {step['action']}  ({tool})")
                self.log.info("  step %d: action=%s tool=%s args=%s", i+1, step.get('action'), tool, step.get('args'))
            self.out.info(f"success condition: {success_condition}")

            # 4. Validate plan (may rewrite tool names if alternatives are needed)
            self.out.section("validate")
            self.log.info("PHASE: VALIDATE_PLAN | action=validating_tools")
            if not self._validate_plan(plan):
                self.log.error("PHASE: VALIDATE_PLAN | result=failed")
                self.out.fatal("validation failed")
                sys.exit(1)
            self.log.info("PHASE: VALIDATE_PLAN | result=all_valid")
            self.out.info("all steps valid")

            # Compute tools_in_plan AFTER validation so rewritten names are captured
            tools_in_plan = sorted({s["tool"] for s in plan if s.get("tool")})
            self.log.info("PHASE: EXECUTE | tools_in_plan=%s", tools_in_plan)

            # 5. Execute - capture output
            self.out.section("execute")
            self.log.info("PHASE: EXECUTE | action=running_steps")
            _, captured = await self._execute_plan(plan, task)
            self.log.info("PHASE: EXECUTE | result=capture_complete")

            # 6. Verify against success condition & save skill
            self.log.info("PHASE: VERIFY | success_condition=%s", success_condition)
            self.out.section("verify")
            verified, result_summary = await self._verify_success(task, success_condition, captured)
            
            if verified:
                self.log.info("PHASE: VERIFY | result=satisfied")
                self.out.separator()
                if result_summary:
                    self.out.result(result_summary)
                skill_path = self._save_skill(task, plan, success_condition, tools_in_plan, success=True)
                self.out.info(f"SUCCESS  {success_condition}")
                self.log.info("=" * 60)
                self.log.info("TASK COMPLETED SUCCESSFULLY")
                self.log.info("skill saved: %s", Path(skill_path).name if skill_path else "(none)")
                self.log.info("=" * 60)
                if skill_path:
                    self.out.info(f"skill saved -> {Path(skill_path).name}")
            else:
                if result_summary:
                    self.out.result(f"partial result: {result_summary}")
                self._save_skill(task, plan, success_condition, tools_in_plan, success=False)
                self.out.separator()
                self.log.error("=" * 60)
                self.log.error("TASK FAILED | success_condition=%s", success_condition)
                self.log.error("=" * 60)
                self.out.fatal(f"FAILED  {success_condition}")
                log_file_path = self.out.save_log()
                self.log.info("Log file saved: %s", log_file_path)
                if log_file_path:
                    self.out.fatal(f"Log available at: {log_file_path}")
                sys.exit(1)

    def show_status(self):
        tools = self.get_all_symlinked_tools()
        md = "### Tools\n"
        for task, names in sorted(tools.items()):
            md += f"- **{task}/bin/** {'  '.join(names)}\n"
        md += "\n"
        md += self._mcp_status_markdown()
        md += self._tasks_markdown()
        md += self._skills_markdown()
        md += "\n---\n\n"
        md += "* `ac '<task description>'` (execute one-shot)\n"
        md += "* `ac <saved-task>` (run saved task script)\n"
        md += "* `ac -t <task-file>` (read task from file)\n"
        md += "* `ac -e <task-name>` (edit task script)\n"
        md += "* `ac -l` (list Anthropic skills)\n"
        self.out.markdown(md)

    def _tasks_markdown(self) -> str:
        # Build a markdown tasks (scripts) section string.
        if not self.tasks_dir.exists():
            return ""
        tasks = sorted([f.name for f in self.tasks_dir.iterdir() if f.is_file()])
        md = "### Tasks (Scripts)\n"
        if not tasks:
            md += "*none yet - save tools or scripts to ~/local/maxac/tasks*\n"
        else:
            for t in tasks:
                md += f"- **{t}**\n"
        md += "\n"
        return md

    def _mcp_status_markdown(self) -> str:
        # Build a markdown MCP servers section string.
        if not self.mcp_file or not self.mcp_file.exists():
            return ""
        
        md = "### MCP Servers\n"
        try:
            with open(self.mcp_file) as f:
                config = json.load(f)
                servers = config.get("mcpServers", {})
                if not servers:
                    md += "*none configured*\n"
                else:
                    for name in sorted(servers.keys()):
                        md += f"- **{name}**\n"
        except Exception:
            md += "*error reading config*\n"
        md += "\n"
        return md

    def _skills_markdown(self) -> str:
        # Build a markdown skills section string
        skills = self.get_available_skills()
        md = "### Skills (Anthropic)\n"
        if not skills:
            md += "*none yet - they are saved automatically on success*\n"
            return md
        for s in skills:
            md += f"- **{s['name']}**\n"
            if s.get("tools_used"):
                md += f"  - uses: {', '.join(f'`{_md_escape(t)}`' for t in s['tools_used'])}\n"
        return md

    def _print_skills(self):
        # Print a formatted list of Anthropic skills
        self.out.markdown(self._skills_markdown())

    def _print_skill_detail(self, skill_name: str):
        # Print raw skill data for debugging - no formatting.
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
        description="maxac: A one-shot command-line helper "
                    "with scoped execution tasks and observable tool symlinks.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Default paths:\n"
               f"  Config directory: {DEFAULT_CONFIG_DIR}\n"
               f"  MCP servers:      {DEFAULT_CONFIG_DIR / 'mcp_servers.json'}\n"
    )
    
    # Task Execution & Pipelines
    task_group = parser.add_argument_group("Task Execution & Pipelines")
    task_group.add_argument("task", nargs="?", help="Task description or saved task name to execute")
    task_group.add_argument(
        "-t", "--task", dest="task_file", metavar="FILE",
        help="Read the task description from a file",
    )
    task_group.add_argument(
        "-e", "--edit", metavar="TASK",
        help="Edit a saved Task script (shell/python) in your default editor",
    )
    task_group.add_argument(
        "-y", "--yes", action="store_true",
        help="Auto-approve tool symlinks (non-interactive mode)",
    )
    task_group.add_argument(
        "-v", "--verbose", action="count", default=0,
        help="Increase verbosity: -v shows sections/steps, -vv shows all tool output",
    )
    task_group.add_argument(
        "-p", "--preview", action="store_true",
        help="Preview the plan before execution (confirm, edit, retry, or abort)",
    )

    # Model Configuration
    model_group = parser.add_argument_group("Model Configuration")
    model_group.add_argument(
        "-s", "--set", nargs="*", metavar=("KEY", "VALUE"),
        help="Set a model config value (model, url, key), or show current values with no args",
    )
    model_group.add_argument("-m", "--model", nargs="?", const="__list__", help="Override model for this run; with no value, list available models")
    model_group.add_argument("-u", "--url", help="Override url for this run")
    model_group.add_argument("-k", "--key", help="Override API key for this run")
    model_group.add_argument(
        "-T", "--timeout", type=int, default=120,
        help="Timeout for API calls in seconds (default: 120)",
    )
    model_group.add_argument(
        "--curlify", action="store_true",
        help="Print the curl equivalent of the model API call for debugging",
    )

    # Agent Configuration
    agent_group = parser.add_argument_group("Agent & MCP Configuration")
    agent_group.add_argument(
        "-c", "--config-dir", type=Path, default=DEFAULT_CONFIG_DIR,
        help=f"Override default config directory",
    )
    agent_group.add_argument(
        "--mcp_file", type=Path,
        help=f"Override default MCP servers config file",
    )

    # Skill Management (Anthropic protocol)
    skill_group = parser.add_argument_group("Skill Management (Anthropic protocol)")
    skill_group.add_argument(
        "-l", "--skills", nargs="?", const="__all__", metavar="SKILL",
        help="List saved Anthropic skills, or show detail for a specific skill",
    )
    skill_group.add_argument(
        "--skill", metavar="NAME",
        help="Explicitly use a specific Anthropic skill by name, skipping inference",
    )
    skill_group.add_argument(
        "-d", "--delete", metavar="SKILL",
        help="Delete a saved skill so it won't be used and must be re-learned",
    )
    skill_group.add_argument(
        "--clean", action="store_true",
        help="Review and deduplicate similar skills interactively",
    )
    skill_group.add_argument(
        "--import", dest="import_path", metavar="PATH",
        help="Import a skill from a directory, SKILL.md file, or .skill archive",
    )
    skill_group.add_argument(
        "--export", nargs="+", metavar=("SKILL", "PATH"),
        help="Export a skill to a .skill archive; if PATH is omitted, saves to current directory",
    )

    args = parser.parse_args()
    agent = AgentCLI(
        config_dir=args.config_dir,
        auto_yes=args.yes,
        verbose=args.verbose,
        mcp_file=args.mcp_file
    )
    agent._curlify_mode = args.curlify
    agent._timeout = args.timeout
    
    # Handle CLI-only options first (these don't need full agent)
    if args.set is not None:
        if len(args.set) == 0:
            # Show current config values
            agent.show_model_config()
        elif len(args.set) % 2 != 0:
            # Invalid number of arguments, must be pairs
            print("Usage: ac -s [KEY VALUE] ...")
            print("  (no args)  show current values")
            print("  KEY VALUE  set a value (model, url, key)")
            sys.exit(1)
        else:
            # Process multiple key-value pairs
            for i in range(0, len(args.set), 2):
                key = args.set[i]
                value = args.set[i+1]
                agent.set_model_config(key, value)
                print(f"Set {key} = {value}")
        return

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    try:
        # one-shot overrides
        if args.model:
            if args.model == "__list__":
                agent.list_models()
                return
            agent.model_config["model"] = args.model
        if args.url:
            agent.model_config["url"] = args.url
        if args.key:
            agent.model_config["key"] = args.key

        # resolve task
        task = args.task
        if args.task_file:
            path = Path(args.task_file).resolve()
            if not path.exists():
                print(f"Error: task file '{args.task_file}' not found")
                sys.exit(1)
            task = path.read_text().strip()

        if task:
            asyncio.run(agent.execute_task(task, explicit_skill=args.skill, preview=args.preview))
        elif args.skills:
            if args.skills == "__all__":
                agent._print_skills()
            else:
                agent._print_skill_detail(args.skills)
        elif args.edit:
            agent.edit_task(args.edit)
        elif args.delete:
            agent.delete_skill(args.delete)
        elif args.clean:
            agent.clean_skills()
        elif args.import_path:
            agent.import_skill(args.import_path)
        elif args.export:
            skill_name = args.export[0]
            output_path = args.export[1] if len(args.export) > 1 else None
            agent.export_skill(skill_name, output_path)
        else:
            agent.show_status()
    except KeyboardInterrupt:
        print()
        sys.exit(130)


if __name__ == "__main__":
    # Enable ctrl+c to work properly
    import signal
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    main()
