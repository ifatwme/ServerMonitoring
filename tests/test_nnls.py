import itertools
import random

import pytest

from servermon.nnls import _solve, nnls


def ls_objective(A, b, x):
    return sum((sum(a * xi for a, xi in zip(r, x)) - bi) ** 2 for r, bi in zip(A, b))


def brute_force(A, b):
    """Exact NNLS by enumerating every support set (fine for <= 5 columns)."""
    n = len(A[0])
    best, best_x = float("inf"), [0.0] * n
    for k in range(n + 1):
        for cols in itertools.combinations(range(n), k):
            x = [0.0] * n
            if cols:
                AtA = [[sum(r[i] * r[j] for r in A) for j in cols] for i in cols]
                Atb = [sum(r[i] * bi for r, bi in zip(A, b)) for i in cols]
                z = _solve(AtA, Atb)
                if z is None or any(v < 0 for v in z):
                    continue
                for c, v in zip(cols, z):
                    x[c] = v
            f = ls_objective(A, b, x)
            if f < best - 1e-12:
                best, best_x = f, x
    return best_x, best


def test_matches_unconstrained_when_solution_is_positive():
    A = [[1, 0], [0, 1], [1, 1]]
    b = [1, 2, 3]
    x = nnls(A, b)
    assert x == pytest.approx([1, 2], abs=1e-9)


def test_negative_coefficient_is_clamped_to_zero():
    # b = 2*a0 - 1*a1: the constrained optimum drops a1
    A = [[1, 1], [2, 1], [3, 1], [4, 1]]
    b = [1, 3, 5, 7]
    x = nnls(A, b)
    assert x[1] == 0.0
    assert x[0] > 0


@pytest.mark.parametrize("seed", range(40))
def test_random_problems_match_brute_force(seed):
    rng = random.Random(seed)
    n = rng.randint(1, 5)
    m = rng.randint(n, 25)
    A = [[rng.uniform(-1, 2) for _ in range(n)] for _ in range(m)]
    b = [rng.uniform(-1, 3) for _ in range(m)]
    x = nnls(A, b)
    _, best = brute_force(A, b)
    assert all(v >= 0 for v in x)
    assert ls_objective(A, b, x) == pytest.approx(best, rel=1e-6, abs=1e-9)


@pytest.mark.parametrize("seed", range(20))
def test_kkt_conditions(seed):
    rng = random.Random(100 + seed)
    A = [[rng.random() for _ in range(4)] for _ in range(30)]
    b = [rng.random() * 3 for _ in range(30)]
    x = nnls(A, b)
    grad = [sum(r[i] * (bi - sum(a * xi for a, xi in zip(r, x))) for r, bi in zip(A, b)) for i in range(4)]
    for xi, gi in zip(x, grad):
        assert gi <= 1e-7                       # no improving direction
        if xi > 1e-9:
            assert abs(gi) < 1e-6               # complementary slackness


def test_collinear_columns_do_not_loop():
    A = [[1, 2, 2], [2, 4, 4], [3, 6, 6.0000001], [1, 1, 1]]
    b = [1, 2, 3, 1]
    x = nnls(A, b)
    assert all(v >= 0 for v in x)
    assert ls_objective(A, b, x) < 1e-6


def test_empty_matrix_rejected():
    with pytest.raises(ValueError):
        nnls([], [])
