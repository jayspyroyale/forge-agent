import pytest

from forge.agent import prompts
from forge.agent.prompts import MODES, build_system_prompt
from forge.agent.runtime import create_agent
from forge.config import ForgeConfig
from forge.models.providers.fake import FakeModelProvider


def test_coding_prompt_contains_every_policy_section():
    prompt = build_system_prompt("coding")
    for section in [prompts.IDENTITY, prompts.TOOL_USE, prompts.CODING_DISCIPLINE, prompts.HONESTY]:
        assert section in prompt
    assert "Verifying:" in prompt


def test_coding_prompt_covers_the_core_behaviors():
    prompt = build_system_prompt("coding").lower()
    for phrase in [
        "read a file before you edit it",
        "instead of guessing",
        "smallest change",
        "do not touch unrelated code",
        "existing structure",
        "read the error and change your approach",
        "never say tests pass unless you ran them",
        "explain the blocker",
    ]:
        assert phrase in prompt, phrase


def test_environment_section_lists_workspace_and_tools():
    prompt = build_system_prompt("coding", workspace="/projects/demo", tool_names=["read_file", "edit_file"])
    assert "Workspace root: /projects/demo" in prompt
    assert "Tools: read_file, edit_file" in prompt
    assert "run_command uses" in prompt


def test_verification_commands_are_mentioned_when_known():
    prompt = build_system_prompt("coding", verification_commands=["python -m pytest -q"])
    assert "`python -m pytest -q`" in prompt
    assert "{commands}" not in build_system_prompt("coding")


def test_modes_are_compositions_of_sections():
    answer = build_system_prompt("answer")
    assert prompts.CODING_DISCIPLINE not in answer
    assert prompts.TOOL_USE in answer
    assert set(MODES) >= {"coding", "answer"}


def test_unknown_mode():
    with pytest.raises(ValueError, match="Unknown prompt mode"):
        build_system_prompt("pirate")


def test_prompt_is_concise():
    assert len(build_system_prompt("coding")) < 3000


def test_runtime_sends_the_coding_prompt_first(workspace_root):
    provider = FakeModelProvider(responses=["done"])
    agent = create_agent(ForgeConfig(workspace=workspace_root), provider=provider)

    import asyncio

    asyncio.run(agent.run("task"))

    first = provider.calls[0]["messages"][0]
    assert first.role == "system"
    assert prompts.CODING_DISCIPLINE in first.content
    assert str(workspace_root.resolve()) in first.content
    assert "edit_file" in first.content
