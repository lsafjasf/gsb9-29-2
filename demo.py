"""Side-by-side demonstration: legacy per-solver handling vs unified API."""

import math

from app import equilibrium, tank_level, yield_curve
from solver import RootProblem, StopReason, solve


def show(label, result):
    status, x, residual = result
    print("  %-12s status=%-16s x=%-22s residual=%s" % (
        label, status,
        "None" if x is None else ("%.12g" % x),
        "None" if residual is None else "%.3g" % residual,
    ))


def main():
    print("== refactored call sites (one enum branch style each) ==")
    show("tank", tank_level.find_level(0.5))
    show("yield", yield_curve.find_yield(0.0, [-100.0, 10.0, 10.0, 110.0]))
    show("equilibrium", equilibrium.find_equilibrium())

    print("\n== unified engine over one problem, all stop reasons ==")
    cases = [
        ("converged (bisection)",
         RootProblem(f=lambda x: x - math.cos(x), bracket=(0.0, 2.0)),
         dict(tol=1e-10)),
        ("max_iter (bisection)",
         RootProblem(f=lambda x: math.sin(x) - 0.5, bracket=(0.0, 1.0)),
         dict(tol=1e-15, max_iter=3, method="bisection")),
        ("diverged (newton blow-up)",
         RootProblem(
             f=lambda x: math.copysign(abs(x) ** (1/3), x),
             fp=lambda x: (1/3) * abs(x) ** (-2/3),
             x0=1.0,
         ), dict(tol=1e-12)),
        ("invalid_input (bad bracket)",
         RootProblem(f=lambda x: x + 1, bracket=(0.0, 1.0)),
         dict(method="bisection")),
    ]
    for label, problem, kwargs in cases:
        result = solve(problem, **kwargs)
        x_text = "None" if result.x is None else "%.6g" % result.x
        r_text = "None" if result.residual is None else "%.3g" % result.residual
        print("  %-28s reason=%-14s iter=%-3d x=%-14s residual=%-10s %s" % (
            label, result.reason.value, result.iterations,
            x_text, r_text, result.message,
        ))


if __name__ == "__main__":
    main()
