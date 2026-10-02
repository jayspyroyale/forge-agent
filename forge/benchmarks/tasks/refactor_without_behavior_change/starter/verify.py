from app import total

assert total([1, None, 3]) == 4
assert total([]) == 0
assert total([-2, 5]) == 3
