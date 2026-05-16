# Alluvium

Alluvium is a local-first task inbox daemon for running coding agents concurrently.

You drop free-form folders or bare files into `tasks/inbox/`. Alluvium assigns each item a unique task ID, runs an agent in an isolated Git worktree/branch, stores task-local artifacts under `.agent/`, and serially integrates any durable repository changes.

The CLI is `alluvium`. The PyPI distribution is `alluvium-swarm` because the bare `alluvium` package name is already taken.

## Quick start

Install:

```bash
uv tool install alluvium-swarm
```

Create a workspace:

```bash
mkdir my-alluvium
cd my-alluvium
alluvium init
```

Start the daemon in the background:

```bash
alluvium daemon
```

Drop work into the inbox:

```bash
echo "Please summarize this." > tasks/inbox/request.txt
```

Check progress:

```bash
alluvium status
find tasks -maxdepth 3 -type f | sort
```

Stop or reload:

```bash
alluvium reload        # reload config.toml
alluvium stop-daemon   # graceful stop
```

For debugging instead of background mode:

```bash
alluvium daemon --foreground
```

## What gets created

```text
my-alluvium/
  config.toml
  tasks/
    inbox/          # public drop zone: folders or bare files
    running/        # currently being worked
    needs_revision/ # integrator sent task back to worker for amendment
    needs_human/    # blocked on clarification/approval/manual intervention
    done/           # worker finished; integration status is in .agent/integration.json
    failed/         # worker failed
    dead_letter/    # reserved for invalid/repeatedly failed tasks
  repo/             # durable Git repo: code, docs, knowledge, skills, tests, etc.
  worktrees/        # one Git worktree per active task
  logs/
  .alluvium/        # daemon pid/lock files
```

A bare file is wrapped into a task folder automatically:

```text
tasks/inbox/report.pdf
  ↓
tasks/running/20260516T101530Z-report.pdf-c771aa/
  report.pdf
  .agent/
```

A dropped folder keeps its contents but receives a unique internal name:

```text
tasks/inbox/acme-contract/
  contract.pdf
  request.md
  ↓
tasks/running/20260516T101530Z-acme-contract-a8f2c1/
  contract.pdf
  request.md
  .agent/
```

`.agent/` is reserved runtime state. If a producer supplies `.agent/` in an inbox item, Alluvium quarantines it and creates a fresh trusted `.agent/` subtree.

## Lifecycle

```text
tasks/inbox/
  ↓
tasks/running/
  ↓
tasks/done/
  ↓ integrator reviews branch
      ├── noop
      ├── merged
      ├── needs_revision → same task/branch is re-run and amended
      └── needs_human
```

Workers may run concurrently. Integration into `repo/main` is serialized.

`done/` means the worker completed. It does **not** necessarily mean the branch is already merged. Integration state is stored in:

```text
.agent/integration.json
```

Examples:

```json
{"status": "noop", "has_repo_changes": false}
```

```json
{"status": "merged", "has_repo_changes": true, "merge_commit": "..."}
```

```json
{"status": "needs_revision", "reason": "integration tests failed"}
```

When revision is needed, the integrator writes:

```text
.agent/revision_request.md
```

and moves the task to `tasks/needs_revision/`. The coordinator re-runs a worker on the same task ID, branch, and worktree so it can amend the branch.

## Configure a real coding agent

The default worker is a deterministic built-in inventory agent. It is useful for smoke tests, but it does not reason or edit meaningfully.

Edit `config.toml`:

```toml
[agent]
# Placeholders: {task_id}, {task_dir}, {agent_dir}, {worktree}, {branch}, {prompt_file}
command = ["your-coding-agent", "--cwd", "{worktree}", "--prompt-file", "{prompt_file}"]
timeout_seconds = 3600
```

Alluvium also passes environment variables to the worker:

```text
ALLUVIUM_TASK_ID
ALLUVIUM_TASK_DIR
ALLUVIUM_AGENT_DIR
ALLUVIUM_WORKTREE
ALLUVIUM_BRANCH
ALLUVIUM_PROMPT_FILE
```

The generated prompt asks the worker to:

- infer the task from the free-form folder,
- write `.agent/understanding.md`, `.agent/plan.md`, `.agent/result.md`, and `.agent/result.json`,
- put task artifacts under `.agent/outputs/`,
- commit useful durable repo changes on the task branch,
- request human help by writing `.agent/needs_human.md`,
- address `.agent/revision_request.md` when returned by the integrator,
- log external effects under `.agent/effects/ledger.jsonl`.

### Example: use pi as the worker

```toml
[agent]
command = [
  "pi",
  "--model", "gpt5/gpt-5.4:high",
  "--no-session",
  "-p", "@{prompt_file}"
]
timeout_seconds = 3600
```

Pi runs from the task worktree. The prompt file includes the absolute task folder path, so pi can write `.agent/` outputs and edit the durable repo.

## Useful commands

```bash
alluvium init [path]                 # initialize workspace
alluvium daemon                      # start background daemon
alluvium daemon --foreground         # foreground/debug mode
alluvium reload                      # SIGHUP daemon to reload config.toml
alluvium stop-daemon                 # graceful stop
alluvium stop-daemon --force         # SIGKILL if graceful stop times out
alluvium status                      # JSON status
alluvium run-once --ignore-settle    # process current inbox once, useful for tests
alluvium integrate-once              # run integrator once
alluvium example-task                # create a sample inbox task
```

Daemon logs:

```text
logs/daemon.log
```

Daemon pid/lock:

```text
.alluvium/daemon.pid
.alluvium/daemon.lock
```

## Inbox publishing safety

The daemon waits for inbox items to be unchanged for `inbox_settle_seconds` before claiming them. This makes drag-and-drop/manual copies less likely to be processed halfway through.

For producer programs, prefer atomic publish:

```text
tasks/inbox/.some-task.tmp/
  files...
  ↓ rename when complete
tasks/inbox/some-task/
```

Bare files are supported too:

```text
tasks/inbox/report.pdf
```

## Durable repo conventions

Alluvium does not impose a special knowledge/proposal topology. The durable repo is just a Git repository. It may contain code, docs, knowledge, skills, tests, generated assets, or anything else your project needs.

Workers should make the branch correct as a whole. The integrator acts like a repo maintainer: it serially reviews, tests, merges, or sends the task back for amendment.

Put project-specific conventions in:

```text
repo/AGENTS.md
```

## Development

```bash
git clone https://github.com/spoj/alluvium
cd alluvium
uv sync --dev
uv run pytest
uv run alluvium --help
```

Build and publish:

```bash
uv build
UV_PUBLISH_TOKEN=... uv publish
```

## systemd user service

Use foreground mode under systemd:

```ini
[Unit]
Description=Alluvium Daemon
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/me/my-alluvium
ExecStart=/home/me/.local/bin/alluvium daemon --foreground --config /home/me/my-alluvium/config.toml
Restart=always
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=60

[Install]
WantedBy=default.target
```

Then:

```bash
systemctl --user daemon-reload
systemctl --user enable alluvium
systemctl --user start alluvium
journalctl --user -u alluvium -f
```

## Shutdown behavior

`alluvium stop-daemon` sends SIGTERM. The daemon stops accepting new work immediately, wakes sleeping loops, waits up to `shutdown_grace_seconds` for active workers, and then cancels remaining worker supervisors. Worker subprocesses receive SIGTERM and then SIGKILL if they do not exit.

If you send `kill <pid>` manually, the same SIGTERM path is used. If it appears slow, it is usually waiting for active worker cleanup. Use:

```bash
alluvium stop-daemon --force
```

only when you are okay with interrupting active workers.

## License

MIT
