# Forge

Forge is a lightweight, model-agnostic runtime for building AI agents that can safely interact with software projects.

> **Status: very early development (v0.1).** Forge currently provides its project foundation and a small CLI. It does **not** run AI agents yet.

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
- A minimal configuration object (`ForgeConfig`)
- Module boundaries for the agent, models, tools, and security layers (placeholders only)
- A test suite run with `pytest`

### Planned

Everything below is **not implemented yet**:

- Model provider adapters (OpenAI, Anthropic, Gemini, local models)
- Agent loop that coordinates the model and tools
- Built-in tools: read/edit files, search, run commands
- Permission system with user approval prompts
- MCP server support
- Memory
- Configuration files and environment-based settings

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
forge --help      # show available commands
forge --version   # show the installed version
forge doctor      # check that your local environment is ready
```

Example `forge doctor` output:

```
Forge Doctor

✓ Python 3.12.10
✓ Forge package loaded (v0.1.0)
✓ Workspace accessible (/path/to/your/project)
✓ Git installed (git version 2.x)
✓ Configuration valid (max_steps=20, debug=False)

Everything looks good.
```

You can also run Forge as a module: `python -m forge --help`.

## Roadmap

| Phase | Focus | Status |
|-------|-------|--------|
| 1 | Project foundation, CLI, config, tests | ✅ Done |
| 2 | First model provider and a minimal agent loop | Planned |
| 3 | Core tools (read, search, edit, run) with permission checks | Planned |
| 4 | MCP integration | Planned |
| 5 | Memory and multi-provider support | Planned |

The roadmap will change as the project evolves.

## Project structure

```
forge/
├── __init__.py          # package version
├── __main__.py          # enables `python -m forge`
├── cli.py               # Typer commands; display only, no agent logic
├── config.py            # ForgeConfig
├── doctor.py            # local environment checks used by `forge doctor`
├── agent/               # (placeholder) agent loop, state, prompts
├── models/              # (placeholder) model provider interface and registry
├── tools/               # (placeholder) tool interface and registry
└── security/            # (placeholder) permission decisions
tests/                   # pytest test suite
examples/                # usage examples (coming in later phases)
```

Dependencies flow in one direction: `cli` → `doctor` / `config`, and `agent` → `models` / `tools` / `security`. The `models`, `tools`, and `security` packages do not import each other or the agent, which keeps the layers independent.

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
