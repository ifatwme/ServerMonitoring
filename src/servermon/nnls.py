"""Non-negative least squares (Lawson & Hanson, 1974) for small, dense problems — stdlib only.

    minimise ||A x - b||_2   subject to   x >= 0

The attribution model has at most a handful of columns (intercept + traffic classes), so the
active-set method with normal equations solved by Gaussian elimination is exact and fast enough
(a 2016 x 6 problem solves in a few milliseconds). Columns should be scaled to comparable
magnitudes by the caller; see attribution.fit().
"""
from __future__ import annotations

Matrix = list[list[float]]


def _solve(M: Matrix, v: list[float]) -> list[float] | None:
    """Solve the square system M y = v with partial pivoting; None if singular."""
    n = len(v)
    a = [row[:] + [v[i]] for i, row in enumerate(M)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(a[r][c]))
        if abs(a[p][c]) < 1e-12:
            return None
        a[c], a[p] = a[p], a[c]
        for r in range(c + 1, n):
            f = a[r][c] / a[c][c]
            if f:
                for k in range(c, n + 1):
                    a[r][k] -= f * a[c][k]
    y = [0.0] * n
    for r in range(n - 1, -1, -1):
        y[r] = (a[r][n] - sum(a[r][k] * y[k] for k in range(r + 1, n))) / a[r][r]
    return y


def _ls_on(AtA: Matrix, Atb: list[float], cols: list[int]) -> list[float] | None:
    sub = [[AtA[i][j] for j in cols] for i in cols]
    return _solve(sub, [Atb[i] for i in cols])


def nnls(A: Matrix, b: list[float], max_iter: int | None = None, tol: float = 1e-10) -> list[float]:
    """Return x >= 0 minimising ||A x - b||. A is a list of rows."""
    if not A:
        raise ValueError("empty design matrix")
    n = len(A[0])
    AtA = [[sum(r[i] * r[j] for r in A) for j in range(n)] for i in range(n)]
    Atb = [sum(r[i] * bi for r, bi in zip(A, b)) for i in range(n)]
    return nnls_normal(AtA, Atb, max_iter=max_iter, tol=tol)


def nnls_normal(AtA: Matrix, Atb: list[float], max_iter: int | None = None, tol: float = 1e-10) -> list[float]:
    """NNLS from precomputed normal equations (lets the bootstrap reuse row products)."""
    n = len(Atb)
    max_iter = max_iter or 30 * n
    x = [0.0] * n
    passive: list[int] = []
    excluded: set[int] = set()          # columns found collinear with the passive set
    scale = max(1.0, *(abs(v) for v in Atb))

    def gradient(x):
        return [Atb[i] - sum(AtA[i][j] * x[j] for j in range(n)) for i in range(n)]

    for _ in range(max_iter):
        w = gradient(x)
        cand = [i for i in range(n) if i not in passive and i not in excluded and w[i] > tol * scale]
        if not cand:
            break
        passive.append(max(cand, key=lambda i: w[i]))
        while True:
            z = _ls_on(AtA, Atb, passive)
            if z is None:                       # collinear column: drop the newest one for good
                excluded.add(passive.pop())
                break
            if all(v > tol for v in z):
                for k, i in enumerate(passive):
                    x[i] = z[k]
                for i in range(n):
                    if i not in passive:
                        x[i] = 0.0
                break
            # step back towards feasibility, drop the variables that hit zero
            alpha = min(x[i] / (x[i] - z[k]) for k, i in enumerate(passive) if z[k] <= tol)
            for k, i in enumerate(passive):
                x[i] += alpha * (z[k] - x[i])
            passive = [i for i in passive if x[i] > tol]
            for i in range(n):
                if i not in passive:
                    x[i] = 0.0
            if not passive:
                break
    return x
