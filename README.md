# Alluvium

Alluvium is a small, local-first runner for coding agents.

You drop free-form folders or bare files into `inbox/`. Alluvium claims each item into a stable `tasks/<task-id>/` artifact folder, runs a low number of workers optimistically in isolated Git worktrees/branches, records task-local artifacts under `.agent/`, stores authoritative task state in a local SQLite database, and serially integrates durable repository changes through temporary integration worktrees.

The design goal is boring robustness over elaborate multi-agent choreography: a filesystem inbox, a tiny state index, bounded worker concurrency, explicit artifacts, and Git as the durable integration boundary.

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

Run the local runner in the foreground:

```bash
alluvium serve
```

For supervised/background use, prefer systemd, launchd, Windows service wrappers, Docker, or your normal process supervisor. `alluvium daemon` remains as a compatibility convenience wrapper.

Drop work into the inbox:

```bash
echo "Please summarize this." > inbox/request.txt
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
  inbox/            # public drop zone: folders or bare files not yet claimed
  tasks/            # stable system-owned task folders; state lives in SQLite
    <task-id>/
      input/        # original producer files
      .agent/       # prompts, logs, outputs, repo metadata, results
  repo/             # durable Git repo: code, docs, knowledge, skills, tests, etc.
  worktrees/        # one Git worktree per active task
  logs/
  .alluvium/        # lock/pid files and alluvium.db SQLite state index
```

A bare file is wrapped into a task folder automatically:

```text
inbox/report.pdf
  ↓
tasks/20260516T101530Z-report.pdf-c771aa/
  input/
    report.pdf
  .agent/
```

A dropped folder keeps its contents but receives a unique internal name:

```text
inbox/acme-contract/
  contract.pdf
  request.md
  ↓
tasks/20260516T101530Z-acme-contract-a8f2c1/
  input/
    contract.pdf
    request.md
  .agent/
```

`.agent/` is reserved runtime state at the task root. Producer files are always moved under `input/`, so a producer-supplied `.agent/` is treated as ordinary untrusted input at `input/.agent/` rather than trusted runtime state.

## Lifecycle

```text
inbox/
  ↓ claim into stable tasks/<task-id>/
SQLite state:
  queued → running → worker_done → integrating
                                  ├── done/noop
                                  ├── done/merged
                                  ├── needs_revision → same task/branch is re-run and amended
                                  └── needs_human
```

Workers may run concurrently, but the default is intentionally low. Integration into `repo/main` is serialized and tested in a temporary integration worktree before the base branch is advanced.

The task folder does not move when state changes. SQLite is authoritative for state; integration details are also stored in:

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

and marks the task `needs_revision` in SQLite. The coordinator re-runs a worker on the same task ID, branch, stable task folder, and worktree so it can amend the branch.

## Configure the worker

The default worker command uses `pi` with `gpt5/gpt-5.4:high`:

```toml
[agent]
command = ["pi", "--model", "gpt5/gpt-5.4:high", "--no-session", "-p", "@{prompt_file}"]
timeout_seconds = 3600
```

Edit `config.toml` to use another coding agent:

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

### Deterministic smoke-test worker

```toml
[agent]
command = ["python", "-m", "alluvium.builtin_agent", "--task-dir", "{task_dir}", "--worktree", "{worktree}", "--prompt-file", "{prompt_file}"]
timeout_seconds = 3600
```

The built-in agent inventories inputs but does not reason or edit meaningfully. It is useful for tests and smoke checks.

Pi runs from the task worktree by default. The prompt file includes the absolute task folder path, so pi can write `.agent/` outputs and edit the durable repo.

## Useful commands

```bash
alluvium init [path]                 # initialize workspace
alluvium serve                       # foreground local runner
alluvium daemon                      # compatibility: start background runner
alluvium daemon --foreground         # compatibility foreground mode
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
inbox/.some-task.tmp/
  files...
  ↓ rename when complete
inbox/some-task/
```

Bare files are supported too:

```text
inbox/report.pdf
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
ExecStart=/home/me/.local/bin/alluvium serve --config /home/me/my-alluvium/config.toml
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
