"""Size estimates for context budgeting.

Forge does not ship a tokenizer for every model. It uses a deterministic
estimate of about four characters per token (a common rule of thumb for
English text and code), plus a small overhead per message. The estimate only
needs to be stable and roughly right: the budget leaves headroom.
"""

import json

from forge.models.types import Message

CHARS_PER_TOKEN = 4
MESSAGE_OVERHEAD_TOKENS = 4


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return -(-len(text) // CHARS_PER_TOKEN)  # ceiling division


def estimate_message_tokens(message: Message) -> int:
    tokens = MESSAGE_OVERHEAD_TOKENS + estimate_tokens(message.content)
    for call in message.tool_calls:
        tokens += estimate_tokens(call.name) + estimate_tokens(json.dumps(call.arguments, ensure_ascii=False))
    return tokens
