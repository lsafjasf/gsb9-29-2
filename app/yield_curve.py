"""Call site 2 (refactored): internal rate of return (IRR) via Newton."""

from solver import RootProblem, StopReason, solve


def find_yield(price, cash_flows, guess=0.1, max_iter=50, tol=1e-8):
    def present_value(rate):
        total = 0.0
        for k, cash in enumerate(cash_flows):
            total += cash / (1.0 + rate) ** k
        return total - price

    def derivative(rate):
        total = 0.0
        for k, cash in enumerate(cash_flows):
            if k > 0:
                total += -k * cash / (1.0 + rate) ** (k + 1)
        return total

    problem = RootProblem(f=present_value, fp=derivative, x0=guess)
    result = solve(problem, tol=tol, max_iter=max_iter, method="newton")

    if result.reason is StopReason.CONVERGED:
        return "ok", result.x, result.residual
    if result.reason is StopReason.MAX_ITER:
        return "did-not-converge", result.x, result.residual
    if result.reason is StopReason.DIVERGED:
        if "derivative evaluated to zero" in result.message:
            return "stalled", result.x, result.residual
        return "blew-up", result.x, result.residual
    return "bad-guess", guess, None
