# Forge

Forge is a lightweight, model-agnostic runtime for building AI agents that can safely interact with software projects.

> **Status: early development.** Forge can run an agent loop: a model reads your project through workspace-confined tools and answers. Editing, command execution, and permissions are being added phase by phase.

## What is Forge?

Forge aims to be a small, readable runtime that sits between an AI model and your codebase. The model decides *what* to do; Forge provides the tools to do it, enforces a permission system around those tools, and keeps the whole loop understandable.

Design goals:

- **Lightweight** — few dependencies, small modules, easy to read end to end.
- **Model-agnostic** — no lock-in to a single AI provider.
- **MCP-native** — tools from Model Context Protocol servers should feel first-class.
- **Safe by default** — actions that touch your files or system must pass a permission check.

## Long-term vision

Eventually, an AI model running inside Forge should be able to:

- inspect, search, and edit files in a repository
- run terminal commands and test its own work
- use different model providers (OpenAI, Anthropic, Gemini, local models, ...)
- connect to MCP servers
- maintain memory across sessions
- do all of this under an explicit permission and safety system

## Current status

### Available now

- Installable Python package (`pip install -e .`)
- `forge --help`, `forge --version`
- `forge doctor` — local environment checks (Python version, package, workspace, Git, configuration)
- `forge models list` — show the available model providers
- `forge ask "..."` — send one prompt to a model and print the reply
- A model-agnostic layer: normalized message, tool-call, response, and usage types; a provider interface; a provider registry; Forge-level model errors
- Model providers:
  - `openai` — OpenAI Chat Completions API
  - `ollama` — local models through Ollama's OpenAI-compatible API
  - `fake` — offline provider for tests
- Tool system: tool interface, registry, executor, and neutral JSON-Schema tool definitions
- Read-only built-in tools: `current_directory`, `list_files`, `read_file`, `file_exists`, `search_text`, all confined to the workspace (no `..` or symlink escapes)
- Editing tools: `write_file` (new files; replacing needs `overwrite=true`) and `edit_file` (exact, unique text replacement; never guesses between multiple matches). Writes are atomic and keep each file's line endings
- `run_command`: runs a shell command in the workspace with a timeout (whole process tree killed), capped output (start and end kept), and secret-looking environment variables removed
- Permission system between the model and every tool call: each call is classified `read` / `write` / `execute` / `dangerous`, then allowed, asked (yes once / always this session / no), or denied by policy (default: read=allow, write=ask, execute=ask, dangerous=deny). A heuristic classifier flags destructive shell commands (recursive deletes, `git reset --hard`, disk formatting, privilege escalation, paths outside the workspace, ...). `--yes` approves "ask" actions but never overrides "deny"
- A concise coding-agent policy (inspect before editing, search instead of guessing, minimal targeted changes, verify, report honestly) built from composable prompt sections in `forge/agent/prompts.py`, plus loop guidance: notes on repeated identical failures, a nudge after several failures in a row, one retry on an empty reply, and a last-step warning
- `examples/broken_calculator/`: a tiny project with a deliberate bug for trying Forge end to end
- Verification-first completion: Forge detects checks from the project's own config (pytest, ruff, mypy, package.json scripts, cargo, go) and, after the agent changes files, runs them itself before accepting "done". Failures go back to the model to fix (up to 3 attempts)
- Proof of work: every task produces `TaskEvidence` built only from Forge's records (tools used, commands run, files changed, verification results), reported as VERIFIED / FAILED / UNVERIFIED. A model saying "tests pass" is not evidence
- Git awareness: read-only `git_status` / `git_diff` tools for the model; a repository reader that refuses any state-changing git command
- Task tracking: each `forge run` snapshots the workspace first, journals original file contents before edits, and reports which files the task changed (with +/- lines) separately from changes you already had. Records live in `.forge/tasks/<id>/` (ignored by Git)
- `forge tasks list | show | undo` — review a task's proof of work, and revert its changes only where provably safe (files untouched since the task; your pre-existing changes are never touched; no git reset/checkout)
- `forge tools list | describe | run` — inspect and run tools directly, without a model
- `forge run "task"` — the agent loop: model → tool calls → results back to the model → final answer, with a hard step limit (`--max-steps`)
- Configuration through `FORGE_*` environment variables
- A test suite run with `pytest` (no network or API key needed)

### Planned

Everything below is **not implemented yet**:

- More providers (Anthropic, Gemini, DeepSeek, ...)
- Streaming replies
- MCP server support
- Memory
- Configuration files

## Installation

Requires **Python 3.12+** and Git.

```bash
git clone https://github.com/jayspyroyale/forge-agent.git
cd forge-agent

python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate

pip install -e ".[dev]"
```

`-e` installs Forge in *editable* mode, so changes to the source code take effect without reinstalling. `[dev]` also installs the test tools.

## CLI commands

```bash
forge                           # interactive session: type tasks, /help, /exit
forge "Fix the failing tests"   # one task (same as: forge run "...")
forge --help                    # show available commands
forge --version                 # show the installed version
forge doctor                    # check that your local environment is ready
forge models list               # list model providers
forge ask "Reply with exactly FORGE_OK"            # use the configured provider
forge ask -p ollama -m llama3 "Explain recursion"  # choose provider and model
forge ask -p fake "hello"       # offline test provider; prints "[fake] hello"
forge tools list                # list built-in tools
forge tools describe read_file  # show a tool's argument schema
forge tools run read_file path=README.md max_lines=20
forge run "Read README.md and summarize it"   # agent loop
forge run --verbose "..."       # also show tool output; --debug shows everything
forge tasks list                # recorded tasks; then: forge tasks show ID / forge tasks undo ID
```

`forge ask` options: `--provider/-p`, `--model/-m`, `--base-url`, `--debug` (show full tracebacks).

You can also run Forge as a module: `python -m forge --help`.

## Configuration

Settings come from environment variables. CLI options override them.

| Variable | Meaning | Default |
|----------|---------|---------|
| `FORGE_PROVIDER` | Provider name (`openai`, `ollama`, `fake`) | `openai` |
| `FORGE_MODEL` | Model name | provider's default (`gpt-5.4-mini` for OpenAI; Ollama has none) |
| `FORGE_BASE_URL` | Server URL for OpenAI-compatible providers | provider's default |
| `FORGE_TEMPERATURE` | Sampling temperature, 0–2 | provider's default |
| `FORGE_TIMEOUT` | Seconds to wait for a reply | `120` |
| `FORGE_DEBUG` | Show full tracebacks (`true`/`false`) | `false` |

**API keys are never stored in Forge's configuration.** Each provider reads its own key from the environment:

- `openai`: `OPENAI_API_KEY`
- `ollama`: none needed (runs locally)

Do not commit keys. `.env` files are ignored by Git.

### Using a local model with Ollama

```bash
ollama pull llama3
forge ask -p ollama -m llama3 "Reply with exactly FORGE_OK"
```

Other OpenAI-compatible servers can be used with `-p openai --base-url <url>`.

## Running tests

```bash
pytest            # all offline tests; no network, no API key
pytest -m live    # optional tests against real providers
```

Live tests skip themselves unless their provider is configured: set `OPENAI_API_KEY` for OpenAI, or `FORGE_LIVE_OLLAMA_MODEL` (for example `llama3`) for Ollama.

## Roadmap

| Phase | Focus | Status |
|-------|-------|--------|
| 1 | Project foundation, CLI, config, tests | ✅ Done |
| 2 | Model-agnostic provider layer, first real provider, `forge ask` | ✅ Done |
| 3 | Agent loop and core tools (read, search, edit, run) with permission checks | Planned |
| 4 | MCP integration | Planned |
| 5 | Memory and more providers | Planned |

The roadmap will change as the project evolves.

## Project structure

```
forge/
├── __init__.py          # package version
├── __main__.py          # enables `python -m forge`
├── cli/                 # Typer + Rich: commands, live event rendering, reports, interactive session
├── config.py            # ForgeConfig and environment variables
├── doctor.py            # local environment checks used by `forge doctor`
├── models/
│   ├── types.py         # Message, ToolCall, ToolDefinition, Usage, ModelResponse
│   ├── base.py          # ModelProvider interface
│   ├── errors.py        # Forge-level model errors
│   ├── registry.py      # provider lookup by name; create_provider(config)
│   └── providers/
│       ├── fake.py              # offline provider for tests
│       └── openai_provider.py   # OpenAI and Ollama adapters
├── agent/               # (placeholder) agent loop, state, prompts
├── tools/               # (placeholder) tool interface and registry
└── security/            # (placeholder) permission decisions
tests/                   # pytest test suite
examples/                # broken_calculator: a tiny buggy project to try Forge on
```

Dependencies flow in one direction:

- `cli` → `models`, `config`, `doctor`
- `agent` → `models`, `tools`, `security`, `config`
- `models` → `config`

`tools` and `security` import nothing else from Forge. Only the files in `models/providers/` know about provider SDKs; everything else uses Forge's own types.

## Contributing

Forge is at a very early stage, and the design is still settling. Issues and discussion are welcome.

If you want to contribute code:

1. Fork the repository and create a branch.
2. Install with `pip install -e ".[dev]"`.
3. Make your change and add tests.
4. Run `pytest` and make sure everything passes.
5. Open a pull request describing what you changed and why.

Please keep changes small and readable; clarity is a core goal of the project.

## License

Forge is released under the [MIT License](LICENSE).
