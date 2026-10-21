import random
import unittest
from dataclasses import FrozenInstanceError
from bonuspro.domain.finance import (
    MAX_MINOR, Rules, allocate_discount, earned_points,
    earning_adjustment, quote, spend_limit,
)


class FinanceTests(unittest.TestCase):
    def test_confirmed_order_example(self):
        result = quote(total_minor=1_000_000, eligible_minor=1_000_000,
                       available_points=5000, requested_points=2000, rules=Rules())
        self.assertEqual((result.payable_minor, result.potential_earn_points),
                         (800_000, 500))

    def test_whole_points_round_down(self):
        self.assertEqual(earned_points(1999, Rules()), 0)
        self.assertEqual(earned_points(2000, Rules()), 1)
        self.assertEqual(earned_points(1_001_999, Rules()), 500)

    def test_limit_balance_and_percentage(self):
        self.assertEqual(spend_limit(1_000_000, 5000, Rules()), 3000)
        self.assertEqual(spend_limit(1_000_000, 123, Rules()), 123)
        self.assertEqual(spend_limit(333, 100, Rules()), 0)

    def test_exclusions_are_respected(self):
        result = quote(total_minor=1_000_000, eligible_minor=500_000,
                       available_points=5000, requested_points=1500, rules=Rules())
        self.assertEqual(result.payable_minor, 850_000)
        self.assertEqual(result.potential_earn_points, 250)

    def test_reject_invalid_numbers(self):
        for value in (-1, True, 1.0, "100", None, MAX_MINOR + 1):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    earned_points(value, Rules())

    def test_rate_limits_and_immutability(self):
        for rate in (-1, 10_001, True, 5.5):
            with self.assertRaises(ValueError):
                Rules(earn_bps=rate)
            with self.assertRaises(ValueError):
                Rules(spend_bps=rate)
        rules = Rules()
        with self.assertRaises(FrozenInstanceError):
            rules.earn_bps = 600
        self.assertEqual(earned_points(10_000, Rules(earn_bps=0)), 0)

    def test_reject_overspend_and_invalid_eligible_base(self):
        for eligible, requested, available in (
            (1_000_000, 3001, 5000), (1_000_000, 101, 100),
            (1_000_001, 0, 0),
        ):
            with self.assertRaises(ValueError):
                quote(total_minor=1_000_000, eligible_minor=eligible,
                      requested_points=requested, available_points=available, rules=Rules())

    def test_allocation_known_rounding_case(self):
        self.assertEqual(allocate_discount(2, {"c": 1, "b": 1, "a": 1}),
                         {"a": 1, "b": 1, "c": 0})
        self.assertEqual(allocate_discount(100, {"a": 100, "b": 300}),
                         {"a": 25, "b": 75})

    def test_zero_and_full_discount(self):
        self.assertEqual(allocate_discount(0, {}), {})
        self.assertEqual(allocate_discount(0, {"a": 0}), {"a": 0})
        self.assertEqual(allocate_discount(8, {"a": 3, "b": 5, "c": 0}),
                         {"a": 3, "b": 5, "c": 0})

    def test_bad_allocation_input(self):
        for discount, lines in ((1, {}), (3, {"a": 2}), (0, {"": 1}),
                                (0, {"a": -1}), (0, {"a": True})):
            with self.assertRaises(ValueError):
                allocate_discount(discount, lines)

    def test_allocation_properties(self):
        rng = random.Random(20261013)
        for _ in range(1000):
            lines = {str(i): rng.randrange(100_000) for i in range(rng.randrange(1, 30))}
            discount = rng.randrange(sum(lines.values()) + 1)
            result = allocate_discount(discount, lines)
            self.assertEqual(sum(result.values()), discount)
            self.assertTrue(all(0 <= result[k] <= lines[k] for k in lines))
            self.assertEqual(result, allocate_discount(discount, dict(reversed(list(lines.items())))))

    def test_return_delta_is_cumulative_and_repeatable(self):
        rules = Rules()
        original = earned_points(6000, rules)
        self.assertEqual(original, 3)
        delta1 = earning_adjustment(remaining_eligible_minor=3999, net_posted_points=original, rules=rules)
        self.assertEqual(delta1, -2)
        posted = original + delta1
        self.assertEqual(earning_adjustment(remaining_eligible_minor=3999,
                                          net_posted_points=posted, rules=rules), 0)
        delta2 = earning_adjustment(remaining_eligible_minor=0, net_posted_points=posted, rules=rules)
        self.assertEqual(original + delta1 + delta2, 0)

    def test_money_boundary_does_not_use_float(self):
        self.assertEqual(earned_points(MAX_MINOR, Rules(earn_bps=10_000)), MAX_MINOR // 100)

    def test_earn_monotonicity(self):
        previous = 0
        for amount in range(0, 100_000, 17):
            points = earned_points(amount, Rules())
            self.assertGreaterEqual(points, previous)
            previous = points


if __name__ == "__main__":
    unittest.main()
