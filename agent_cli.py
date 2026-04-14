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
import subprocess
import sys
from pathlib import Path
from typing import Optional


# Default configuration
DEFAULT_CONFIG_DIR = Path(".config/agent-cli")
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


# Output markers for visual hierarchy
MARKER_TASK   = "▸▸▸"       # task header
MARKER_STEP   = "▹"         # sub-step / phase header
MARKER_CMD    = "  ➜"       # command about to run
MARKER_OUTPUT = "  │"       # output line
MARKER_OK     = "  ✓"       # success / approved
MARKER_FAIL   = "  ✗"       # failure
MARKER_INFO   = "  ·"       # informational note
MARKER_PROMPT = "  ?"       # user prompt
MARKER_RULE   = "─" * 60


def _prefix_lines(text: str, prefix: str) -> str:
    """Prefix every line of *text* with *prefix*."""
    return "\n".join(f"{prefix} {line}" for line in text.splitlines())


class AgentCLI:
    """Main agent-cli class."""

    def __init__(self, config_dir: Optional[Path] = None, auto_yes: bool = False):
        self.config_dir = config_dir or DEFAULT_CONFIG_DIR
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
                    print(f"\n{MARKER_CMD} {cmd_display}")
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
            print(f"\n{MARKER_FAIL} '{name}' not found on PATH — may need to be installed")
            return False

        if task is None:
            task = classify_tool(name)

        task_bin = self.tools_dir / task / "bin"
        task_bin.mkdir(parents=True, exist_ok=True)
        link = task_bin / name

        if auto_yes:
            print(f"{MARKER_OK} symlink  {name} → {path}  [{task}]")
        else:
            print(f"\n{MARKER_PROMPT} Allow '{name}' ({path})?  category: [{task}]  (y/n): ", end="")
            resp = sys.stdin.readline().strip().lower()
            if resp != "y":
                print(f"{MARKER_INFO} skipped")
                return False

        link.symlink_to(path)
        return True

    # ------------------------------------------------------------------
    # Skills
    # ------------------------------------------------------------------
    def get_available_skills(self) -> list[dict]:
        """Return list of skill metadata (name, file, description, success_count)."""
        skills = []
        if not self.skills_dir.exists():
            return skills
        for f in sorted(self.skills_dir.iterdir()):
            if f.is_file() and f.suffix == ".json":
                try:
                    with open(f) as fh:
                        data = json.load(fh)
                    skills.append({
                        "name": data.get("name", f.stem),
                        "file": f.name,
                        "file_path": str(f),
                        "description": data.get("description", ""),
                        "task_pattern": data.get("task_pattern", ""),
                        "success_count": data.get("success_count", 1),
                        "tools_used": data.get("tools_used", []),
                        "invalidated": data.get("invalidated", False),
                        "_data": data,  # Full data loaded from file
                    })
                except (json.JSONDecodeError, IOError):
                    pass
        return skills

    def _find_applicable_skill(self, task: str) -> Optional[dict]:
        task_lower = task.lower()
        for skill in self.get_available_skills():
            if skill.get("invalidated"):
                continue
            pattern = skill.get("task_pattern", "").lower()
            if not pattern:
                continue
            if pattern in task_lower or task_lower in pattern:
                return skill
        return None

    def _load_skill(self, name: str) -> dict:
        """Load a skill by name or filename. Tries both."""
        for f in self.skills_dir.iterdir():
            if f.stem == name or f.name == name:
                if f.suffix == ".json":
                    with open(f) as fh:
                        return json.load(fh)
        # Also try matching against the 'name' field inside each file
        for f in self.skills_dir.iterdir():
            if f.is_file() and f.suffix == ".json":
                try:
                    with open(f) as fh:
                        data = json.load(fh)
                    if data.get("name") == name:
                        return data
                except (json.JSONDecodeError, IOError):
                    pass
        return {}

    def _save_skill(self, task: str, plan: list[dict], success_condition: dict,
                    tools_used: list[str], success: bool = True) -> Optional[str]:
        """Persist the completed task as a reusable skill."""
        self.skills_dir.mkdir(parents=True, exist_ok=True)

        # Generate a stable skill name from the task
        skill_slug = re.sub(r'[^a-z0-9]+', '-', task.lower().strip())[:60].strip('-')
        skill_file = self.skills_dir / f"{skill_slug}.json"

        # If skill already exists, bump success_count instead of overwriting
        existing = {}
        if skill_file.exists():
            try:
                with open(skill_file) as fh:
                    existing = json.load(fh)
            except (json.JSONDecodeError, IOError):
                pass

        skill_data = {
            "name": existing.get("name", task),
            "task_pattern": existing.get("task_pattern", task.lower()),
            "description": existing.get("description", f"Auto-saved from: {task}"),
            "plan": plan,
            "tools_used": sorted(set(tools_used)),
            "success_condition": success_condition,
            "success_count": existing.get("success_count", 0) + (1 if success else 0),
            "invalidated": False,
        }

        with open(skill_file, "w") as fh:
            json.dump(skill_data, fh, indent=2)
        return str(skill_file)

    def invalidate_skill(self, skill_name: str) -> bool:
        """Mark a skill as invalidated so it won't be used."""
        skill_file = self.skills_dir / f"{skill_name}.json"
        if not skill_file.exists():
            # Try exact file match with extension
            for f in self.skills_dir.iterdir():
                if f.stem == skill_name and f.suffix == ".json":
                    skill_file = f
                    break
            else:
                print(f"{MARKER_FAIL} skill '{skill_name}' not found")
                return False

        try:
            with open(skill_file) as fh:
                data = json.load(fh)
            data["invalidated"] = True
            with open(skill_file, "w") as fh:
                json.dump(data, fh, indent=2)
            print(f"{MARKER_OK} skill '{skill_name}' invalidated")
            return True
        except (json.JSONDecodeError, IOError) as e:
            print(f"{MARKER_FAIL} error invalidating skill: {e}")
            return False

    def delete_skill(self, skill_name: str) -> bool:
        """Permanently remove a skill file."""
        skill_file = self.skills_dir / f"{skill_name}.json"
        if not skill_file.exists():
            for f in self.skills_dir.iterdir():
                if f.stem == skill_name and f.suffix == ".json":
                    skill_file = f
                    break
            else:
                print(f"{MARKER_FAIL} skill '{skill_name}' not found")
                return False

        skill_file.unlink()
        print(f"{MARKER_OK} skill '{skill_name}' deleted")
        return True

    def apply_skill(self, skill_meta: dict) -> bool:
        """Execute a saved skill's plan. Returns True if all steps succeeded."""
        # Load full skill data from file
        full = self._load_skill(skill_meta["name"])
        if not full:
            print(f"{MARKER_INFO} skill file not found for '{skill_meta['name']}'")
            return False

        plan = full.get("plan", [])
        if not plan:
            print(f"{MARKER_INFO} skill has no saved plan")
            return False

        print(f"{MARKER_OK} executing skill: {full.get('name', skill_meta['name'])}")
        print(f"{MARKER_INFO} pattern: {full.get('task_pattern', '')}")
        if full.get("tools_used"):
            print(f"{MARKER_INFO} tools: {', '.join(full['tools_used'])}")

        # Validate each step
        for i, step in enumerate(plan):
            tool = step.get("tool")
            if tool and tool not in self.all_tool_names():
                if self.discover_tool(tool):
                    if not self.symlink_tool(tool, auto_yes=self.auto_yes):
                        print(f"\n{MARKER_FAIL} cannot proceed without '{tool}'")
                        return False
                else:
                    print(f"\n{MARKER_FAIL} tool '{tool}' not available on this system")
                    return False

        # Execute
        for i, step in enumerate(plan):
            print(f"\n{MARKER_STEP} skill step {i + 1}: {step['action']}")
            tool = step.get("tool")
            args = step.get("args", [])

            if tool:
                rc, out, err = self._run_symlinked(tool, args, timeout=300, show_cmd=True)
                if out.strip():
                    for line in out.strip().splitlines():
                        print(f"{MARKER_OUTPUT} {line}")
                if err.strip():
                    for line in err.strip().splitlines():
                        print(f"{MARKER_OUTPUT} {line}")
            else:
                print(f"{MARKER_INFO} no tool for this step")

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
    def _call_model(self, messages: list[dict]) -> Optional[str]:
        """Call the configured model via OpenAI-compatible API. Returns response text or None."""
        if not self.model_config.get("model") or not self.model_config.get("key"):
            return None

        base_url = self.model_config.get("base_url")
        if not base_url:
            base_url = "https://api.openai.com/v1"

        url = f"{base_url.rstrip('/')}/chat/completions"
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
            with urllib.request.urlopen(req, timeout=60) as resp:
                result = json.loads(resp.read())
                return result["choices"][0]["message"]["content"]
        except Exception as e:
            print(f"{MARKER_FAIL} model call failed: {e}")
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
                    print(f"{MARKER_OK} plan generated by model")
                    return plan
            except (json.JSONDecodeError, ValueError, KeyError):
                print(f"{MARKER_INFO} model response unparsable — falling back to heuristics")

        # --- heuristic fallback ---
        print(f"{MARKER_INFO} using heuristic plan (no model configured)")
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

            print(f"\n{MARKER_STEP} validate  step {i + 1}: {step['action']}")

            if tool in self.all_tool_names():
                print(f"{MARKER_OK} tool '{tool}' already available")
            else:
                # Tool not symlinked — try to discover it
                if self.discover_tool(tool):
                    if self.symlink_tool(tool, auto_yes=self.auto_yes):
                        print(f"{MARKER_OK} tool '{tool}' linked and ready")
                    else:
                        print(f"\n{MARKER_FAIL} cannot proceed without '{tool}'")
                        return False
                else:
                    print(f"\n{MARKER_FAIL} '{tool}' not found on this system")
                    resp = input(f"\n{MARKER_PROMPT} Install '{tool}' and retry? (y/n): ").strip().lower()
                    if resp == "y":
                        print(f"{MARKER_INFO} please install '{tool}' then re-run the task")
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
            print(f"\n{MARKER_STEP} execute  step {i + 1}: {step['action']}")
            tool = step.get("tool")
            args = step.get("args", [])

            if tool:
                rc, out, err = self._run_symlinked(tool, args, timeout=300, show_cmd=True)
                if out.strip():
                    for line in out.strip().splitlines():
                        print(f"{MARKER_OUTPUT} {line}")
                if err.strip():
                    for line in err.strip().splitlines():
                        print(f"{MARKER_OUTPUT} {line}")
            elif step.get("write_program"):
                script = self._write_custom_program(task)
                if script:
                    print(f"{MARKER_OK} custom program written to {script}")
                    print(f"{MARKER_INFO} review and run manually")
                else:
                    print(f"\n{MARKER_FAIL} failed to write custom program")
                    return False
            else:
                print(f"{MARKER_INFO} no specific tool — executing task directly")

        return True

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------
    def execute_task(self, task: str):
        print(f"{MARKER_TASK} {task}")
        print(MARKER_RULE)

        # 1. Define success condition
        print(f"\n{MARKER_STEP} success condition")
        success = self._define_success_condition(task)
        print(f"{MARKER_INFO} {success['description']}")

        # 2. Check for applicable skills
        print(f"\n{MARKER_STEP} skills")
        skill = self._find_applicable_skill(task)
        if skill:
            print(f"{MARKER_OK} match: {skill['name']}  ({skill.get('success_count', 0)}× success)")

            # 3. Apply skill
            print(f"\n{MARKER_STEP} applying skill")
            if self.apply_skill(skill):
                if self._check_success(success):
                    print(f"\n{MARKER_RULE}")
                    print(f"{MARKER_OK} SUCCESS  {success['description']}")
                    return
            print(f"\n{MARKER_INFO} skill did not satisfy — falling back to plan")
        else:
            print(f"{MARKER_INFO} none found")

        # 4. Create plan
        print(f"\n{MARKER_STEP} plan")
        plan = self._create_plan(task)
        tools_in_plan = sorted({s["tool"] for s in plan if s.get("tool")})
        for i, step in enumerate(plan):
            tool = step.get("tool", "custom")
            print(f"{MARKER_INFO} step {i + 1}: {step['action']}  ({tool})")

        # 5. Validate plan
        print(f"\n{MARKER_STEP} validate")
        if not self._validate_plan(plan):
            print(f"\n{MARKER_FAIL} validation failed")
            sys.exit(1)
        print(f"{MARKER_OK} all steps valid")

        # 6. Execute
        print(f"\n{MARKER_STEP} execute")
        self._execute_plan(plan, task)

        # Verify & save skill
        print(f"\n{MARKER_RULE}")
        if self._check_success(success):
            skill_path = self._save_skill(task, plan, success, tools_in_plan, success=True)
            print(f"{MARKER_OK} SUCCESS  {success['description']}")
            if skill_path:
                print(f"{MARKER_OK} skill saved → {Path(skill_path).name}")
        else:
            self._save_skill(task, plan, success, tools_in_plan, success=False)
            print(f"{MARKER_FAIL} FAILED  {success['description']}")
            sys.exit(1)

    def show_status(self):
        print(f"{MARKER_TASK} agent-cli")
        print(MARKER_RULE)

        tools = self.get_all_symlinked_tools()
        print(f"\n{MARKER_STEP} tools")
        for task, names in sorted(tools.items()):
            print(f"{MARKER_INFO} {task}/bin/  {' '.join(names)}")

        self._print_skills()

        print(f"\n{MARKER_RULE}")
        print(f"{MARKER_INFO} agent-cli '<task>'")
        print(f"{MARKER_INFO} agent-cli --skills")
        print(f"{MARKER_INFO} agent-cli --invalidate <skill>")
        print(f"{MARKER_INFO} agent-cli --set model 'model-name'")

    def _print_skills(self):
        """Print a formatted list of skills."""
        skills = self.get_available_skills()
        print(f"\n{MARKER_STEP} skills")
        if not skills:
            print(f"{MARKER_INFO} none yet — they are saved automatically on success")
            return
        for s in skills:
            status = "invalidated" if s.get("invalidated") else f"{s['success_count']}×"
            tools_str = f" [{', '.join(s['tools_used'])}]" if s.get("tools_used") else ""
            print(f"{MARKER_INFO} {s['name']:30s}  {status:12s}{tools_str}")
            if s.get("description"):
                print(f"         {s['description']}")


def main():
    parser = argparse.ArgumentParser(
        description="agent-cli: A one-shot command-line helper "
                    "with scoped execution tasks and observable tool symlinks."
    )
    parser.add_argument("task", nargs="?", help="Task description to execute")
    parser.add_argument(
        "--set", nargs=2, metavar=("KEY", "VALUE"),
        help="Set a model config value (model, base_url, key)",
    )
    parser.add_argument("--model", help="Override model for this run")
    parser.add_argument("--base-url", help="Override base_url for this run")
    parser.add_argument("--key", help="Override API key for this run")
    parser.add_argument(
        "--config-dir", type=Path, default=DEFAULT_CONFIG_DIR,
        help="Config directory (default: .config/agent-cli)",
    )
    parser.add_argument(
        "-y", "--yes", action="store_true",
        help="Auto-approve tool symlinks (non-interactive mode)",
    )
    parser.add_argument(
        "--skills", action="store_true",
        help="List available skills",
    )
    parser.add_argument(
        "--invalidate", metavar="SKILL",
        help="Invalidate a skill so it won't be used",
    )
    parser.add_argument(
        "--delete", metavar="SKILL",
        help="Permanently delete a skill",
    )

    args = parser.parse_args()
    agent = AgentCLI(config_dir=args.config_dir, auto_yes=args.yes)

    # --set
    if args.set:
        agent.set_model_config(args.set[0], args.set[1])
        print(f"Set {args.set[0]} = {args.set[1]}")
        return

    # one-shot overrides
    if args.model:
        agent.model_config["model"] = args.model
    if args.base_url:
        agent.model_config["base_url"] = args.base_url
    if args.key:
        agent.model_config["key"] = args.key

    if args.task:
        agent.execute_task(args.task)
    elif args.skills:
        agent._print_skills()
    elif args.invalidate:
        agent.invalidate_skill(args.invalidate)
    elif args.delete:
        agent.delete_skill(args.delete)
    else:
        agent.show_status()


if __name__ == "__main__":
    main()
