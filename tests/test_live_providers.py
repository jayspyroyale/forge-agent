"""Optional tests against real model servers.

These never run with plain `pytest` (see `addopts` in pyproject.toml).
Run them with:

    pytest -m live

Each test also skips itself unless its provider is configured:
- OpenAI: set OPENAI_API_KEY
- Ollama: set FORGE_LIVE_OLLAMA_MODEL to a model you have pulled (e.g. gemma4:31b)
"""

import asyncio
import os

import pytest

from forge.config import ForgeConfig
from forge.models.registry import create_provider
from forge.models.types import Message

pytestmark = pytest.mark.live

PROMPT = "Reply with exactly FORGE_OK and nothing else."


@pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="OPENAI_API_KEY is not set")
def test_openai_live():
    provider = create_provider(ForgeConfig(provider="openai"))
    response = asyncio.run(provider.generate([Message.user(PROMPT)]))
    assert "FORGE_OK" in response.content


@pytest.mark.skipif(
    not os.environ.get("FORGE_LIVE_OLLAMA_MODEL"), reason="FORGE_LIVE_OLLAMA_MODEL is not set"
)
def test_ollama_live():
    config = ForgeConfig(provider="ollama", model=os.environ["FORGE_LIVE_OLLAMA_MODEL"], timeout=600)
    provider = create_provider(config)
    response = asyncio.run(provider.generate([Message.user(PROMPT)]))
    assert "FORGE_OK" in response.content
    assert response.usage is not None
