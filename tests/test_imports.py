import importlib
import pkgutil

import pytest

import forge


def all_forge_modules() -> list[str]:
    """Every module in the forge package, discovered automatically."""
    names = ["forge"]
    for module in pkgutil.walk_packages(forge.__path__, prefix="forge."):
        if module.name != "forge.__main__":
            names.append(module.name)
    return sorted(names)


MODULES = all_forge_modules()


def test_expected_modules_are_discovered():
    for name in ["forge.cli", "forge.config", "forge.models.registry", "forge.tools.executor"]:
        assert name in MODULES


@pytest.mark.parametrize("module_name", MODULES)
def test_module_imports(module_name):
    importlib.import_module(module_name)


def test_version_is_a_string():
    assert isinstance(forge.__version__, str)
    assert forge.__version__
