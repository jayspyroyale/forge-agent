"""A tiny calculator with a deliberate bug, used to exercise Forge end to end."""


def add(a, b):
    return a + b


def subtract(a, b):
    return a - b


def multiply(a, b):
    return a + b  # BUG: should be a * b
