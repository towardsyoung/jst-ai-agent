from argparse import Namespace
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from jushuitan import LOCAL_TZ, QueryError, sales_summary
from jst_analysis import (load_rules, read_catalog, read_sales, run_analysis, sales_daily,
                          stock_sales, validate_options, present_result)
from test_queries import COMPANY_ID, ItemPager, item, order


START = datetime(2026, 10, 1, tzinfo=LOCAL_TZ)
END = datetime(2026, 10, 3, 12, tzinfo=LOCAL_TZ)


def product(sku="A", **changes):
    return {"sku_id": sku, "i_id": sku, "name": sku, "sku_type": "normal", "enabled": True,
            "business_type": "finished", "ownership": "own", "unit": "个",
            "physical_after_order_qty": 20, "qty": 20, "stock_lock_present": False, **changes}


def args(command="stock-sales", **changes):
    return Namespace(command=command, month="2026-10", days=None, sku=None,
                     sku_rules=None, output=None, limit=1, **changes)


class AnalysisTests(unittest.TestCase):
    def test_daily_reuses_filters_and_covers_zero_and_partial_days(self):
        orders = [order(1, [item(qty=4)], order_date="2026-10-01 10:00:00"),
                  order(2, [item(qty=9, is_gift=True, oi=2)], "WaitPay"),
                  order(3, [item(qty=6, oi=3)], order_date="2026-10-03 10:00:00"),
                  order(4, [item(qty=7, oi=4)], "Split")]
        client = ItemPager([])
        with patch.object(client, "orders_in_period", return_value=orders):
            lines, stats = read_sales(client, START, END)
        rows = sales_daily(lines, START, END)
        self.assertEqual([d["qty"] for d in rows[0]["daily"]], [4, 0, 6])
        self.assertEqual([d["partial_day"] for d in rows[0]["daily"]], [False, False, True])
        self.assertEqual(rows[0]["qty"], sales_summary(orders)["total_qty"])
        self.assertEqual(stats["valid_order_count"], 2)
        self.assertEqual(set(lines[0]), {"order_id", "line_id", "order_date", "sku_id", "i_id",
                                       "name", "sku_type", "qty", "shop_id"})

    def test_actual_stock_does_not_use_virtual_or_double_deduct_orders(self):
        catalog = [product(qty=30, physical_after_order_qty=20, orderable=100, order_lock=10)]
        daily = [{"sku_id": "A", "sku_type": "normal", "qty": 25,
                  "daily": [{"date": "2026-10-01", "qty": 25}]}]
        rows, unmatched = stock_sales(catalog, daily, START, END)
        self.assertEqual(rows[0]["average_daily_order_qty"], 10)
        self.assertEqual(rows[0]["coverage_days_scenario"], 2)
        self.assertEqual(unmatched, [])

    def test_unknown_unit_ownership_and_locked_stock_do_not_generate_coverage(self):
        daily = [{"sku_id": "A", "sku_type": "normal", "qty": 25, "daily": []}]
        for changes in ({"unit": None}, {"ownership": "unknown"},
                        {"business_type": "unknown"}, {"stock_lock_present": True},
                        {"physical_after_order_qty": None}):
            with self.subTest(changes=changes):
                rows, _ = stock_sales([product(**changes)], daily, START, END)
                self.assertIsNone(rows[0]["coverage_days_scenario"])
                self.assertEqual(rows[0]["analysis_status"], "needs_review")

    def test_candidate_flags_are_not_stagnation_or_asset_value(self):
        rows, _ = stock_sales([product(), product("FEE", business_type="service"),
                              product("CLIENT", ownership="customer")], [], START, END)
        by_sku = {r["sku_id"]: r for r in rows}
        self.assertEqual(by_sku["A"]["candidate_flags"], ["stock_without_period_sales"])
        self.assertEqual(by_sku["FEE"]["candidate_flags"], [])
        self.assertEqual(by_sku["CLIENT"]["candidate_flags"], [])
        self.assertNotIn("asset_value", by_sku["A"])

    def test_bundle_and_components_stay_separate_with_unmatched_sales_visible(self):
        daily = [{"sku_id": "OLD", "sku_type": "normal", "qty": 1, "daily": []}]
        rows, unmatched = stock_sales([product(), product("SET", sku_type="combine", qty=None)],
                                     daily, START, END)
        self.assertEqual(len(rows), 2)
        self.assertEqual(unmatched, ["OLD"])
        wrong = [{"sku_id": "A", "sku_type": "combine", "qty": 1, "daily": []}]
        with self.assertRaises(QueryError): stock_sales([product()], wrong, START, END)

    def test_catalog_missing_is_not_zero_and_formula_mismatch_stops(self):
        raw = {"sku_id": "A", "i_id": "A", "name": "快递费", "co_id": COMPANY_ID,
               "enabled": 1, "sku_type": "normal", "qty": None, "order_lock": None,
               "virtual_qty": None, "orderable": 0}
        client = ItemPager([raw])
        with patch.object(client, "all_table_rows", return_value=[]):
            catalog = read_catalog(client, {})
            self.assertIsNone(catalog[0]["physical_after_order_qty"])
            self.assertEqual(catalog[0]["type_hint"], "service")
            self.assertEqual(catalog[0]["business_type"], "unknown")
        broken = {**raw, "qty": 10, "order_lock": 2, "virtual_qty": 80, "orderable": 100}
        with patch.object(client, "all_items", return_value=[broken]), \
                patch.object(client, "all_table_rows", return_value=[]), self.assertRaises(QueryError):
            read_catalog(client, {})

    def test_rules_require_company_and_confirmation_source(self):
        with TemporaryDirectory() as folder:
            p = Path(folder) / "rules.json"
            for rule in ({"business_type": "finished"}, {"business_type": "invented", "source": "owner"},
                         {"ownership": "own", "source": "owner", "unit": ""}):
                p.write_text(json.dumps({"version": 1, "company_id": COMPANY_ID, "skus": {"A": rule}}))
                with self.assertRaises(QueryError): load_rules(COMPANY_ID, p)
            p.write_text(json.dumps({"version": 1, "company_id": COMPANY_ID,
                                     "skus": {"A": {"business_type": "finished", "ownership": "own",
                                                    "unit": "个", "source": "工厂已确认口径"}}}))
            self.assertEqual(load_rules(COMPANY_ID, p)["A"]["ownership"], "own")

    def test_limit_only_truncates_display_and_report_never_overwrites(self):
        result = {"ok": True, "complete": True, "rows": [{"sku_id": "A"}, {"sku_id": "B"}]}
        with TemporaryDirectory() as folder:
            options = args()
            options.output = str(Path(folder) / "report.json")
            with patch("builtins.print") as out:
                present_result(result, options)
            preview = json.loads(out.call_args.args[0])
            self.assertTrue(preview["display_truncated"])
            self.assertEqual(len(json.loads(Path(options.output).read_text())["rows"]), 2)
            with self.assertRaises(QueryError): present_result(result, options)

    def test_request_range_limits(self):
        options = args()
        options.days = 30
        with self.assertRaises(QueryError): validate_options(options)
        options.month = None
        options.days = 91
        with self.assertRaises(QueryError): validate_options(options)

    def test_join_totals_checked_before_display_filter(self):
        raw = {"sku_id": "A", "i_id": "A", "name": "A", "co_id": COMPANY_ID,
               "enabled": 1, "sku_type": "normal", "qty": 10, "order_lock": 0,
               "virtual_qty": 0, "orderable": 10}
        client = ItemPager([raw])
        options = args()
        options.sku = "A"
        with patch.object(client, "all_table_rows", return_value=[]), \
                patch.object(client, "orders_in_period", return_value=[order()]), \
                patch("jst_analysis.now", return_value=END):
            result = run_analysis(client, options)
        self.assertEqual(result["join_checks"]["sold_sku_count"], 1)
        self.assertEqual(result["row_count"], 1)
        self.assertEqual(result["rows"][0]["period_sales_qty"], 1)


if __name__ == "__main__":
    unittest.main()
