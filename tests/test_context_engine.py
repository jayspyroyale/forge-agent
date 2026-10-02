"""The context engine: provenance, budgets, compression, redundancy, and determinism."""

import asyncio
import json

from forge.agent.runtime import run_task
from forge.config import ForgeConfig
from forge.context import ContextBudget, ContextEngine, Importance, Provenance, SourceType
from forge.context.compress import HEADER, summarize_output, summarize_test_output
from forge.context.retrieval import find_relevant_files, task_keywords
from forge.context.tokens import estimate_message_tokens, estimate_tokens
from forge.models.providers.fake import FakeModelProvider
from forge.models.types import Message, ModelResponse, ToolCall
from forge.security.permissions import PermissionEngine
from forge.security.policy import PermissionPolicy
from forge.tasks.store import TaskStore
from forge.workspace import Workspace

BUG = "def multiply(a, b):\n    return a + b  # BUG: should be a * b"
FIX = "def multiply(a, b):\n    return a * b"


def tool(call_id, name, **arguments):
    return ModelResponse(tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)], finish_reason="tool_calls")


def pytest_output(failures, passed=10, noise_lines=10_000):
    lines = [f"tests/test_auth.py::test_{n} PASSED" for n in range(noise_lines)]
    lines += [f"FAILED tests/test_auth.py::{name} - AssertionError" for name in failures]
    lines.append(f"===== {len(failures)} failed, {passed} passed in 3.21s =====")
    return "$ pytest\n[exit code 1, 3.2s]\n--- stdout ---\n" + "\n".join(lines)


def conversation(engine, outputs):
    """system + task, then one assistant tool call and tool result per output."""
    engine.add(Message.system("You are Forge."), Provenance(source=SourceType.SYSTEM), importance=Importance.CRITICAL)
    engine.add(Message.user("Fix the login tests."), Provenance(source=SourceType.USER), importance=Importance.CRITICAL)
    for index, (name, arguments, output) in enumerate(outputs, start=1):
        call = ToolCall(id=f"c{index}", name=name, arguments=arguments)
        engine.add(Message(role="assistant", tool_calls=[call]), Provenance(source=SourceType.MODEL, step=index))
        summary = summarize_output(name, arguments, output) if estimate_tokens(output) > 100 else None
        source = SourceType.TERMINAL if name == "run_command" else SourceType.FILE
        engine.add(
            Message(role="tool", content=output, tool_call_id=call.id),
            Provenance(source=source, source_id=arguments.get("path") or arguments.get("command"), step=index, tool_name=name),
            key=f"file:{arguments['path']}" if name == "read_file" else None,
            changes=[arguments["path"]] if name == "edit_file" else [],
            summary=summary,
        )


# --- Token estimation -----------------------------------------------------------------------


def test_token_estimate_is_deterministic_and_roughly_chars_over_four():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2
    assert estimate_tokens("x" * 4000) == 1000
    message = Message(role="assistant", tool_calls=[ToolCall(id="1", name="read_file", arguments={"path": "a.py"})])
    assert estimate_message_tokens(message) > estimate_message_tokens(Message(role="assistant"))


# --- Compression ----------------------------------------------------------------------------


def test_large_pytest_output_becomes_a_structural_summary():
    failures = ["test_login_wrong_password", "test_session_expiry", "test_missing_user"]
    raw = pytest_output(failures)
    summary = summarize_output("run_command", {"command": "pytest"}, raw)

    assert summary.startswith(HEADER)
    assert "pytest: 3 failed, 10 passed" in summary
    for name in failures:
        assert f"- tests/test_auth.py::{name}" in summary
    assert "exit code 1" in summary
    assert estimate_tokens(summary) < estimate_tokens(raw) / 100


def test_test_summary_is_none_for_non_test_output():
    assert summarize_test_output("hello\nworld") is None


def test_file_and_search_summaries():
    content = "\n".join(f"line {n}" for n in range(500))
    assert "app.py, 500 lines" in summarize_output("read_file", {"path": "app.py"}, content)
    search = "a.py:1: x\na.py:9: x\nb.py:3: x"
    assert "3 matches in 2 files: a.py (2), b.py (1)" in summarize_output("search_text", {"pattern": "x"}, search)


def test_old_large_tool_output_is_compressed_but_recent_output_is_kept():
    engine = ContextEngine(ContextBudget(keep_recent_outputs=1, compress_above_tokens=100))
    raw = pytest_output(["test_a"])
    conversation(engine, [("run_command", {"command": "pytest"}, raw), ("read_file", {"path": "app.py"}, "x = 1\n")])
    view = engine.render()

    old_output = view.messages[3].content
    assert old_output.startswith(HEADER) and "test_a" in old_output
    assert view.messages[5].content == "x = 1\n"
    assert view.tokens < view.full_tokens
    # The full output is still in the item (the record); only what is sent shrinks.
    assert engine.items[3].message.content == raw


# --- Budget ---------------------------------------------------------------------------------


def test_budget_is_enforced_and_critical_items_survive():
    engine = ContextEngine(ContextBudget(max_tokens=2_000, keep_recent_outputs=10, compress_above_tokens=10_000))
    outputs = [("read_file", {"path": f"module_{n}.py"}, f"# module {n}\n" + "x = 1\n" * 1_000) for n in range(6)]
    conversation(engine, outputs)

    view = engine.render()

    assert view.full_tokens > 2_000
    assert view.tokens <= 2_000
    assert view.messages[0].content == "You are Forge."
    assert view.messages[1].content == "Fix the login tests."
    # The latest exchange is never shortened.
    assert view.messages[-1].content == outputs[-1][2]
    # Summaries are tried first; they are enough here.
    assert "summarized" in {item.state for item in view.items}


def test_items_are_omitted_when_summaries_are_not_enough():
    engine = ContextEngine(ContextBudget(max_tokens=2_000, keep_recent_outputs=10, compress_above_tokens=10_000))
    conversation(engine, [])
    for n in range(5):  # outputs without a summary: the only way to shrink them is to omit them
        call = ToolCall(id=f"c{n}", name="read_file", arguments={"path": f"m{n}.py"})
        engine.add(Message(role="assistant", tool_calls=[call]), Provenance(source=SourceType.MODEL, step=n + 1))
        engine.add(
            Message(role="tool", content="z" * 4_000, tool_call_id=call.id),
            Provenance(source=SourceType.FILE, source_id=f"m{n}.py", step=n + 1, tool_name="read_file"),
        )

    view = engine.render()

    assert view.tokens <= 2_000
    assert [item.state for item in view.items if item.state != "full"] == ["omitted"] * 4  # only the latest exchange still fits beside the prompt
    assert "output omitted to fit the context budget (source: file m0.py (step 1))" in view.messages[3].content
    assert view.messages[-1].content == "z" * 4_000


def test_least_important_and_oldest_go_first():
    engine = ContextEngine(ContextBudget(max_tokens=1_500))
    conversation(engine, [])
    big = "y" * 4_000  # about 1,000 tokens each
    engine.add(Message.user(big), Provenance(source=SourceType.SYSTEM), importance=Importance.LOW, key="note:a")
    engine.add(Message.user(big), Provenance(source=SourceType.MEMORY, source_id="m1"), importance=Importance.HIGH)
    engine.add(Message.user("latest"), Provenance(source=SourceType.USER))

    view = engine.render()

    states = [item.state for item in view.items]
    assert states == ["full", "full", "dropped", "full", "full"]


def test_tool_messages_always_keep_their_assistant_call():
    engine = ContextEngine(ContextBudget(max_tokens=2_000))
    conversation(engine, [("read_file", {"path": f"m{n}.py"}, "z" * 4_000) for n in range(5)])
    messages = engine.render().messages
    open_calls = set()
    for message in messages:
        if message.role == "assistant":
            open_calls |= {call.id for call in message.tool_calls}
        if message.role == "tool":
            assert message.tool_call_id in open_calls


# --- Redundancy -----------------------------------------------------------------------------


def test_repeated_read_supersedes_the_earlier_one():
    engine = ContextEngine()
    conversation(engine, [("read_file", {"path": "app.py"}, "old"), ("read_file", {"path": "app.py"}, "new"), ("read_file", {"path": "x.py"}, "x")])
    view = engine.render()
    assert "a later read_file for app.py returned newer output" in view.messages[3].content
    assert view.messages[5].content == "new"


def test_read_before_an_edit_becomes_stale():
    engine = ContextEngine()
    conversation(
        engine,
        [
            ("read_file", {"path": "app.py"}, "before"),
            ("edit_file", {"path": "app.py"}, "Edited app.py"),
            ("read_file", {"path": "other.py"}, "other"),
        ],
    )
    view = engine.render()
    assert "earlier content of app.py omitted because the file was changed later" in view.messages[3].content


def test_superseded_notes_are_dropped():
    engine = ContextEngine()
    conversation(engine, [])
    engine.add(Message.user("checks failed: 3"), Provenance(source=SourceType.VERIFICATION), key="note:verification")
    engine.add(Message.user("checks failed: 1"), Provenance(source=SourceType.VERIFICATION), key="note:verification")
    contents = [message.content for message in engine.render().messages]
    assert "checks failed: 3" not in contents and "checks failed: 1" in contents


# --- Determinism and provenance -----------------------------------------------------------------


def test_rendering_is_deterministic():
    def build():
        engine = ContextEngine(ContextBudget(max_tokens=2_500, keep_recent_outputs=1, compress_above_tokens=50))
        conversation(engine, [("run_command", {"command": "pytest"}, pytest_output(["test_a"], noise_lines=300))] * 3)
        return engine

    first, second = build().render(), build().render()
    assert first.model_dump() == second.model_dump()
    engine = build()
    assert engine.render().model_dump() == engine.render().model_dump()


def test_agent_records_provenance_for_every_item(calculator_project):
    provider = FakeModelProvider(
        responses=[
            tool("c1", "read_file", path="calculator.py"),
            tool("c2", "edit_file", path="calculator.py", old_text=BUG, new_text=FIX),
            tool("c3", "run_command", command="echo done"),
            "Fixed multiply.",
        ]
    )
    outcome = asyncio.run(
        run_task(
            ForgeConfig(workspace=calculator_project, verification="off"),
            "Fix multiply in the calculator",
            provider=provider,
            permissions=PermissionEngine(PermissionPolicy.permissive()),
        )
    )

    manifest = json.loads((TaskStore(Workspace(calculator_project)).directory(outcome.state.task_id) / "context.json").read_text())
    sources = [(item["role"], item["provenance"]["source"], item["provenance"].get("source_id")) for item in manifest]
    assert sources[0] == ("system", "system", "system_prompt")
    assert ("user", "file", None) not in sources
    background = [item for item in manifest if item["key"] == "background:relevant_files"]
    assert background and "calculator.py" in background[0]["provenance"]["source_id"]
    assert ("user", "user", "task") in sources
    assert ("tool", "file", "calculator.py") in sources
    assert ("tool", "terminal", "echo done") in sources
    assert ("assistant", "model", "fake") in sources
    # The task is the last thing before the model's first reply.
    first_assistant = next(n for n, item in enumerate(manifest) if item["role"] == "assistant")
    assert manifest[first_assistant - 1]["provenance"]["source_id"] == "task"


def test_trace_answers_where_information_came_from():
    engine = ContextEngine()
    conversation(engine, [("read_file", {"path": "pyproject.toml"}, 'dependencies = ["psycopg[binary]"]  # PostgreSQL driver')])
    (provenance,) = engine.trace("postgresql")
    assert provenance.source == SourceType.FILE
    assert provenance.source_id == "pyproject.toml"
    assert provenance.describe() == "file pyproject.toml (step 1)"


# --- Retrieval ------------------------------------------------------------------------------


def test_task_keywords():
    assert task_keywords("Fix the parseConfig bug in user_auth") == ["parseconfig", "parse", "config", "bug", "user_auth", "user", "auth"]


def test_relevant_files_by_name_and_text(calculator_project):
    files = find_relevant_files(Workspace(calculator_project), "multiply returns the wrong result in calculator")
    assert files[0].path == "calculator.py"
    assert any("name matches" in reason for reason in files[0].reasons)
    assert "tests/test_calculator.py" in [item.path for item in files]


def test_retrieval_can_be_disabled(calculator_project):
    provider = FakeModelProvider(responses=["nothing to do"])
    config = ForgeConfig(workspace=calculator_project, context={"retrieval": False})
    asyncio.run(run_task(config, "multiply calculator", provider=provider, record=False))
    assert [message.role for message in provider.calls[0]["messages"]] == ["system", "user"]
