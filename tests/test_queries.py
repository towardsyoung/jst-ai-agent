import copy
from datetime import datetime
import json
import unittest

import requests
from unittest.mock import patch

from jushuitan import (Client, COMBINE_URL, ORDER_URL, LOCAL_TZ,
                      QueryError, month_window, parse_callback, sales_summary)


# 构造公司与用户；不使用实际企业账号。
COMPANY_ID = "910001"
USER_ID = "810001"


def test_session(company_id=COMPANY_ID, user_id=USER_ID):
    session = requests.Session()
    for name, value in (("u_co_id", company_id), ("u_id", user_id), ("u_cid", "test-token")):
        session.cookies.set(name, value, domain=".erp321.com", path="/")
    return session


def item(sku="A", qty=1, oi=1, **changes):
    row = {"oi_id": oi, "sku_id": sku, "i_id": sku, "name": sku,
           "qty": qty, "is_gift": False, "item_status": None, "sku_type": "normal"}
    return {**row, **changes}


def order(oid=1, items=None, status="Sent", **changes):
    row = {"o_id": oid, "co_id": int(COMPANY_ID), "type": "普通订单",
           "src_status": status, "order_date": "2026-10-03 10:00:00",
           "items": items if items is not None else [item(oi=oid)]}
    return {**row, **changes}


class SalesTests(unittest.TestCase):
    def test_nested_split_only_counts_active_leaves(self):
        # 同一笔购买先拆一次，再拆子单：父单共重复承载 23 件明细。
        parent = [item("A", 5, 1), item("B", 4, 2), item("C", 2, 3), item("D", 1, 4)]
        orders = [order(1, parent, "Split"), order(2, [item("D", 1, 5)], link_oid=1),
                  order(3, parent[:3], "Split", link_oid=1),
                  order(4, [item("B", 4, 6), item("C", 2, 7)], link_oid=3),
                  order(5, [item("A", 5, 8)], "WaitConfirm", link_oid=3)]
        result = sales_summary(orders)
        self.assertEqual(result["total_qty"], 12)
        self.assertEqual(result["valid_order_count"], 3)
        self.assertEqual(result["excluded"]["invalid_or_unpaid_orders"], 2)

    def test_waitpay_and_gifts_excluded_credit_shipped_included(self):
        result = sales_summary([order(1, [item(qty=100)], "WaitPay"),
                                order(2, [item(qty=2, oi=2), item(qty=99, oi=3, is_gift=True)],
                                      is_paid=False),
                                order(3, [item(qty=7, oi=4)], "Cancelled")])
        self.assertEqual(result["total_qty"], 2)
        self.assertEqual(result["excluded"]["gift_items"], 1)
        self.assertEqual(result["ranking"][0]["order_ids"], [2])

    def test_merged_parent_and_replaced_detail_excluded(self):
        result = sales_summary([order(1, [item(qty=50)], "Merged"),
                                order(2, [item(qty=4, oi=2), item(qty=30, oi=3, item_status="Replaced")],
                                      is_merge=True)])
        self.assertEqual(result["total_qty"], 4)
        self.assertEqual(result["excluded"]["invalid_items"], 1)

    def test_nonsales_and_nonpositive_quantities(self):
        result = sales_summary([order(1, [], type="初始欠款"),
                                order(2, [item(qty=30)], type="普通订单,换货订单"),
                                order(3, [item(qty=0, oi=3), item(qty=-1, oi=4), item(qty=2, oi=5)],
                                      type="普通订单,供销+")])
        self.assertEqual(result["total_qty"], 2)
        self.assertEqual(result["excluded"]["non_sales_orders"], 2)
        self.assertEqual(result["excluded"]["nonpositive_qty_items"], 2)

    def test_ties_and_bundle_unit(self):
        result = sales_summary([order(1, [item("A", 2), item("B", 2, 2, sku_type="combine"),
                                          item("C", 1, 3)])])
        self.assertEqual([r["rank"] for r in result["ranking"]], [1, 1, 2])
        self.assertEqual(result["total_qty"], 5)
        self.assertEqual(result["ranking"][1]["sku_type"], "combine")

    def test_uncertain_data_and_duplicate_detail_fail(self):
        cases = [order(status="NewStatus"), order(type="新订单类型"), order(items=[]),
                 order(items=[item(is_gift=None)]), order(items=[item(qty=None)]),
                 order(items=[item(qty="NaN")]), order(items=[item(), item()])]
        for row in cases:
            with self.subTest(row=row):
                with self.assertRaises(QueryError):
                    sales_summary([row])


class CalendarTests(unittest.TestCase):
    def test_current_month_cutoff_and_previous_month(self):
        clock = datetime(2026, 10, 5, 12, 34, 56, 1234, tzinfo=LOCAL_TZ)
        self.assertEqual(month_window(clock=clock),
                         (datetime(2026, 10, 1, tzinfo=LOCAL_TZ), clock.replace(microsecond=0)))
        self.assertEqual(month_window("2026-09", clock),
                         (datetime(2026, 9, 1, tzinfo=LOCAL_TZ), datetime(2026, 10, 1, tzinfo=LOCAL_TZ)))

    def test_year_boundary_and_invalid_month(self):
        clock = datetime(2027, 2, 1, tzinfo=LOCAL_TZ)
        self.assertEqual(month_window("2026-12", clock)[1], datetime(2027, 1, 1, tzinfo=LOCAL_TZ))
        for value in ("2027-03", "2026-13", "2026-1", "anything"):
            with self.subTest(value=value), self.assertRaises(QueryError):
                month_window(value, clock)


class CallbackTests(unittest.TestCase):
    def test_valid_callback_parses_without_running_script(self):
        expected = {"dp": {"DataCount": 0}, "datas": []}
        envelope = {"IsSuccess": True, "ReturnValue": json.dumps(expected),
                    "ClientScript": "throw new Error('must never execute')"}
        self.assertEqual(parse_callback("0|" + json.dumps(envelope)), expected)

    def test_login_failure_and_bad_callback_fail(self):
        for raw in ("<html>login</html>", "99|{}", "0|{}",
                    '0|{"IsSuccess":true,"GotoLogin":true}',
                    '0|{"IsSuccess":true,"ReturnValue":[]}'):
            with self.subTest(raw=raw), self.assertRaises(QueryError):
                parse_callback(raw)


class ItemPager(Client):
    def __init__(self, rows, final_count=None, bad_page=None):
        super().__init__(session=test_session())
        self.rows, self.final_count, self.bad_page = rows, final_count, bad_page
        self.count_calls = 0

    def item_page(self, data, page=1, size=50, action=1):
        if action == 2:
            self.count_calls += 1
            total = len(self.rows) if self.count_calls == 1 or self.final_count is None else self.final_count
            return {"page": {"count": total}, "data": []}
        batch = self.rows[(page - 1) * size:page * size]
        if self.bad_page == page:
            batch = batch[:-1]
        return {"page": {"currentPage": page}, "data": batch}


class TablePager(Client):
    def __init__(self, rows, changed_count=False, truncate=False):
        super().__init__(session=test_session())
        self.rows, self.calls = rows, 0
        self.changed_count, self.truncate = changed_count, truncate

    def _table_page(self, url, filters, key, page=1, size=200):
        self.calls += 1
        batch = self.rows[(page - 1) * size:page * size]
        total = len(self.rows) + int(self.changed_count and self.calls > 1)
        if self.truncate:
            batch = batch[:-1]
        return {"dp": {"DataCount": total, "PageIndex": page, "PageSize": size}, "datas": batch}


class PagingTests(unittest.TestCase):
    def test_full_item_pages_and_empty_result(self):
        rows = [{"sku_id": str(i), "co_id": COMPANY_ID} for i in range(5)]
        self.assertEqual(ItemPager(rows).all_items({}, size=2), rows)
        self.assertEqual(ItemPager([]).all_items({}), [])

    def test_item_truncation_duplicates_wrong_company_and_changing_count_fail(self):
        rows = [{"sku_id": "A", "co_id": COMPANY_ID}, {"sku_id": "B", "co_id": COMPANY_ID}]
        for client in [ItemPager(rows, bad_page=2), ItemPager(rows, final_count=3),
                       ItemPager([rows[0], rows[0]]), ItemPager([{"sku_id": "A", "co_id": 1}])]:
            with self.subTest(client=client), self.assertRaises(QueryError):
                client.all_items({}, size=1)

    def test_table_pages_and_invalid_pages(self):
        rows = [order(i) for i in range(1, 6)]
        self.assertEqual(TablePager(rows).all_table_rows(ORDER_URL, [], "o_id", size=2), rows)
        self.assertEqual(TablePager([]).all_table_rows(ORDER_URL, [], "o_id"), [])
        for client in (TablePager(rows, truncate=True), TablePager(rows, changed_count=True),
                       TablePager([rows[0], rows[0]]), TablePager([order(co_id=1)])):
            with self.subTest(client=client), self.assertRaises(QueryError):
                client.all_table_rows(ORDER_URL, [], "o_id", size=2)

    def test_server_must_apply_month_filter(self):
        clock = datetime(2026, 10, 5, 12, tzinfo=LOCAL_TZ)
        for date in ("2026-09-30 23:59:59", "2026-10-05 12:00:00", "invalid"):
            client = TablePager([order(order_date=date)])
            with self.subTest(date=date), patch("jushuitan.now", return_value=clock), self.assertRaises(QueryError):
                client.month_sales()

    def test_onsale_uses_available_not_actual_and_includes_bundles(self):
        normal = [{"sku_id": "A", "i_id": "STYLE", "co_id": COMPANY_ID, "enabled": 1,
                   "qty": 10, "orderable": -2, "sku_type": "normal"},
                  {"sku_id": "B", "i_id": "STYLE", "co_id": COMPANY_ID, "enabled": 1,
                   "qty": 0, "orderable": 3, "sku_type": "normal"}]
        combine = [{"sku_id": "SET", "i_id": "SET", "co_id": COMPANY_ID, "enabled": "启用",
                    "stock": 0, "v_stock": 1, "sku_type": "combine"}]
        client = ItemPager(normal)
        with patch.object(client, "all_table_rows", return_value=combine):
            result = client.onsale_count()
        self.assertEqual(result["onsale_sku_count"], 2)
        self.assertEqual(result["groups"]["normal"]["onsale_sku_count"], 1)
        self.assertEqual(result["groups"]["combine"]["onsale_sku_count"], 1)
        broken = copy.deepcopy(combine)
        broken[0]["v_stock"] = None
        with patch.object(client, "all_table_rows", return_value=broken), self.assertRaises(QueryError):
            client.onsale_count()

    def test_table_endpoint_allowlist(self):
        client = Client(session=test_session())
        with self.assertRaises(QueryError):
            client._table_page("https://www.erp321.com/write", [], "sku_id")


if __name__ == "__main__":
    unittest.main()
