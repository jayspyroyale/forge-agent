"""Built-in configuration profiles.

A profile is a partial config applied on top of the defaults and below
everything the user sets explicitly. Profiles adjust today's settings (step
limits, verification, permissions) and already carry the reserved
`exploration` settings that a future branching phase will read.
"""

from typing import Any

BUILTIN_PROFILES: dict[str, dict[str, Any]] = {
    "cheap": {
        "agent": {"max_steps": 10, "verification_attempts": 1},
        "context": {"max_tokens": 24_000, "keep_recent_outputs": 4},
        "exploration": {"approaches": 1, "budget_usd": 0.10, "weights": {"cost": 1.0, "latency": 0.5}},
    },
    "balanced": {},
    "production": {
        "agent": {"max_steps": 30, "verification": "auto", "verification_attempts": 3},
        "permissions": {"write": "ask", "execute": "ask", "dangerous": "deny"},
        "exploration": {"weights": {"correctness": 1.0, "safety": 1.0, "cost": 0.2, "latency": 0.1}},
    },
    "maximum-quality": {
        "agent": {"max_steps": 50, "verification": "auto", "verification_attempts": 5},
        "context": {"max_tokens": 128_000},
        "exploration": {
            "approaches": 3,
            "selection_mode": "recommend",
            "weights": {"correctness": 1.0, "safety": 1.0, "cost": 0.0, "latency": 0.0},
        },
    },
}

DEFAULT_PROFILE = "balanced"
