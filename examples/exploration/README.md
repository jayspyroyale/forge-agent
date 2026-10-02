# Reproducible exploration demo

Install Forge, then run:

```sh
python -m forge.benchmarks.demo
```

No API key, network or paid model is needed. Scripted fake providers propose three different approaches and call real editing tools in separate temporary workspaces. Forge runs the acceptance script independently for each, rejects the incorrect subtraction implementation, compares evidence and configured cost estimates, applies the selected implementation, then proves undo restores the starter.

The demo prints plans, tool/verification events, candidate costs, comparison, selection and application. It leaves your current workspace unchanged. The fake model supplies known token counts; this demonstrates accounting, not real vendor pricing or model quality.

With a real provider, try:

```sh
forge benchmark run fix_python_bug --approaches 3 --yes --output experiment.json
```

For your own project: `forge explore --approaches 3 "Implement caching" --no-apply`, inspect `forge explorations show latest --candidate A --patch`, then `forge explorations select latest A` and `forge explorations apply latest`.
