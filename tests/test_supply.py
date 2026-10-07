from argparse import Namespace
from contextlib import redirect_stdout
from datetime import datetime
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from jushuitan import LOCAL_TZ, Client, QueryError, MANUFACTURE_MATERIAL_URL, PURCHASE_ITEM_URL
from test_queries import COMPANY_ID, USER_ID, test_session
from jst_analysis import present_result
from jst_supply import read_headers, read_materials, reconcile_document, run_supply, validate_supply_options

AS_OF = datetime(2026, 10, 7, 12, tzinfo=LOCAL_TZ)
RULES = {"A": {"unit": "个", "ownership": "own", "business_type": "finished", "source": "业务确认"}}


def header(**changes):
    return {"po_id": 1, "co_id": COMPANY_ID, "status": "已确认", "receive_status": "部分入库",
            "is_archive": False, "po_date": "2026-04-01 12:00:00", "modified": "2026-10-06 10:00:00",
            "qty_count": 50, "remark": "", **changes}


def item(**changes):
    return {"po_id": 1, "poi_id": 11, "co_id": COMPANY_ID, "sku_id": "A", "name": "A",
            "qty": 50, "diffQty": 30, "ioQty": 20, **changes}


def receipt(**changes):
    return {"io_id": 20, "ioi_id": 21, "co_id": COMPANY_ID, "sku_id": "A", "type": "采购进仓",
            "qty": 20, "created": "2026-10-01 10:00:00", "error_in": False, **changes}


def options(**changes):
    return Namespace(**{"command": "purchase-lines", "kind": None, "document_id": None, "age_days": None,
                        "month": None, "days": None, "sku": None, "sku_rules": None, "output": None, "limit": 1, **changes})


def reconcile(items=None, receipts=None, **head_changes):
    return reconcile_document("purchase", header(**head_changes), items if items is not None else [item()],
                              receipts if receipts is not None else [receipt()], AS_OF, 90, RULES, COMPANY_ID)


class SupplyTests(unittest.TestCase):
    def test_partial_receipts_are_net_once_and_dates_have_distinct_meanings(self):
        rows, movements, checks = reconcile([item(delivery_date="2026-10-09", min_plan_arrive_date="2026-10-08")],
                                            [receipt(qty=9), receipt(qty=11, ioi_id=22)])
        row = rows[0]
        self.assertEqual((row["received_net_qty"], row["eligible_book_remaining_qty"]), (20, 30))
        self.assertTrue(row["receipt_reconciled"])
        self.assertIn("预约", row["expected_date_source"])
        self.assertIn("old_open_document", row["candidate_flags"])
        self.assertEqual(row["days_since_modified"], 1)
        self.assertEqual(len(movements), 2)
        self.assertEqual(checks["mismatched_skus"], [])

    def test_empty_io_field_uses_verified_difference_but_missing_difference_stops(self):
        rows, _, _ = reconcile([item(ioQty="")])
        self.assertEqual(rows[0]["received_net_qty"], 20)
        for changes in ({"diffQty": None}, {"ioQty": 19}):
            with self.subTest(changes=changes), self.assertRaises(QueryError):
                reconcile([item(**changes)])

    def test_missing_head_quantity_is_unknown_not_zero(self):
        rows, _, check = reconcile(qty_count="")
        self.assertIsNone(check["header_qty_matches"])
        self.assertIn("header_qty_unavailable", rows[0]["quality_issues"])
        self.assertEqual(rows[0]["eligible_book_remaining_qty"], 30)
        rows, _, check = reconcile(qty_count=51)
        self.assertFalse(check["header_qty_matches"])
        self.assertIsNone(rows[0]["eligible_book_remaining_qty"])

    def test_remark_hint_or_pending_confirmation_is_not_eligible_supply(self):
        for changes in ({"remark": "此单作废，待核对"}, {"status": "待审核"}, {"status": "作废"}):
            rows, _, _ = reconcile(**changes)
            self.assertIsNone(rows[0]["eligible_book_remaining_qty"])
            self.assertEqual(rows[0]["remaining_qty"], 30)
        self.assertNotIn("remark", rows[0])

    def test_receipt_mismatch_or_error_is_visible_and_blocks_eligible_amount(self):
        for records in ([receipt(qty=19)], [receipt(error_in=True)], [receipt(sku_id="B")]):
            rows, _, _ = reconcile(receipts=records)
            self.assertIsNone(rows[0]["eligible_book_remaining_qty"])

    def test_duplicate_sku_is_reconciled_in_total_without_invented_line_allocation(self):
        rows, _, check = reconcile([item(qty=25, ioQty=10, diffQty=15), item(poi_id=12, qty=25, ioQty=10, diffQty=15)])
        self.assertEqual(check["mismatched_skus"], [])
        self.assertTrue(all(r["receipt_reconciled"] for r in rows))
        self.assertTrue(all(r["eligible_book_remaining_qty"] is None for r in rows))

    def test_unknown_return_types_wrong_company_and_duplicate_receipts_stop(self):
        for records in ([receipt(type="未核实退货")], [receipt(co_id="other")], [receipt(), receipt()],
                        [receipt(qty=-20)], [receipt(error_in="false")]):
            with self.subTest(records=records), self.assertRaises(QueryError):
                reconcile(receipts=records)

    def test_over_receipt_and_closed_balance_do_not_create_future_quantity(self):
        rows, _, _ = reconcile([item(diffQty=-2, ioQty=52)], [receipt(qty=52)])
        self.assertEqual(rows[0]["eligible_book_remaining_qty"], 0)
        self.assertIn("over_received", rows[0]["quality_issues"])
        rows, _, _ = reconcile([item(diffQty=0, ioQty=50)], [receipt(qty=50)])
        self.assertIn("zero_balance_open_document", rows[0]["candidate_flags"])

    def test_signed_reversal_is_not_gross_receipt_or_confirmed_future_supply(self):
        rows, movements, check = reconcile([item(qty=400, ioQty=0, diffQty=400)],
                                           [receipt(qty=400), receipt(qty=-400, ioi_id=22)], qty_count=400)
        self.assertEqual(rows[0]["received_net_qty"], 0)
        self.assertEqual(rows[0]["remaining_qty"], 400)
        self.assertIsNone(rows[0]["eligible_book_remaining_qty"])
        self.assertIn("negative_receipt_needs_check", rows[0]["candidate_flags"])
        self.assertEqual(check["mismatched_skus"], [])
        self.assertTrue(movements[1]["negative_quantity"])

    def test_material_stock_is_separate_from_issue_and_output_receipts(self):
        client = Mock()
        client.all_table_rows.return_value = [item(qty=50, out_qty=40, in_qty=999, remark="私人信息")]
        rows = read_materials(client, header(), RULES)
        self.assertEqual((rows[0]["planned_material_qty"], rows[0]["issued_qty"], rows[0]["incoming_warehouse_stock_qty"]), (50, 40, 999))
        self.assertEqual(rows[0]["conversion_link_level"], "document")
        self.assertNotIn("remark", rows[0])

    def test_unknown_status_ignored_filter_and_archive_are_rejected(self):
        for record in (header(status="未知状态"), header(status="作废"), header(is_archive=True)):
            client = Mock()
            client.all_table_rows.return_value = [record]
            with self.subTest(record=record), self.assertRaises(QueryError):
                read_headers(client, "purchase")

    def test_head_changes_during_read_stop_success(self):
        with patch("jst_supply.read_headers", side_effect=[[header()], [header(qty_count=51)]]), \
             patch("jst_supply.read_document", return_value=([item()], [receipt()])), \
             self.assertRaises(QueryError):
            run_supply(Mock(company_id=COMPANY_ID, user_id=USER_ID), options())

    def test_invalid_supply_options_fail_without_network(self):
        for changes in ({"age_days": 0}, {"month": "2026-10"}, {"days": 30}, {"kind": "purchase"},
                        {"document_id": -1}, {"command": "supply-review", "document_id": 1}):
            with self.subTest(changes=changes), self.assertRaises(QueryError):
                validate_supply_options(options(**changes))

    def test_document_form_cache_is_separate_and_write_parameters_rejected(self):
        session = Mock()
        session.cookies = test_session().cookies
        session.get.return_value.status_code = 200
        session.get.return_value.text = '<input type="hidden" name="__VIEWSTATE" value="memory-only" />'
        session.post.return_value.status_code = 200
        session.post.return_value.text = "unused"
        client = Client(session=session)
        with patch("jushuitan.parse_callback", return_value={}):
            for po_id in (1, 2, 1):
                client._table_page(PURCHASE_ITEM_URL, [], "poi_id", query_params={"po_id": po_id})
        self.assertEqual(session.get.call_count, 2)
        for params in ({"po_id": 1, "archive": "true"}, {"po_id": 1, "p_co_id": "other"},
                       {"po_id": 1, "type": "rm"}, {"po_id": 1, "all_data": "false"}):
            with self.subTest(params=params), self.assertRaises(QueryError):
                client._table_page(PURCHASE_ITEM_URL, [], "poi_id", query_params=params)
        with self.assertRaises(QueryError):
            client._table_page(MANUFACTURE_MATERIAL_URL, [], "poi_id", query_params={"po_id": 1, "type": "sku"})

    def test_full_report_retains_receipts_materials_and_checks_while_display_is_limited(self):
        with TemporaryDirectory() as folder:
            result = {"rows": [1, 2], "receipt_rows": [3, 4], "material_rows": [5, 6], "document_checks": [7, 8]}
            args = options(output=str(Path(folder) / "report.json"))
            output = io.StringIO()
            with redirect_stdout(output): present_result(result, args)
            self.assertEqual(json.loads(Path(args.output).read_text()), result)
            self.assertEqual(json.loads(output.getvalue())["receipt_rows"], [3])


if __name__ == "__main__":
    unittest.main()
