# Inbox Swarm

Inbox Swarm is a local-first task inbox daemon for running commodity coding agents concurrently.

The public API is intentionally tiny:

> Drop a folder **or a bare file** into `tasks/inbox/`. The daemon claims it, assigns a unique task ID, gives it an isolated Git worktree/branch, runs an agent, stores outputs under `.agent/`, and serially integrates any repository changes.

It is designed for workflows like:

- incoming files that should be summarized or ingested into long-term knowledge,
- mail/upload/folder watchers that dump task folders into a local inbox,
- concurrent coding-agent tasks in isolated worktrees,
- self-improving `knowledge/` and `skills/` repositories over time.

## Status

Alpha. The core filesystem/Git orchestration is implemented. The default worker is a deterministic built-in inventory agent; configure `[agent].command` to use your preferred coding agent.

## Concepts

```text
agent-system/
  tasks/
    inbox/          # public drop zone: folders or bare files
    running/        # currently being worked
    needs_human/    # blocked on clarification/approval/integration conflict
    done/           # worker finished; integration status is in .agent/integration.json
    failed/         # worker failed
    dead_letter/    # reserved for invalid/repeatedly failed tasks

  repo/             # long-term Git repo: knowledge, skills, code, docs
  worktrees/        # one worktree per task
  config.toml
```

A dropped item named `acme-contract` becomes a unique internal task like:

```text
tasks/running/20260516T101530Z-acme-contract-a8f2c1/
```

A bare file works too:

```text
tasks/inbox/report.pdf
```

becomes:

```text
tasks/running/20260516T101530Z-report.pdf-c771aa/
  report.pdf
  .agent/
```

Every task gets:

- a reserved `.agent/` subtree,
- a Git branch `task/<task-id>`,
- a Git worktree `worktrees/<task-id>`.

The daemon does not pre-classify tasks. The agent infers meaning from the free-form folder.

## Installation / development with uv

```bash
git clone <this-repo>
cd inbox-swarm
uv sync --dev
uv run inbox-swarm --help
```

## Quick start

Initialize a system root:

```bash
uv run inbox-swarm init ~/agent-system
cd ~/agent-system
```

Drop a bare file into the inbox:

```bash
echo "Please summarize this." > tasks/inbox/request.txt
```

Run once:

```bash
uv run --project /path/to/inbox-swarm inbox-swarm run-once --config config.toml --ignore-settle
```

Or run as a daemon:

```bash
uv run --project /path/to/inbox-swarm inbox-swarm daemon --config config.toml
```

Check status:

```bash
uv run --project /path/to/inbox-swarm inbox-swarm status --config config.toml
```

## Using a real coding agent

Edit `config.toml`:

```toml
[agent]
# Placeholders: {task_id}, {task_dir}, {agent_dir}, {worktree}, {branch}, {prompt_file}
command = ["your-coding-agent", "--cwd", "{worktree}", "--prompt-file", "{prompt_file}"]
timeout_seconds = 3600
```

The daemon provides environment variables too:

```text
INBOX_SWARM_TASK_ID
INBOX_SWARM_TASK_DIR
INBOX_SWARM_AGENT_DIR
INBOX_SWARM_WORKTREE
INBOX_SWARM_BRANCH
INBOX_SWARM_PROMPT_FILE
```

The generated prompt asks the worker to:

- inspect the free-form task folder,
- write `.agent/understanding.md`, `.agent/plan.md`, `.agent/result.md`, and `.agent/result.json`,
- put artifacts under `.agent/outputs/`,
- commit useful repo changes on the task branch,
- request human help by writing `.agent/needs_human.md`,
- log external effects under `.agent/effects/ledger.jsonl`.

## Integration model

Workers run concurrently. Integration into `main` is serial.

After a worker exits, the task is moved to `tasks/done/` if it succeeded. The daemon writes:

```text
.agent/integration.json
```

If the task branch has no changes:

```json
{"status": "noop", "has_repo_changes": false}
```

If it has changes:

```json
{"status": "pending", "has_repo_changes": true, "branch": "task/..."}
```

The integrator loop scans `tasks/done/` for pending integrations, performs a squash merge into `main`, runs configured tests, and updates the integration status to `merged`, `noop`, or `blocked`.

Blocked integrations can be moved to `tasks/needs_human/`.

## Knowledge and skills conventions

The initialized repo contains:

```text
repo/
  knowledge/
    curated/
    inbox/
    sources/
  skills/
    approved/
    proposed/
```

Recommended conventions:

- Workers write per-task knowledge notes to `knowledge/inbox/`.
- Workers write source metadata to `knowledge/sources/`.
- Workers propose generated tools under `skills/proposed/`.
- `skills/approved/` is treated as read-only unless a task is explicitly a skill-review task.
- A synthesis task can periodically compress accepted notes into `knowledge/curated/`.

Optional synthesis scheduling can be enabled in `config.toml`:

```toml
[synthesis]
enabled = true
min_inbox_notes = 10
interval_seconds = 86400
check_interval_seconds = 300
```

Synthesis is implemented as a normal internally-created inbox task, so it goes through the same worker/worktree/integration flow.

## Inbox publishing safety

The daemon supports human-friendly dropping. It waits for inbox items to be unchanged for `inbox_settle_seconds` before claiming them.

For producer programs, the safest pattern is atomic publish:

```text
tasks/inbox/.some-task.tmp/
  files...
  ↓ rename when complete
tasks/inbox/some-task/
```

Bare files are also supported:

```text
tasks/inbox/report.pdf
```

The daemon wraps the file into a unique task folder when claiming it.

## systemd user service example

```ini
[Unit]
Description=Inbox Swarm Daemon
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/me/agent-system
ExecStart=/path/to/inbox-swarm/.venv/bin/inbox-swarm daemon --config /home/me/agent-system/config.toml
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
systemctl --user enable inbox-swarm
systemctl --user start inbox-swarm
journalctl --user -u inbox-swarm -f
```

## Design tradeoffs

- The inbox is free-form; `.agent/` is the only reserved task subtree.
- Every task gets a branch/worktree, even if no repo changes are ultimately made.
- `done/` means the worker finished. Integration status is separate and stored inside `.agent/integration.json`.
- Workers may run concurrently; `main` integration is serialized.
- The default crash recovery is conservative: interrupted `running/` tasks move to `failed/` on daemon restart.

## License

MIT
