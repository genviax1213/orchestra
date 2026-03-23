from __future__ import annotations

import argparse
import curses
import json
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


CONFIG_DIRNAME = ".orchestra"
CONFIG_FILENAME = "state.json"


class OrchestraError(RuntimeError):
    pass


HELP_TEXT = "j/k or arrows move | l launch | a launch-all | m merge | r refresh | q quit"


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def run(cmd: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        check=check,
    )


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["git", *args], cwd=repo, check=check)


def ensure_git_repo(repo: Path) -> None:
    result = git(repo, "rev-parse", "--show-toplevel", check=False)
    if result.returncode != 0:
        raise OrchestraError(f"{repo} is not a git repository")


def find_state_path(start: Path) -> Path:
    current = start.resolve()
    for candidate in [current, *current.parents]:
        state_path = candidate / CONFIG_DIRNAME / CONFIG_FILENAME
        if state_path.exists():
            return state_path
    raise OrchestraError("could not find .orchestra/state.json from the current directory")


def load_state(state_path: Path) -> dict[str, Any]:
    with state_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_state(state_path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)
        handle.write("\n")


def default_worktree_root(repo: Path) -> Path:
    return repo.parent / f"{repo.name}-worktrees"


def default_log_root(repo: Path) -> Path:
    return repo / CONFIG_DIRNAME / "logs"


def normalize_path(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve())


def require_task(state: dict[str, Any], task_name: str) -> dict[str, Any]:
    for task in state["tasks"]:
        if task["name"] == task_name:
            return task
    raise OrchestraError(f"task {task_name!r} not found")


def command_for_agent(agent: str, worktree_path: Path, prompt: str) -> str:
    quoted_prompt = json.dumps(prompt)
    templates = {
        "codex": f"cd {json.dumps(str(worktree_path))} && codex {quoted_prompt}",
        "claude": f"cd {json.dumps(str(worktree_path))} && claude {quoted_prompt}",
        "shell": f"cd {json.dumps(str(worktree_path))} && bash -lc {quoted_prompt}",
    }
    return templates.get(agent, f"cd {json.dumps(str(worktree_path))} && {agent} {quoted_prompt}")


def session_exists(name: str) -> bool:
    if shutil.which("tmux") is None:
        return False
    result = run(["tmux", "has-session", "-t", name], check=False)
    return result.returncode == 0


def repo_dirty(repo: Path) -> bool:
    result = git(repo, "status", "--porcelain", check=False)
    return bool(result.stdout.strip())


def worktree_state(task: dict[str, Any]) -> str:
    return "present" if Path(task["worktree_path"]).exists() else "missing"


def task_session_state(task: dict[str, Any]) -> str:
    if shutil.which("tmux") is None:
        return "n/a"
    return "running" if session_exists(task["session_name"]) else "stopped"


def task_snapshot(task: dict[str, Any]) -> dict[str, str]:
    return {
        "name": task["name"],
        "status": task["status"],
        "branch": task["branch"],
        "agent": task["agent"],
        "session": task_session_state(task),
        "worktree": worktree_state(task),
    }


def load_task_prompt(task: dict[str, Any]) -> str:
    prompt_file = task.get("prompt_file")
    if prompt_file:
        prompt_path = Path(prompt_file)
        if not prompt_path.exists():
            raise OrchestraError(f"prompt file not found: {prompt_path}")
        return prompt_path.read_text(encoding="utf-8").strip()
    prompt = task.get("prompt", "").strip()
    if not prompt:
        raise OrchestraError(f"task {task['name']!r} has no prompt or prompt file")
    return prompt


def task_log_path(ctx: "Context", task: dict[str, Any]) -> Path:
    log_path = task.get("log_path")
    if log_path:
        return Path(log_path)
    log_root = Path(ctx.state.get("log_root", default_log_root(ctx.repo_path)))
    return log_root / f"{task['name']}.log"


def tail_text(path: Path, lines: int) -> str:
    if not path.exists():
        raise OrchestraError(f"log file not found: {path}")
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if lines <= 0:
        return ""
    return "\n".join(content[-lines:])


@dataclass
class Context:
    state_path: Path
    state: dict[str, Any]

    @property
    def repo_path(self) -> Path:
        return Path(self.state["repo_path"])


def load_context(repo: str | None) -> Context:
    if repo:
        repo_path = Path(repo).expanduser().resolve()
        state_path = repo_path / CONFIG_DIRNAME / CONFIG_FILENAME
        if not state_path.exists():
            raise OrchestraError(f"missing state file at {state_path}")
    else:
        state_path = find_state_path(Path.cwd())
    return Context(state_path=state_path, state=load_state(state_path))


def cmd_init(args: argparse.Namespace) -> int:
    repo = Path(args.repo).expanduser().resolve()
    ensure_git_repo(repo)
    state_path = repo / CONFIG_DIRNAME / CONFIG_FILENAME
    if state_path.exists() and not args.force:
        raise OrchestraError(f"state already exists at {state_path}")

    worktree_root = Path(args.worktree_root).expanduser().resolve() if args.worktree_root else default_worktree_root(repo)
    state = {
        "version": 1,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "repo_path": normalize_path(repo),
        "base_branch": args.base_branch,
        "integration_branch": args.integration_branch or args.base_branch,
        "session_prefix": args.session_prefix,
        "worktree_root": normalize_path(worktree_root),
        "log_root": normalize_path(default_log_root(repo)),
        "tasks": [],
    }
    save_state(state_path, state)
    print(f"initialized orchestra state at {state_path}")
    return 0


def cmd_task_add(args: argparse.Namespace) -> int:
    ctx = load_context(args.repo)
    repo = ctx.repo_path
    ensure_git_repo(repo)

    state = ctx.state
    if any(task["name"] == args.name for task in state["tasks"]):
        raise OrchestraError(f"task {args.name!r} already exists")

    worktree_root = Path(state["worktree_root"])
    worktree_path = Path(args.worktree).expanduser().resolve() if args.worktree else worktree_root / args.branch
    session_name = f"{state['session_prefix']}-{args.name}"
    prompt_file = normalize_path(args.prompt_file) if args.prompt_file else None
    task = {
        "name": args.name,
        "branch": args.branch,
        "agent": args.agent,
        "prompt": args.prompt,
        "prompt_file": prompt_file,
        "files": args.files or [],
        "status": "planned",
        "session_name": session_name,
        "worktree_path": normalize_path(worktree_path),
        "log_path": normalize_path(Path(state["log_root"]) / f"{args.name}.log"),
        "created_at": utc_now(),
        "last_launched_at": None,
        "last_merged_at": None,
    }
    state["tasks"].append(task)
    save_state(ctx.state_path, state)
    print(f"added task {args.name} -> {args.branch}")
    return 0


def cmd_task_list(args: argparse.Namespace) -> int:
    ctx = load_context(args.repo)
    tasks = ctx.state["tasks"]
    if not tasks:
        print("no tasks")
        return 0
    for task in tasks:
        prompt_source = task.get("prompt_file") or "inline"
        print(
            f"{task['name']}\t{task['status']}\t{task['branch']}\t{task['agent']}\t"
            f"{task['worktree_path']}\t{prompt_source}"
        )
    return 0


def ensure_worktree(ctx: Context, task: dict[str, Any]) -> None:
    repo = ctx.repo_path
    worktree_path = Path(task["worktree_path"])
    if worktree_path.exists() and (worktree_path / ".git").exists():
        return

    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    result = git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{task['branch']}", check=False)
    if result.returncode == 0:
        git(repo, "worktree", "add", str(worktree_path), task["branch"])
    else:
        git(repo, "worktree", "add", "-b", task["branch"], str(worktree_path), ctx.state["base_branch"])


def launch_task(ctx: Context, task: dict[str, Any], *, dry_run: bool = False, allow_existing: bool = False) -> str:
    ensure_worktree(ctx, task)

    worktree_path = Path(task["worktree_path"])
    prompt = load_task_prompt(task)
    command = command_for_agent(task["agent"], worktree_path, prompt)
    log_path = task_log_path(ctx, task)

    if dry_run:
        return f"{task['name']}\tDRY-RUN\t{command}"

    if shutil.which("tmux") is None:
        raise OrchestraError("tmux is required for launch")

    if session_exists(task["session_name"]):
        if allow_existing:
            return f"{task['name']}\tSKIPPED\tsession {task['session_name']} already exists"
        raise OrchestraError(f"tmux session {task['session_name']!r} already exists")

    log_path.parent.mkdir(parents=True, exist_ok=True)
    run(["tmux", "new-session", "-d", "-s", task["session_name"], command])
    pipe_command = f"cat >> {shlex.quote(str(log_path))}"
    run(["tmux", "pipe-pane", "-t", task["session_name"], "-o", pipe_command])
    task["status"] = "launched"
    task["last_launched_at"] = utc_now()
    return f"{task['name']}\tLAUNCHED\t{task['session_name']}\t{log_path}"


def cmd_launch(args: argparse.Namespace) -> int:
    ctx = load_context(args.repo)
    task = require_task(ctx.state, args.name)
    result = launch_task(ctx, task, dry_run=args.dry_run)
    if not args.dry_run:
        save_state(ctx.state_path, ctx.state)
    print(result)
    return 0


def cmd_launch_all(args: argparse.Namespace) -> int:
    ctx = load_context(args.repo)
    outputs: list[str] = []
    state_changed = False

    for task in ctx.state["tasks"]:
        if args.only_status and task["status"] not in args.only_status:
            continue
        result = launch_task(ctx, task, dry_run=args.dry_run, allow_existing=True)
        outputs.append(result)
        if "\tLAUNCHED\t" in result:
            state_changed = True

    if state_changed:
        save_state(ctx.state_path, ctx.state)

    if not outputs:
        print("no matching tasks")
        return 0

    for line in outputs:
        print(line)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    ctx = load_context(args.repo)
    tasks = ctx.state["tasks"]
    if args.name:
        tasks = [require_task(ctx.state, args.name)]

    for task in tasks:
        session_state = task_session_state(task)
        current_worktree_state = worktree_state(task)
        print(
            f"{task['name']}: status={task['status']} branch={task['branch']} "
            f"worktree={current_worktree_state} session={session_state}"
        )
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    ctx = load_context(args.repo)
    repo = ctx.repo_path
    task = require_task(ctx.state, args.name)
    target = args.target or ctx.state["integration_branch"]

    if repo_dirty(repo):
        raise OrchestraError(f"repo {repo} has uncommitted changes; merge aborted")

    merge_cmd = ["git", "merge"]
    if args.strategy == "ff-only":
        merge_cmd.append("--ff-only")
    elif args.strategy == "squash":
        merge_cmd.append("--squash")
    else:
        merge_cmd.append("--no-ff")
    merge_cmd.append(task["branch"])

    if args.dry_run:
        print(f"git -C {repo} checkout {target}")
        print(f"git -C {repo} {' '.join(merge_cmd[1:])}")
        return 0

    git(repo, "checkout", target)
    run(merge_cmd, cwd=repo)
    task["status"] = "merged"
    task["last_merged_at"] = utc_now()
    save_state(ctx.state_path, ctx.state)
    print(f"merged {task['branch']} into {target}")
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    ctx = load_context(args.repo)
    task = require_task(ctx.state, args.name)
    log_path = task_log_path(ctx, task)
    if args.path:
        print(log_path)
        return 0
    print(tail_text(log_path, args.lines))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    checks = {
        "python3": shutil.which("python3"),
        "git": shutil.which("git"),
        "tmux": shutil.which("tmux"),
        "codex": shutil.which("codex"),
        "claude": shutil.which("claude"),
    }
    for name, path in checks.items():
        status = path if path else "missing"
        print(f"{name}\t{status}")

    if args.repo:
        ctx = load_context(args.repo)
        ensure_git_repo(ctx.repo_path)
        print(f"repo\t{ctx.repo_path}")
        print(f"state\t{ctx.state_path}")
    return 0


def draw_tui(stdscr: Any, ctx: Context, merge_strategy: str) -> int:
    curses.curs_set(0)
    stdscr.nodelay(False)
    stdscr.keypad(True)

    selected = 0
    top = 0
    message = "ready"

    while True:
        ctx.state = load_state(ctx.state_path)
        tasks = ctx.state["tasks"]
        if selected >= len(tasks):
            selected = max(0, len(tasks) - 1)

        height, width = stdscr.getmaxyx()
        stdscr.erase()
        title = f"orchestra tui | repo={ctx.repo_path.name} | tasks={len(tasks)}"
        stdscr.addnstr(0, 0, title, max(1, width - 1), curses.A_BOLD)
        stdscr.addnstr(1, 0, HELP_TEXT, max(1, width - 1))

        columns = [
            ("task", 18),
            ("status", 10),
            ("session", 10),
            ("agent", 8),
            ("branch", max(10, width - 52)),
        ]

        x = 0
        for header, col_width in columns:
            stdscr.addnstr(3, x, header.ljust(col_width), col_width, curses.A_UNDERLINE)
            x += col_width + 1

        visible_rows = max(1, height - 6)
        if selected < top:
            top = selected
        if selected >= top + visible_rows:
            top = selected - visible_rows + 1

        if not tasks:
            stdscr.addnstr(5, 0, "No tasks. Add tasks from the CLI first.", max(1, width - 1))
        else:
            for row_index, task in enumerate(tasks[top : top + visible_rows], start=4):
                snapshot = task_snapshot(task)
                row = [
                    snapshot["name"][: columns[0][1]].ljust(columns[0][1]),
                    snapshot["status"][: columns[1][1]].ljust(columns[1][1]),
                    snapshot["session"][: columns[2][1]].ljust(columns[2][1]),
                    snapshot["agent"][: columns[3][1]].ljust(columns[3][1]),
                    snapshot["branch"][: columns[4][1]].ljust(columns[4][1]),
                ]
                attr = curses.A_REVERSE if (top + row_index - 4) == selected else curses.A_NORMAL
                x = 0
                for cell, (_, col_width) in zip(row, columns, strict=True):
                    stdscr.addnstr(row_index, x, cell, col_width, attr)
                    x += col_width + 1

            task = tasks[selected]
            detail = (
                f"selected={task['name']} worktree={worktree_state(task)} "
                f"log={task_log_path(ctx, task)}"
            )
            stdscr.addnstr(height - 2, 0, detail, max(1, width - 1))

        stdscr.addnstr(height - 1, 0, message, max(1, width - 1), curses.A_DIM)
        stdscr.refresh()

        key = stdscr.getch()
        if key in (ord("q"), 27):
            return 0
        if key in (curses.KEY_DOWN, ord("j")) and tasks:
            selected = min(len(tasks) - 1, selected + 1)
            continue
        if key in (curses.KEY_UP, ord("k")) and tasks:
            selected = max(0, selected - 1)
            continue
        if key == ord("r"):
            message = "refreshed"
            continue
        if key == ord("a"):
            try:
                outputs = []
                state_changed = False
                for task in tasks:
                    result = launch_task(ctx, task, dry_run=False, allow_existing=True)
                    outputs.append(result)
                    if "\tLAUNCHED\t" in result:
                        state_changed = True
                if state_changed:
                    save_state(ctx.state_path, ctx.state)
                message = outputs[-1] if outputs else "no tasks"
            except (OrchestraError, subprocess.CalledProcessError) as exc:
                message = str(exc)
            continue
        if not tasks:
            message = "no tasks to operate on"
            continue
        if key == ord("l"):
            try:
                result = launch_task(ctx, tasks[selected], dry_run=False, allow_existing=True)
                if "\tLAUNCHED\t" in result:
                    save_state(ctx.state_path, ctx.state)
                message = result
            except (OrchestraError, subprocess.CalledProcessError) as exc:
                message = str(exc)
            continue
        if key == ord("m"):
            task = tasks[selected]
            try:
                target = ctx.state["integration_branch"]
                if repo_dirty(ctx.repo_path):
                    raise OrchestraError(f"repo {ctx.repo_path} has uncommitted changes; merge aborted")
                git(ctx.repo_path, "checkout", target)
                merge_cmd = ["git", "merge"]
                if merge_strategy == "ff-only":
                    merge_cmd.append("--ff-only")
                elif merge_strategy == "squash":
                    merge_cmd.append("--squash")
                else:
                    merge_cmd.append("--no-ff")
                merge_cmd.append(task["branch"])
                run(merge_cmd, cwd=ctx.repo_path)
                task["status"] = "merged"
                task["last_merged_at"] = utc_now()
                save_state(ctx.state_path, ctx.state)
                message = f"merged {task['branch']} into {target}"
            except (OrchestraError, subprocess.CalledProcessError) as exc:
                message = str(exc)
            continue


def cmd_tui(args: argparse.Namespace) -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise OrchestraError("tui requires an interactive terminal")
    ctx = load_context(args.repo)
    return curses.wrapper(draw_tui, ctx, args.merge_strategy)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orchestra", description="Minimal multi-agent CLI orchestrator")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Initialize orchestra state for a repo")
    init_parser.add_argument("repo", help="Path to the git repository to orchestrate")
    init_parser.add_argument("--base-branch", default="main")
    init_parser.add_argument("--integration-branch")
    init_parser.add_argument("--worktree-root")
    init_parser.add_argument("--session-prefix", default="orchestra")
    init_parser.add_argument("--force", action="store_true")
    init_parser.set_defaults(func=cmd_init)

    task_parser = subparsers.add_parser("task", help="Manage tasks")
    task_subparsers = task_parser.add_subparsers(dest="task_command", required=True)

    task_add = task_subparsers.add_parser("add", help="Add a task")
    task_add.add_argument("name")
    task_add.add_argument("--branch", required=True)
    task_add.add_argument("--agent", required=True, help="codex, claude, shell, or a custom CLI command")
    prompt_group = task_add.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument("--prompt")
    prompt_group.add_argument("--prompt-file")
    task_add.add_argument("--files", nargs="*")
    task_add.add_argument("--worktree")
    task_add.add_argument("--repo")
    task_add.set_defaults(func=cmd_task_add)

    task_list = task_subparsers.add_parser("list", help="List tasks")
    task_list.add_argument("--repo")
    task_list.set_defaults(func=cmd_task_list)

    launch_parser = subparsers.add_parser("launch", help="Create worktree and launch a task session")
    launch_parser.add_argument("name")
    launch_parser.add_argument("--repo")
    launch_parser.add_argument("--dry-run", action="store_true")
    launch_parser.set_defaults(func=cmd_launch)

    launch_all_parser = subparsers.add_parser("launch-all", help="Launch all matching task sessions")
    launch_all_parser.add_argument("--repo")
    launch_all_parser.add_argument("--dry-run", action="store_true")
    launch_all_parser.add_argument(
        "--only-status",
        nargs="+",
        choices=["planned", "launched", "merged"],
        help="Only launch tasks currently in one of these manifest statuses",
    )
    launch_all_parser.set_defaults(func=cmd_launch_all)

    status_parser = subparsers.add_parser("status", help="Show task status")
    status_parser.add_argument("name", nargs="?")
    status_parser.add_argument("--repo")
    status_parser.set_defaults(func=cmd_status)

    merge_parser = subparsers.add_parser("merge", help="Merge a completed task branch")
    merge_parser.add_argument("name")
    merge_parser.add_argument("--repo")
    merge_parser.add_argument("--target")
    merge_parser.add_argument("--strategy", choices=["no-ff", "ff-only", "squash"], default="no-ff")
    merge_parser.add_argument("--dry-run", action="store_true")
    merge_parser.set_defaults(func=cmd_merge)

    logs_parser = subparsers.add_parser("logs", help="Show the log for a task")
    logs_parser.add_argument("name")
    logs_parser.add_argument("--repo")
    logs_parser.add_argument("--lines", type=int, default=40)
    logs_parser.add_argument("--path", action="store_true")
    logs_parser.set_defaults(func=cmd_logs)

    doctor_parser = subparsers.add_parser("doctor", help="Check external dependencies")
    doctor_parser.add_argument("--repo")
    doctor_parser.set_defaults(func=cmd_doctor)

    tui_parser = subparsers.add_parser("tui", help="Open the terminal dashboard")
    tui_parser.add_argument("--repo")
    tui_parser.add_argument("--merge-strategy", choices=["no-ff", "ff-only", "squash"], default="no-ff")
    tui_parser.set_defaults(func=cmd_tui)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except OrchestraError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(exc.stderr.strip() or str(exc), file=sys.stderr)
        return exc.returncode or 1


if __name__ == "__main__":
    raise SystemExit(main())
