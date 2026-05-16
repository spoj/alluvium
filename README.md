# Alluvium

Alluvium is a local-first task inbox daemon for running commodity coding agents concurrently.

The primary CLI is named `alluvium`. The PyPI distribution is `alluvium-swarm` because the bare `alluvium` package name is already taken on PyPI.

The public API is intentionally tiny:

> Drop a folder **or a bare file** into `tasks/inbox/`. The daemon claims it, assigns a unique task ID, gives it an isolated Git worktree/branch, runs an agent, stores outputs under `.agent/`, and serially integrates any repository changes.

It is designed for workflows like:

- incoming files that should be summarized or ingested into long-term knowledge,
- mail/upload/folder watchers that dump task folders into a local inbox,
- concurrent coding-agent tasks in isolated worktrees,
- self-improving durable repositories containing code, docs, knowledge, skills, tests, or any other project state.

## Status

Alpha. The core filesystem/Git orchestration is implemented. The default worker is a deterministic built-in inventory agent; configure `[agent].command` to use your preferred coding agent.

## Concepts

```text
agent-system/
  tasks/
    inbox/          # public drop zone: folders or bare files
    running/        # currently being worked
    needs_revision/ # integrator returned task to worker for amendment
    needs_human/    # blocked on clarification/approval/manual intervention
    done/           # worker finished; integration status is in .agent/integration.json
    failed/         # worker failed
    dead_letter/    # reserved for invalid/repeatedly failed tasks

  repo/             # long-term Git repo: code, docs, knowledge, skills, tests, etc.
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

## Installation with uv

After PyPI publication:

```bash
uv tool install alluvium-swarm
# or without installing permanently:
uvx --from alluvium-swarm alluvium --help
```

From GitHub before PyPI publication:

```bash
uv tool install git+https://github.com/spoj/alluvium
```

## Development with uv

```bash
git clone https://github.com/spoj/alluvium
cd alluvium
uv sync --dev
uv run alluvium --help
uv run pytest
```

## Publishing to PyPI

The intended PyPI distribution name is `alluvium-swarm`. The installed command is `alluvium`.

Build locally:

```bash
uv build
```

Publish with a PyPI token:

```bash
UV_PUBLISH_TOKEN=... uv publish
```

A GitHub Actions trusted-publishing workflow can also be used once the PyPI project is configured for trusted publishing.

## Quick start

Initialize a system root in the current directory:

```bash
mkdir agent-system
cd agent-system
alluvium init
```

Or initialize a specific path:

```bash
alluvium init ~/agent-system
cd ~/agent-system
```

Drop a bare file into the inbox:

```bash
echo "Please summarize this." > tasks/inbox/request.txt
```

Run once:

```bash
alluvium run-once --ignore-settle
```

Or run as a daemon:

```bash
alluvium daemon
```

Check status:

```bash
alluvium status
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
ALLUVIUM_TASK_ID
ALLUVIUM_TASK_DIR
ALLUVIUM_AGENT_DIR
ALLUVIUM_WORKTREE
ALLUVIUM_BRANCH
ALLUVIUM_PROMPT_FILE
```

The generated prompt asks the worker to:

- inspect the free-form task folder,
- write `.agent/understanding.md`, `.agent/plan.md`, `.agent/result.md`, and `.agent/result.json`,
- put artifacts under `.agent/outputs/`,
- commit useful repo changes directly on the task branch,
- request human help by writing `.agent/needs_human.md`,
- address integrator feedback from `.agent/revision_request.md` when returned,
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

The integrator loop scans `tasks/done/` for pending integrations, performs a squash merge into `main`, runs configured tests, and updates the integration status to `merged`, `noop`, `needs_revision`, or `blocked`.

If a merge conflict or integration test failure is likely fixable by the worker, the integrator writes:

```text
.agent/revision_request.md
```

and moves the task to:

```text
tasks/needs_revision/
```

The coordinator then re-runs a worker on the same task ID, branch, and worktree so it can amend the branch. After the worker finishes, the task returns to `done/` and integration is attempted again. After `max_revision_rounds`, or for unrevisionable problems, the task is moved to `needs_human/`.

## Durable repo conventions

Alluvium does not impose a special knowledge/proposal topology. The durable repo is just a Git repository. It may contain code, docs, knowledge, skills, tests, generated assets, or anything else your project needs.

Workers are expected to make the branch correct as a whole. The integrator acts like a repo maintainer: it serially reviews, tests, merges, or sends the task back for amendment.

Project-specific conventions should live in `repo/AGENTS.md`.

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
Description=Alluvium Daemon
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/me/agent-system
ExecStart=/home/me/.local/bin/alluvium daemon --config /home/me/agent-system/config.toml
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

## Design tradeoffs

- The inbox is free-form; `.agent/` is the only reserved task subtree.
- Every task gets a branch/worktree, even if no repo changes are ultimately made.
- `done/` means the worker finished. Integration status is separate and stored inside `.agent/integration.json`.
- `needs_revision/` is the send-back primitive: the same task branch is re-run and amended.
- Workers may run concurrently; `main` integration is serialized.
- The default crash recovery is conservative: interrupted `running/` tasks move to `failed/` on daemon restart.

## License

MIT
