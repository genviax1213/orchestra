# orchestra

`orchestra` is a thin orchestrator for running multiple Codex CLI or Claude CLI workers against the same repository with isolation by `git worktree`.

It is intentionally small:

- one repo manifest
- one branch and worktree per task
- one tmux session per worker
- explicit operator-driven merge

## Interface

The CLI is the interface. Version `0.1.0` supports:

```bash
orchestra init /path/to/repo --base-branch main
orchestra task add auth-ui --repo /path/to/repo \
  --branch feat/auth-ui \
  --agent codex \
  --prompt "Implement the auth UI in src/auth. Run targeted tests before stopping."

orchestra task add billing-api --repo /path/to/repo \
  --branch feat/billing-api \
  --agent claude \
  --prompt-file /path/to/prompts/billing-api.md

orchestra task list --repo /path/to/repo
orchestra launch auth-ui --repo /path/to/repo
orchestra launch billing-api --repo /path/to/repo
orchestra launch-all --repo /path/to/repo
orchestra status --repo /path/to/repo
orchestra logs billing-api --repo /path/to/repo --lines 100
orchestra tui --repo /path/to/repo
orchestra merge auth-ui --repo /path/to/repo --strategy no-ff
orchestra doctor --repo /path/to/repo
```

## Operator Workflow

1. Run `orchestra init` once inside the target repository.
2. Add one task per branch with a tight prompt and clear ownership.
3. Launch tasks into tmux-backed sessions.
4. Use `orchestra launch-all` when you want the orchestrator to start every planned worker in one pass.
5. Monitor with `orchestra status`.
6. Review each branch manually.
7. Merge with `orchestra merge` only after verification.

## Repo State

`orchestra init` creates `.orchestra/state.json` inside the target repo.

That state file tracks:

- repo path
- base branch
- integration branch
- worktree root
- session prefix
- task definitions
- launch and merge timestamps

Worktrees default to a sibling directory:

```text
/repos/my-app
/repos/my-app-worktrees/feat-auth-ui
/repos/my-app-worktrees/feat-billing-api
```

## Command Semantics

`task add`

- registers a branch, agent type, prompt or prompt file, and optional file ownership hints
- does not create a worktree yet

`launch`

- creates the task worktree if missing
- creates the branch from the configured base branch if needed
- starts a detached tmux session named `<session-prefix>-<task-name>`
- pipes pane output into the task log file under `.orchestra/logs/`

`status`

- shows manifest status plus worktree presence and tmux session state

`launch-all`

- walks all tasks in the manifest
- creates missing worktrees
- launches sessions for tasks that are not already running
- skips existing tmux sessions instead of failing the full batch
- supports `--only-status planned` to launch only untouched tasks

`merge`

- checks that the orchestrated repo is clean before merging
- merges the task branch into the integration branch
- supports `--strategy no-ff`, `ff-only`, or `squash`

`logs`

- prints the latest lines from a task log file
- supports `--path` to show the log file location only

`tui`

- opens an interactive terminal dashboard
- shows task, status, session state, agent, and branch
- lets you launch one task, launch all tasks, refresh state, and merge the selected task
- requires an interactive terminal

`doctor`

- checks whether `python3`, `git`, `tmux`, `codex`, and `claude` are on `PATH`

## Why This Interface

This keeps the orchestrator readable and operationally safe:

- no shared checkout between workers
- no hidden merge automation
- no framework lock-in
- no dependency on one vendor CLI

## Prompt Files And Logs

Prompt files are useful when a worker brief is long or changes over time:

```bash
./bin/orchestra task add api-cleanup \
  --repo /path/to/repo \
  --branch feat/api-cleanup \
  --agent codex \
  --prompt-file /path/to/repo/.orchestra/prompts/api-cleanup.md
```

For utility runs and smoke tests, `shell` runs the prompt as `bash -lc` inside the task worktree:

```bash
./bin/orchestra task add smoke \
  --repo /path/to/repo \
  --branch chore/smoke \
  --agent shell \
  --prompt "printf 'smoke ok\n'"
```

Task logs are written to `.orchestra/logs/<task-name>.log`:

```bash
./bin/orchestra logs smoke --repo /path/to/repo
./bin/orchestra logs smoke --repo /path/to/repo --path
```

## TUI Controls

Inside `orchestra tui`:

- `j` or down arrow: move down
- `k` or up arrow: move up
- `l`: launch selected task
- `a`: launch all tasks
- `m`: merge selected task into the integration branch
- `r`: refresh
- `q`: quit

## Development

```bash
cd /mnt/rll/projects/orchestra
python3 -m venv .venv
./.venv/bin/python -m pip install -e .
./.venv/bin/orchestra --help
```

Or with the included make target:

```bash
cd /mnt/rll/projects/orchestra
make install
./bin/orchestra --help
```

The wrapper at [`bin/orchestra`](/mnt/rll/projects/orchestra/bin/orchestra) delegates to `./.venv/bin/orchestra`, so inside this repo you can use:

```bash
cd /mnt/rll/projects/orchestra
./bin/orchestra --help
```

If you want `orchestra` available from anywhere, add this project `bin/` directory to `PATH`:

```bash
export PATH="/mnt/rll/projects/orchestra/bin:$PATH"
orchestra --help
```

For source-tree execution without installing:

```bash
cd /mnt/rll/projects/orchestra
PYTHONPATH=src python3 -m orchestra --help
PYTHONPATH=src python3 -m unittest discover -s tests
```
