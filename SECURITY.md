# Security model

Forge mediates model-requested actions with a permission engine. Filesystem tools resolve paths and symlinks, reject workspace escapes and protect Git internals and evidence. Shell command classification catches common risky patterns; it cannot prove arbitrary code safe.

Approved shell commands, verification scripts and MCP servers run with your account's privileges. Candidate worktrees provide separate checkouts, not OS isolation. Git worktrees share repository administrative objects. Use a container or VM for hostile code and minimize available credentials.

API credentials come from environment variables. MCP credentials use explicit environment references. Forge removes secret-looking environment names from ordinary commands and refuses secret literals in config/memory. Detection is heuristic. Local journals and candidate contents retain exact project bytes for apply/undo and may contain sensitive data. Protect `.forge` records and inspect exports before sharing.

Verification proves that configured commands ran and returned their recorded status; it does not prove test quality, absence of vulnerabilities, or adversarial grading integrity. Missing checks remain unverified. Undo restores recorded file changes where hashes still match; it cannot undo database, network or external tool side effects.

The v1 hardening review covered workspace and record path traversal, symlinks/junctions, shell boundaries, MCP disconnects and message limits, secret-bearing output, Git worktree lifecycle, apply/undo integrity, cancellation, output growth, context overflow and corrupted memory. Tests exercise these boundaries alongside existing provider, permission, verification and budget failures.

Known limits remain: path checks and atomic writes do not eliminate filesystem races against another local process; exact file artifacts are not encrypted; heuristic scrubbing can miss unfamiliar secrets; shell/MCP child processes that escape their process group are not guaranteed to terminate. Cancellation waits for blocking tools to settle (up to their timeout) before cleanup. A second forced interrupt or machine crash can leave temporary resources; preserved run IDs and records support inspection. Apply restores its completed writes on ordinary write failure, but a crash between file writes or record persistence is not a transactional multi-file commit. Inspect task journals for recovery.

Budget admission reserves estimated input plus the configured output cap before API calls. Provider tokenizers, retries and billing can differ from these estimates. Configure prices and provider limits; Forge records unknown costs rather than inventing them. Live vendor behavior requires credentials and is outside the offline release test suite.

Report a vulnerability privately through the repository's GitHub Security Advisories when enabled. If private reporting is unavailable, contact the repository owner without posting credentials or an exploit against a live service. Ordinary bugs can use GitHub issues.
