from app import median

assert median([3, 1, 2]) == 2
assert median([4, 1, 3, 2]) == 2.5
try:
    median([])
except ValueError:
    pass
else:
    raise AssertionError("empty input must fail")
