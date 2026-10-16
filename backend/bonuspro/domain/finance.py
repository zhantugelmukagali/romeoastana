"""Integer-only financial primitives; no persistence or external side effects.

Amounts are KZT minor units (100 = 1 KZT). Points are whole units (1 = 1 KZT).
This module does not prove payment/fulfilment, authorize operations, or mutate wallets.
"""
from dataclasses import dataclass
from collections.abc import Mapping

BASIS_POINTS = 10_000
MINOR_PER_POINT = 100
MAX_MINOR = 9_000_000_000_000_000


def natural(value: int, name: str, maximum: int = MAX_MINOR) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{name} must be a non-negative bounded integer")
    return value


@dataclass(frozen=True)
class Rules:
    earn_bps: int = 500
    spend_bps: int = 3000

    def __post_init__(self) -> None:
        natural(self.earn_bps, "earn_bps", BASIS_POINTS)
        natural(self.spend_bps, "spend_bps", BASIS_POINTS)


def earned_points(eligible_minor: int, rules: Rules) -> int:
    """Target earned points for the caller-verified eligible base, before bonus discount."""
    natural(eligible_minor, "eligible_minor")
    return eligible_minor * rules.earn_bps // (BASIS_POINTS * MINOR_PER_POINT)


def spend_limit(eligible_minor: int, available_points: int, rules: Rules) -> int:
    natural(eligible_minor, "eligible_minor")
    natural(available_points, "available_points")
    return min(
        available_points,
        eligible_minor * rules.spend_bps // (BASIS_POINTS * MINOR_PER_POINT),
    )


@dataclass(frozen=True)
class Quote:
    spend_points: int
    payable_minor: int
    potential_earn_points: int


def quote(
    *,
    total_minor: int,
    eligible_minor: int,
    available_points: int,
    requested_points: int,
    rules: Rules,
) -> Quote:
    natural(total_minor, "total_minor")
    natural(eligible_minor, "eligible_minor")
    natural(requested_points, "requested_points")
    if eligible_minor > total_minor:
        raise ValueError("eligible amount exceeds total")
    if requested_points > spend_limit(eligible_minor, available_points, rules):
        raise ValueError("requested points exceed the available limit")
    return Quote(
        spend_points=requested_points,
        payable_minor=total_minor - requested_points * MINOR_PER_POINT,
        potential_earn_points=earned_points(eligible_minor, rules),
    )


def allocate_discount(discount_minor: int, lines: Mapping[str, int]) -> dict[str, int]:
    """Largest-remainder allocation. Stable line IDs break ties, not input order.

    'lines' contains the caller-verified eligible line totals after ordinary discounts.
    Integration must verify that the external API can represent the resulting amounts.
    This does not select which goods or services participate in the loyalty program.
    """
    natural(discount_minor, "discount_minor")
    if len(lines) > 10_000:
        raise ValueError("too many lines")
    for key, amount in lines.items():
        if not isinstance(key, str) or not key or len(key) > 128:
            raise ValueError("line IDs must be nonempty strings up to 128 characters")
        natural(amount, "line amount")
    total = sum(lines.values())
    natural(total, "line total")
    if discount_minor > total:
        raise ValueError("discount exceeds eligible lines")
    result = {key: 0 for key in sorted(lines)}
    if total == 0:
        return result
    remainders = {}
    for key in result:
        result[key], remainders[key] = divmod(discount_minor * lines[key], total)
    remainder = discount_minor - sum(result.values())
    for key in sorted(result, key=lambda key: (-remainders[key], key))[:remainder]:
        result[key] += 1
    return result


def earning_adjustment(
    *, remaining_eligible_minor: int, net_posted_points: int, rules: Rules
) -> int:
    """Delta against cumulative net accrual, not against the most recent refund.

    Eligibility and return accounting are caller responsibilities. A negative delta
    must never be silently clamped to the current wallet balance.
    """
    natural(net_posted_points, "net_posted_points")
    return earned_points(remaining_eligible_minor, rules) - net_posted_points
