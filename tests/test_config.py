from pathlib import Path

import pytest
from pydantic import ValidationError

from forge.config import ForgeConfig


def test_defaults():
    config = ForgeConfig()
    assert config.workspace == Path.cwd()
    assert config.max_steps == 20
    assert config.debug is False


def test_max_steps_must_be_positive():
    with pytest.raises(ValidationError):
        ForgeConfig(max_steps=0)
