# Backend architecture walkthrough

## Normal execution

`forge run "Fix the tests"` loads layered configuration and creates the permission policy. `agent/runtime.py:run_task` resolves the workspace, reads Git state, snapshots pre-existing changes and creates a task journal. It retrieves relevant project memory and deterministic file references, connects configured MCP servers, registers their adapters alongside built-ins and creates the shared `Agent` loop.

The provider receives normalized messages and neutral tool schemas. Requested tools go through ToolExecutor: lookup, argument validation, risk classification, PermissionEngine, then execution. The loop records results and provenance. After edits, Forge runs detected verification checks itself, feeds failures back for repair and eventually creates TaskEvidence from its own records. Final workspace comparison catches changes made by shell commands too. The task's evidence and context manifest are persisted locally.

## Exploration execution

`forge explore --approaches 3 "Implement caching"` uses ExplorationController, not a second agent. The planner requests distinct structured approaches, rejects malformed/duplicate plans and assigns candidate IDs. Preflight establishes a baseline and rejects ambiguous Git operations. Isolation creates a detached worktree for each candidate, overlays the saved uncommitted baseline, or copies a non-Git project.

Each candidate calls `run_task` with its own workspace/config/provider and approach brief. It gets the same tools, permission engine, context, verifier and evidence builder as a normal task. Candidates execute sequentially today. A candidate exception is recorded without aborting its peers. Hash manifests collect independently changed files, diff statistics and dependency additions; runtime records supply checks, tool usage, elapsed time, token usage and risk events. All candidates retain their evidence and final file artifacts.

## Isolation and its boundary

Candidate directories and working files are independent. Filesystem tools resolve every target against their own Workspace; symlink escapes and protected writes are refused. The controller's cleanup is scoped to its candidate directories. The active project is never the target of candidate editing tools.

This is filesystem organization, not a sandbox. Approved terminal code and MCP servers can access other directories with your OS privileges; worktrees share Git administration. A malicious program can corrupt another checkout. Use an external container/VM when that threat matters. See SECURITY.md.

## Evidence, review and selection

`exploration/compare.py` derives MEASURED values from CandidateResult and TaskEvidence. No model can supply test status or fabricated cost through its prose. `review.py` separately asks a model about diffs, validates its scores and stores MODEL-ASSESSMENT. Opinions only contribute to maintainability/scalability factors when weighted; missing assessments are explicitly neutral.

Weights normalize to one. Hard constraints generate a separate list of violations and ineligible candidates. Eligible candidates are deterministically ranked; ties use correctness, smaller diff, lower cost and ID. Explanations describe measured advantages and tradeoffs. Manual selection records a user's choice, assisted selection records acceptance or an alternative, and autonomous selection takes the top eligible candidate. User overrides of constraints remain visible.

## Usage, adaptation and multiple models

`models/budget.py` wraps the existing provider. Planning, candidates and review share a ledger. Before each request, it reserves conservative input size plus the configured output cap, prices tokens from explicit rates, and denies requests that do not fit. Reported usage replaces the reservation; absent usage remains estimated. Provider costs take precedence over configured estimates. Unknown cost is never labelled zero. A dollar admission cap requires known rates. Token estimates and vendor billing are not identical; in-flight/retried requests may still be charged.

Adaptive exploration starts with a configured small batch. It compares measured results and stops on maximum candidates, budget, verified dominance or plateau. Weak/close results cause another plan and candidate. Experimental generation prompts later plans with earlier eligible candidates' evidence and records parent IDs; it never combines code blindly. Model pools use deterministic rotation; explicit candidate assignments override the pool. Same-approach mode permits testing one plan with different models. The core controller knows only ModelProvider, not vendor SDKs.

## Context, memory and MCP

ContextEngine retains normalized items with source type/reference, order, importance, estimated tokens and provenance. Its deterministic renderer compresses older tool output, supersedes repeated/stale observations and prioritizes instructions/errors/current state. The transcript is the record; the rendered view is what gets sent. Retrieval uses filename/text search, not embeddings.

MemoryStore uses local SQLite, project-scoped records, confidence, expiry and verification state. Equal or stronger conflicting evidence supersedes old memory; weaker contradictions are contested. Only relevant active records enter context. Memory is optional and corrupted databases cause a reported fallback. Model-written memories are off by default.

McpManager starts trusted configured stdio processes, McpClient handles handshake/discovery/timeouts and McpTool adapts tools into the same registry. Names use `server.tool`. Forge configuration controls risk; server hints may raise it. Remote failures become tool failures. Starting a configured server itself executes trusted code; per-call permissions do not sandbox the process.

## Proof of work, application and undo

Evidence combines recorded checks, tools, commands, change reports and usage. VERIFIED means an actual check passed after the last mutation; FAILED and UNVERIFIED remain explicit. Selected-candidate evidence becomes final task evidence while other candidates remain inspectable.

Apply reads every saved final file and verifies its hash, then checks current touched files against the baseline. Conflicts abort application. It journals originals and writes only the candidate change set with atomic per-file writes. It does not merge branches, reset Git or overwrite unrelated edits. The applied task links back to candidate evidence and selection.

Undo checks current file hashes against recorded final hashes, restores saved originals where safe and backs up replaced content. It skips later edits or missing originals. Shell/MCP side effects outside recorded files cannot be reversed. Verification was performed in the candidate workspace; application does not claim a new check in the active workspace.

## Limitations and study order

No OS sandbox; heuristic shell risk and secret detection; no native Anthropic/Gemini adapters or streaming; sequential candidates; deterministic retrieval without semantic indexes; small example benchmarks rather than an adversarial grading platform; finite snapshots/output/diff limits; estimated vendor costs; per-file application rather than a database/filesystem transaction; local records are not cryptographically authenticated.

Study these modules in this order:

1. `agent/runtime.py`, `agent/loop.py`, `agent/state.py`: shared execution and wiring.
2. `tools/registry.py`, `tools/executor.py`, `security/permissions.py`, `workspace.py`: action boundaries.
3. `verification/runner.py`, `evidence.py`, `tasks/snapshot.py`, `tasks/journal.py`, `tasks/undo.py`: evidence/reversibility.
4. `exploration/controller.py`, `isolation.py`, `results.py`, `store.py`: candidate orchestration and records.
5. `exploration/compare.py`, `selection.py`, `apply.py`, `review.py`: measured selection and application.
6. `models/base.py`, `types.py`, `registry.py`, `budget.py`: neutral providers and accounting.
7. `context/engine.py`, `items.py`, `memory/store.py`, `mcp/client.py`, `mcp/adapter.py`: context and integrations.
8. `config/schema.py`, `loader.py`, `cli/app.py`, `benchmarks/runner.py`: configuration, commands and experiments.
