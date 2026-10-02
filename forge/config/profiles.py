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
        "exploration": {"approaches": 1, "budget_usd": 0.10, "weights": {"correctness": 50, "cost": 40, "speed": 10}},
    },
    "balanced": {},
    "production": {
        "agent": {"max_steps": 30, "verification": "auto", "verification_attempts": 3},
        "permissions": {"write": "ask", "execute": "ask", "dangerous": "deny"},
        "exploration": {
            "selection_mode": "assisted",
            "weights": {"correctness": 50, "safety": 30, "cost": 5, "speed": 5, "simplicity": 10},
            "constraints": {"tests_must_pass": True, "security_checks_must_pass": True},
        },
    },
    "maximum-quality": {
        "agent": {"max_steps": 50, "verification": "auto", "verification_attempts": 5},
        "context": {"max_tokens": 128_000},
        "exploration": {
            "approaches": 3,
            "selection_mode": "assisted",
            "weights": {"correctness": 50, "safety": 20, "simplicity": 5, "maintainability": 15, "scalability": 10},
        },
    },
}

DEFAULT_PROFILE = "balanced"
