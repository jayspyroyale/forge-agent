import importlib

import pytest

MODULES = [
    "forge",
    "forge.cli",
    "forge.config",
    "forge.doctor",
    "forge.agent",
    "forge.agent.loop",
    "forge.agent.state",
    "forge.agent.prompts",
    "forge.models",
    "forge.models.base",
    "forge.models.registry",
    "forge.tools",
    "forge.tools.base",
    "forge.tools.registry",
    "forge.security",
    "forge.security.permissions",
]


@pytest.mark.parametrize("module_name", MODULES)
def test_module_imports(module_name):
    importlib.import_module(module_name)


def test_version_is_a_string():
    import forge

    assert isinstance(forge.__version__, str)
    assert forge.__version__
