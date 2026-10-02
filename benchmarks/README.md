# Benchmarks

The installable task data lives in `forge/benchmarks/tasks/` so the same tasks work from a wheel and a checkout. Each task contains `benchmark.toml`, a starter project, an acceptance script and expected selection constraints.

```sh
forge benchmark list
forge benchmark run fix_python_bug --approaches 3 --yes --output result.json
forge benchmark run performance_improvement --normal --yes
forge benchmark compare result.json another-result.json
```

Available tasks: fix_python_bug, fix_typescript_bug (Node 22.6+ with type stripping), add_feature, refactor_without_behavior_change, performance_improvement. The performance task measures comparison counts, avoiding machine-dependent timing thresholds.

Every run copies the starter into a temporary workspace and uses `run_task` or `ExplorationController`. JSON contains the starter SHA-256, task, acceptance command, configuration, Python/platform/Forge versions, candidate records, measured usage, selection and proof of work. Model APIs are not perfectly reproducible. The fake-provider demo is deterministic.

Use trusted starters and inspect their verification code before running. Benchmarks execute code with your account's privileges. Acceptance scripts are evaluation examples, not an adversarial grading sandbox. Costs remain unknown unless reported by a provider or configured prices are available.

