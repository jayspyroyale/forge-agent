# Security model

Forge mediates model-requested actions with a permission engine. Filesystem tools resolve paths and symlinks, reject workspace escapes and protect Git internals and evidence. Shell command classification catches common risky patterns; it cannot prove arbitrary code safe.

Approved shell commands, verification scripts and MCP servers run with your account's privileges. Candidate worktrees provide separate checkouts, not OS isolation. Git worktrees share repository administrative objects. Use a container or VM for hostile code and minimize available credentials.

API credentials come from environment variables. MCP credentials use explicit environment references. Forge removes secret-looking environment names from ordinary commands and refuses secret literals in config/memory. Detection is heuristic. Local journals and candidate contents retain exact project bytes for apply/undo and may contain sensitive data. Protect `.forge` records and inspect exports before sharing.

Verification proves that configured commands ran and returned their recorded status; it does not prove test quality, absence of vulnerabilities, or adversarial grading integrity. Missing checks remain unverified. Undo restores recorded file changes where hashes still match; it cannot undo database, network or external tool side effects.

Report a vulnerability privately through the repository's GitHub Security Advisories when enabled. If private reporting is unavailable, contact the repository owner without posting credentials or an exploit against a live service. Ordinary bugs can use GitHub issues.
