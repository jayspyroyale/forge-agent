from app import unique


class Value:
    comparisons = 0

    def __init__(self, n):
        self.n = n

    def __hash__(self):
        return hash(self.n)

    def __eq__(self, other):
        Value.comparisons += 1
        return self.n == other.n


assert unique([3, 1, 3, 2]) == [3, 1, 2]
assert unique([]) == []
assert len(unique([Value(n) for n in range(1000)])) == 1000
assert Value.comparisons < 5000, "quadratic comparison count"
