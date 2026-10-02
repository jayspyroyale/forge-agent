# Contributing

Install Python 3.12+ and Git, then `python -m pip install -e ".[dev]"`.

Keep normal tasks and candidates on the same `run_task` path. New tools must use ToolRegistry, ToolExecutor and PermissionEngine. Verification evidence must come from execution records. Model opinions must stay separately labelled. Never introduce a second agent loop for exploration.

Add deterministic fake-provider tests and temporary repositories for meaningful behavior changes. Run `python -m pytest --cov=forge`, `python -m compileall -q forge tests`, `python -m ruff check forge tests`, `git diff --check`, and `python -m build`. Format new modules with `python -m ruff format path/to/module.py`; avoid unrelated formatting churn. Live-provider tests are opt-in (`pytest -m live`) and may cost money.

Inspect diffs and scan for secrets before committing. Do not upload local `.forge` records, keys or private project content. Describe the concrete behavior change, validation and limitations in a pull request. Follow [SECURITY.md](SECURITY.md) for sensitive reports.
