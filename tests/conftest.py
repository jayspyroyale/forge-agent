"""Shared pytest setup. pytest loads this file automatically before running tests."""

import pytest

from forge.config import ENV_VARS

_PROVIDER_KEY_VARS = ["OPENAI_API_KEY", "OLLAMA_API_KEY"]


@pytest.fixture(autouse=True)
def isolated_environment(request, monkeypatch):
    """Hide the developer's own Forge settings and API keys from tests.

    Without this, a test could pass or fail depending on what is set in the
    shell that runs pytest. Live tests are left alone because they need them.
    """
    if request.node.get_closest_marker("live"):
        return
    for name in [*ENV_VARS.values(), *_PROVIDER_KEY_VARS]:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def workspace_root(tmp_path):
    """A workspace directory with a sibling file outside it:

    tmp_path/
        workspace/allowed.txt
        outside.txt
    """
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "allowed.txt").write_text("hello from inside\n", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("secret from outside\n", encoding="utf-8")
    return root
