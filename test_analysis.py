import unittest
from collections import Counter
from datetime import date
from pathlib import Path

from analysis import add_months, load_rates, make_cases, settle, settlement_date, stress_summary


class CashFlowTests(unittest.TestCase):
    def test_full_hedge_locks_receipts(self):
        result = settle(100, 100, 1.25, 1.50, cost_bps=0)
        self.assertAlmostEqual(result["forward_gbp"], 80)
        self.assertAlmostEqual(result["spot_gbp"], 0)
        self.assertAlmostEqual(result["cash_gbp"], 80)

    def test_unhedged_receipts_use_spot(self):
        result = settle(100, 0, 1.25, 1.50, cost_bps=0)
        self.assertAlmostEqual(result["cash_gbp"], 100 / 1.50)

    def test_smaller_order_requires_buying_dollars(self):
        result = settle(50, 100, 1.25, 1.20, cost_bps=0)
        self.assertAlmostEqual(result["spot_gbp"], -50 / 1.20)
        self.assertAlmostEqual(result["cash_gbp"], 80 - 50 / 1.20)

    def test_cancelled_order_can_gain_or_lose(self):
        for spot, expected in [(1.50, 80 - 100 / 1.50), (1.00, -20)]:
            with self.subTest(spot=spot):
                result = settle(0, 100, 1.25, spot, cost_bps=0)
                self.assertAlmostEqual(result["cash_gbp"], expected)

    def test_costs_charge_both_legs_without_netting(self):
        result = settle(0, 100, 1.25, 1.00, cost_bps=10)
        self.assertAlmostEqual(result["cost_gbp"], 0.18)
        self.assertAlmostEqual(result["cash_gbp"], -20.18)

    def test_no_order_and_no_contract_produce_no_cash(self):
        result = settle(0, 0, 1.25, 1.50)
        for field in ("forward_gbp", "spot_gbp", "cost_gbp", "cash_gbp"):
            self.assertEqual(result[field], 0)

    def test_invalid_exchange_rates_are_rejected(self):
        for invalid in (0, -1, float("nan"), float("inf")):
            for forward, spot in ((invalid, 1.2), (1.2, invalid)):
                with self.subTest(forward=forward, spot=spot):
                    with self.assertRaises(ValueError):
                        settle(100, 50, forward, spot)


class DateTests(unittest.TestCase):
    def test_calendar_months_clip_at_month_end(self):
        cases = [
            (date(2019, 11, 30), 3, date(2020, 2, 29)),
            (date(2020, 11, 30), 3, date(2021, 2, 28)),
            (date(2020, 1, 31), 3, date(2020, 4, 30)),
        ]
        for start, months, expected in cases:
            with self.subTest(start=start):
                self.assertEqual(add_months(start, months), expected)

    def test_weekend_rolls_forward_then_shifts_observations(self):
        dates = [date(2020, 1, day) for day in (2, 3, 6, 7, 8)]
        target = date(2020, 1, 4)
        self.assertEqual(settlement_date(target, dates), date(2020, 1, 6))
        self.assertEqual(settlement_date(target, dates, -2), dates[0])
        self.assertEqual(settlement_date(target, dates, 2), dates[-1])

    def test_settlement_cannot_leave_available_dates(self):
        dates = [date(2020, 1, day) for day in (2, 3, 6)]
        for target, shift in [(dates[0], -2), (dates[-1], 2), (date(2020, 1, 7), 0)]:
            with self.subTest(target=target, shift=shift):
                with self.assertRaises(ValueError):
                    settlement_date(target, dates, shift)


class HistoricalSampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rates = load_rates(Path(__file__).with_name("rates.csv"))
        cls.cases = make_cases(cls.rates)

    def test_sample_has_42_months_and_fixed_split(self):
        self.assertEqual(len(self.cases), 42)
        self.assertEqual(Counter(row["split"] for row in self.cases),
                         {"development": 24, "check": 18})
        months = [add_months(date(2018, 1, 1), i) for i in range(42)]
        self.assertEqual([row["start"].replace(day=1) for row in self.cases], months)

    def test_each_case_uses_first_valid_quote_and_three_month_target(self):
        for case in self.cases:
            first = min(
                (row for row in self.rates
                 if row["date"].year == case["start"].year
                 and row["date"].month == case["start"].month
                 and row["spot"] is not None and row["forward"] is not None),
                key=lambda row: row["date"],
            )
            with self.subTest(start=case["start"]):
                self.assertEqual(case["start"], first["date"])
                self.assertEqual(case["forward"], first["forward"])
                self.assertEqual(case["target"], add_months(case["start"], 3))


class StressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.summary = stress_summary(1.25, shock=0.20)
        cls.rows = {(r["receipt_floor_usd"], r["hedge_usd"]): r for r in cls.summary}

    def test_half_receipt_floor_matches_hand_calculations(self):
        for hedge, cash in [(0, 33300), (50000, 39960), (100000, 29870)]:
            with self.subTest(hedge=hedge):
                row = self.rows[50000, hedge]
                self.assertAlmostEqual(row["min_cash_gbp"], cash)
                self.assertEqual(row["worst_receipt_usd"], 50000)
        self.assertAlmostEqual(self.rows[50000, 0]["worst_spot"], 1.50)
        self.assertAlmostEqual(self.rows[50000, 100000]["worst_spot"], 1.00)

    def test_certain_full_receipt_and_full_hedge_lock_cash(self):
        row = self.rows[100000, 100000]
        self.assertAlmostEqual(row["min_cash_gbp"], 79920)
        self.assertEqual(row["worst_receipt_usd"], 100000)
        self.assertEqual(row["max_topup_usd"], 0)

    def test_cancellation_without_hedging_leaves_zero_cash(self):
        row = self.rows[0, 0]
        self.assertEqual(row["min_cash_gbp"], 0)
        self.assertEqual(row["worst_receipt_usd"], 0)

    def test_all_nine_choices_report_the_dollar_shortfall(self):
        expected = {(floor, hedge) for floor in (0, 50000, 100000)
                    for hedge in (0, 50000, 100000)}
        self.assertEqual(len(self.summary), 9)
        self.assertEqual(set(self.rows), expected)
        for (floor, hedge), row in self.rows.items():
            with self.subTest(floor=floor, hedge=hedge):
                self.assertEqual(row["max_topup_usd"], max(hedge - floor, 0))

    def test_zero_shock_has_no_fx_effect_but_keeps_conversion_costs(self):
        for row in stress_summary(1.25, cost_bps=0, shock=0):
            self.assertAlmostEqual(row["min_cash_gbp"], row["receipt_floor_usd"] / 1.25)
        rows = {(r["receipt_floor_usd"], r["hedge_usd"]): r
                for r in stress_summary(1.25, cost_bps=10, shock=0)}
        self.assertAlmostEqual(rows[0, 100000]["min_cash_gbp"], -160)
        self.assertAlmostEqual(rows[50000, 100000]["min_cash_gbp"], 39880)
        self.assertAlmostEqual(rows[50000, 50000]["min_cash_gbp"], 39960)

    def test_invalid_shocks_are_rejected(self):
        for shock in (-0.1, 1, float("nan"), float("inf")):
            with self.subTest(shock=shock):
                with self.assertRaises(ValueError):
                    stress_summary(1.25, shock=shock)


if __name__ == "__main__":
    unittest.main()
