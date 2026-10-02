# Broken calculator

A tiny project with a deliberate bug: `multiply` adds instead of multiplying, so two tests fail.

It is used by Forge's own tests (with a scripted fake model) and is handy for trying Forge with a real model.

## Try it

Work on a copy so the example stays broken:

```bash
cp -r examples/broken_calculator /tmp/calc && cd /tmp/calc      # Windows: xcopy /E /I examples\broken_calculator %TEMP%\calc
python -m pytest -q                                              # 2 failed, 2 passed
forge run "The multiply tests fail. Find and fix the bug, then run the tests."
```

With a local model: add `-p ollama -m <model>`. Forge will ask before editing files or running commands.
