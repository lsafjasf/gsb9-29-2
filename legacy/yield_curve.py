"""Call site 2 (legacy): internal rate of return (IRR) via Newton.

Price P of an annual cash-flow stream c_0..c_n discounted at rate r::

    P(r) = sum_k c_k / (1 + r) ** k

Solve P(r) - market_price = 0. ``None`` from the solver means overflow.
"""

from legacy.newton_solver import newton


def find_yield(price, cash_flows, guess=0.1, max_iter=50, eps=1e-8):
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

    try:
        result = newton(
            present_value, derivative, guess, max_iter=max_iter, eps=eps
        )
    except ZeroDivisionError:
        return "stalled", guess, None
    except FloatingPointError:
        return "blew-up", None, None
    except ValueError:
        return "bad-guess", guess, None
    if result is None:
        return "did-not-converge", None, None
    return "ok", result["x"], abs(result["fx"])
