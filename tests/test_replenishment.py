from argparse import Namespace
from datetime import datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from jushuitan import LOCAL_TZ, QueryError
from test_queries import COMPANY_ID, USER_ID
from jst_replenishment import (arrival_batches, demand_baseline, effective_product, load_scenario,
                              plan_product, run_plan, simulate, validate_plan_options)

AS_OF = datetime(2026, 1, 1, tzinfo=LOCAL_TZ)


def product(**changes):
    return {"sku_id": "A", "name": "A", "sku_type": "normal", "enabled": True,
            "business_type": "finished", "ownership": "own", "unit": "个", "unit_source": "erp",
            "physical_after_order_qty": 50, "stock_lock_present": False, "rule_source": "业务确认", **changes}


def history(qtys=None, days=56):
    qtys = qtys if qtys is not None else [10]*days
    start = AS_OF-timedelta(days=len(qtys))
    return [{"date": (start+timedelta(days=i)).date().isoformat(), "qty": q, "partial_day": False}
            for i, q in enumerate(qtys)]


def policy(**changes):
    return {"mode": "purchase", "lead_days": 20, "review_days": 10, "safety_days": 0,
            "pack_multiple": 1, "moq": 0, "basis": "confirmed", "source": "业务负责人确认的试点参数", **changes}


def source(**changes):
    return {"kind": "purchase", "document_id": 1, "line_id": 11, "sku_id": "A",
            "remaining_qty": 300, "eligible_book_remaining_qty": 300,
            "expected_date": None, "quality_issues": [], **changes}


def batch(**changes):
    return {"kind": "purchase", "document_id": 1, "line_id": 11, "batch_id": "batch-1", "qty": 300,
            "available_date": "2026-01-20", "unit": "个", "stock_scope": "main_public",
            "basis": "confirmed", "source": "供应商及仓库确认可销售批次", **changes}


def scenario(**changes):
    return {"version": 1, "company_id": COMPANY_ID, "name": "构造验收", "skus": {"A": policy()},
            "arrivals": [batch()], **changes}


def plan(**changes):
    data = {"item": product(), "series": history(), "supply_rows": [source()], "batches": [batch()],
            "policy": policy(), "as_of": AS_OF, "horizon": 30, "largest_order_qty": Decimal(10), **changes}
    return plan_product(**data)


class ReplenishmentTests(unittest.TestCase):
    def test_daily_calendar_and_backtest_use_only_earlier_dates(self):
        rate, info = demand_baseline(history([10]*28+[20]*7))
        self.assertEqual(rate, Decimal("12.5"))
        self.assertEqual(info["demand_backtest"]["fold_count"], 1)
        self.assertEqual(info["demand_backtest"]["mae_week_qty"], 70)
        self.assertEqual(info["demand_backtest"]["bias_week_qty"], -70)
        self.assertEqual(info["comparison_daily_means"]["7"], 20)
        zero, info = demand_baseline(history([0]*56))
        self.assertEqual(zero, 0)
        self.assertIsNone(info["demand_backtest"]["wape_percent"])
        for change in ("partial", "gap", "negative"):
            rows = history()
            if change == "partial": rows[-1]["partial_day"] = True
            if change == "gap": rows.pop(20)
            if change == "negative": rows[-1]["qty"] = -1
            with self.subTest(change=change), self.assertRaises(QueryError): demand_baseline(rows)

    def test_late_supply_covers_total_but_leaves_earlier_gap(self):
        row = plan()
        self.assertEqual(row["raw_replenishment_qty"], 0)
        self.assertEqual(row["horizon_simulation"]["first_gap_date"], "2026-01-06")
        self.assertEqual(row["gap_before_new_supply_qty"], 140)
        self.assertFalse(row["risk_resolved_by_total_quantity"])
        self.assertEqual(row["latest_order_date_scenario"], "2025-12-17")

    def test_negative_stock_preserved_without_double_deducting_history(self):
        row = plan(item=product(physical_after_order_qty=-10), batches=[],
                   policy=policy(lead_days=1, review_days=1))
        self.assertEqual(row["raw_replenishment_qty"], 30)
        self.assertEqual(row["horizon_simulation"]["first_gap_date"], "2026-01-01")
        self.assertEqual(row["gap_before_new_supply_qty"], 20)

    def test_partial_today_and_last_day_have_exact_horizon_demand(self):
        as_of = AS_OF.replace(hour=12)
        result = simulate(as_of, Decimal(50), Decimal(10), [], 2)
        self.assertEqual([x["duration_days"] for x in result["timeline"]], [0.5, 1, 0.5])
        self.assertEqual(result["new_order_demand_qty"], 20)
        self.assertEqual(result["end_projected_qty"], 30)

    def test_moq_and_pack_adjustment_does_not_force_purchase_when_zero(self):
        row = plan(item=product(physical_after_order_qty=20), batches=[],
                   policy=policy(lead_days=1, review_days=2, safety_days=1, moq=25, pack_multiple=12))
        self.assertEqual(row["raw_replenishment_qty"], 20)
        self.assertEqual(row["rounded_replenishment_qty"], 36)
        row = plan(policy=policy(moq=100, pack_multiple=12))
        self.assertEqual(row["rounded_replenishment_qty"], 0)

    def test_rounding_for_display_does_not_change_order_quantity(self):
        row = plan(item=product(physical_after_order_qty=Decimal("19.999999")), batches=[],
                   policy=policy(lead_days=1, review_days=1))
        self.assertGreater(row["raw_replenishment_qty"], 0)
        self.assertEqual(row["rounded_replenishment_qty"], 1)

    def test_missing_stock_unit_ownership_and_kit_mapping_block_numerical_plan(self):
        for changes in ({"unit": None}, {"ownership": "unknown"}, {"physical_after_order_qty": None},
                        {"business_type": "raw_material"}, {"sku_type": "combine"},
                        {"stock_lock_present": True}, {"enabled": False}):
            with self.subTest(changes=changes):
                row = plan(item=product(**changes))
                self.assertEqual(row["analysis_status"], "needs_data")
                self.assertIsNone(row["horizon_simulation"])
                self.assertIsNone(row["raw_replenishment_qty"])

    def test_explicit_assumptions_do_not_change_confirmed_rules_or_override_known_facts(self):
        item = product(ownership="unknown", business_type="unknown", unit=None)
        p = policy(basis="assumption", sku_assumptions={"ownership": "own", "business_type": "finished", "unit": "个"})
        row = plan(item=item, policy=p)
        self.assertEqual(row["analysis_status"], "assumption_scenario")
        self.assertEqual(item["ownership"], "unknown")
        self.assertEqual(row["unit_source"], "assumption")
        with self.assertRaises(QueryError): effective_product(product(ownership="customer"), p)

    def test_missing_policy_keeps_quantity_unset_and_pending_supply_visible(self):
        row = plan(policy={}, batches=[])
        self.assertIsNone(row["raw_replenishment_qty"])
        self.assertIn("lead_days", row["missing_parameters"])
        self.assertEqual(row["unresolved_supply_line_count"], 1)
        self.assertIn("pending_existing_supply_not_deducted", row["limitations"])

    def test_batch_allocation_preserves_source_and_does_not_auto_use_erp_date(self):
        rows = [source(expected_date="2026-01-02T00:00:00+08:00")]
        batches, checks, unresolved = arrival_batches(rows, scenario(arrivals=[]), {"A": product()}, AS_OF)
        self.assertEqual(dict(batches), {})
        self.assertEqual(unresolved[0]["unallocated_qty"], 300)
        batches, checks, unresolved = arrival_batches(rows, scenario(), {"A": product()}, AS_OF)
        self.assertTrue(checks[0]["included"])
        self.assertEqual(batches["A"][0]["document_id"], 1)
        self.assertEqual(unresolved, [])

    def test_reversed_invalid_unit_and_past_arrivals_stay_unresolved(self):
        for rows, config in (([source(eligible_book_remaining_qty=None)], scenario()),
                             ([source()], scenario(arrivals=[batch(unit="箱")])),
                             ([source()], scenario(arrivals=[batch(available_date="2025-12-31")]))):
            with self.subTest(config=config):
                batches, checks, unresolved = arrival_batches(rows, config, {"A": product()}, AS_OF)
                self.assertFalse(checks[0]["included"])
                self.assertEqual(dict(batches), {})
                self.assertEqual(unresolved[0]["unallocated_qty"], 300)

    def test_batches_cannot_exceed_book_remaining_or_reference_unknown_source(self):
        for config in (scenario(arrivals=[batch(qty=301)]), scenario(arrivals=[batch(line_id=999)]),
                       scenario(arrivals=[batch(qty=200), batch(batch_id="batch-2", qty=101)])):
            with self.subTest(config=config), self.assertRaises(QueryError):
                arrival_batches([source()], config, {"A": product()}, AS_OF)

    def test_scenario_file_source_company_assumptions_and_duplicates_are_checked(self):
        with TemporaryDirectory() as folder:
            path = Path(folder)/"scenario.json"
            for bad in (scenario(company_id="other"), scenario(arrivals=[batch(), batch()]),
                        scenario(skus={"A": policy(source="")}),
                        scenario(skus={"A": policy(sku_assumptions={"unit": "个"})}),
                        scenario(skus={"A": policy(pack_multiple=0)}),
                        scenario(arrivals=[batch(stock_scope="other")] )):
                path.write_text(json.dumps(bad))
                with self.subTest(bad=bad), self.assertRaises(QueryError): load_scenario(COMPANY_ID, path)
            path.write_text(json.dumps(scenario()))
            self.assertEqual(load_scenario(COMPANY_ID, path)["skus"]["A"]["lead_days"], 20)

    def test_run_uses_full_days_and_preserves_scope_before_sku_display_filter(self):
        args = Namespace(command="replenishment-plan", month=None, days=7, horizon=30,
                         scenario_file=None, sku_rules=None, sku="A")
        supply = {"rows": [], "source_reads": [], "header_counts": {}, "row_count": 0,
                  "receipt_row_count": 0, "quality_counts": {}}
        client = Mock(company_id=COMPANY_ID, user_id=USER_ID)
        instant = AS_OF.replace(hour=12)
        with patch("jst_replenishment.now", return_value=instant), \
             patch("jst_replenishment.read_catalog", return_value=[product(), product(sku_id="B", physical_after_order_qty=-1)]), \
             patch("jst_replenishment.read_sales", return_value=([], {"queried_order_count": 0} )) as read, \
             patch("jst_replenishment.run_supply", return_value=supply):
            result = run_plan(client, args)
        self.assertEqual(read.call_args.args[2].hour, 0)
        self.assertEqual(result["training_end_exclusive"], "2026-01-01T00:00:00+08:00")
        self.assertEqual(result["full_candidate_count"], 2)
        self.assertEqual(result["row_count"], 1)
        self.assertFalse(result["execution_ready"])

    def test_invalid_windows_are_rejected(self):
        for changes in ({"month": "2026-01"}, {"days": 6}, {"days": 91}, {"horizon": 0}, {"horizon": 366}):
            args = Namespace(**{"month": None, "days": None, "horizon": None, **changes})
            with self.subTest(changes=changes), self.assertRaises(QueryError): validate_plan_options(args)

    def test_unmatched_historical_sku_keeps_demand_evidence_without_inventory_plan(self):
        args = Namespace(command="replenishment-plan", month=None, days=7, horizon=30,
                         scenario_file=None, sku_rules=None, sku=None)
        supply = {"rows": [], "source_reads": [], "header_counts": {}, "row_count": 0,
                  "receipt_row_count": 0, "quality_counts": {}}
        daily = [{"sku_id": "MISSING", "name": "历史SKU", "sku_type": "normal", "qty": 7,
                  "daily": history([1]*7)}]
        with patch("jst_replenishment.now", return_value=AS_OF), \
             patch("jst_replenishment.read_catalog", return_value=[product()]), \
             patch("jst_replenishment.read_sales", return_value=([], {})), \
             patch("jst_replenishment.sales_daily", return_value=daily), \
             patch("jst_replenishment.run_supply", return_value=supply):
            result = run_plan(Mock(company_id=COMPANY_ID, user_id=USER_ID), args)
        self.assertEqual(result["join_checks"]["unmatched_skus"], ["MISSING"])
        self.assertEqual(result["unmatched_demand"][0]["history_order_qty"], 7)
        self.assertEqual(result["rows"], [])


if __name__ == "__main__":
    unittest.main()
