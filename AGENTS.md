# Alluvium Development Notes

Use `uv` for all Python commands:

```bash
uv sync --dev
uv run pytest
uv run alluvium --help
```

Project conventions:

- Keep runtime dependencies minimal; prefer the Python standard library.
- The public task API is `tasks/inbox/` accepting free-form folders and bare files.
- `.agent/` is the worker-facing reserved per-task subtree; `.system/` is the harness-only subtree (workers must not touch it).
- Every task gets a Git branch/worktree; task type is classified after worker execution.
- Worker concurrency is allowed; integration into `main` must remain serialized.
