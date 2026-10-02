# Forge v1 release verification

The release uses the existing GitHub repository. Packages are built and installed locally; no external package registry publication is performed.

The checks required before tagging are:

- Complete offline pytest suite, including context, memory, MCP, isolated candidates, selection, adaptive budgets, benchmarks and failure/stress cases.
- Python compile checks, Ruff checks, Git diff inspection and credential-pattern review.
- Wheel and source archive build; wheel installation into an isolated environment, CLI version/help and the packaged offline exploration demo.
- GitHub Actions on Windows and Ubuntu with Python 3.12, 3.13 and 3.14, plus the clean-install package job.
- Accurate README, MIT license, documented security limits, committed and pushed release version.

The demo uses scripted model replies but runs filesystem editing, independent verification, comparison, apply and undo for real. Live provider tests are opt-in and require credentials; passing offline checks does not certify live vendor availability or billing accuracy.

Recovery: interrupted tasks and candidates preserve proof of work and journals. Inspect `forge tasks show <id>` or `forge explorations show <id>` before applying or undoing. Explicitly kept workspaces remain for review. Undo only restores files whose current hash matches the recorded completed version; it skips later user edits. Exact journal/candidate file bytes may be sensitive and must stay local.
