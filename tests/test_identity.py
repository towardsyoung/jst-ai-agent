"""构造会话验证公司切换、请求身份和企业参数隔离，不访问真实账号。"""
from argparse import Namespace
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import Mock, patch

from jushuitan import (Client, COMBINE_URL, PURCHASE_ITEM_URL,
                      QueryError, session_identity)
from jst_analysis import load_rules, run_analysis
from jst_replenishment import load_scenario, run_plan
from jst_supply import read_document, reconcile_document, run_supply
from test_queries import test_session
from test_supply import header, item, receipt, options, RULES, AS_OF


def http_session(company="910001", user="810001"):
    session = Mock()
    session.cookies = test_session(company, user).cookies
    session.get.return_value.status_code = 200
    session.get.return_value.text = '<input name="__VIEWSTATE" value="fixture" />'
    session.post.return_value.status_code = 200
    session.post.return_value.json.return_value = {
        "code": 0, "page": {"count": 1, "currentPage": 1},
        "data": [{"co_id": company, "sku_id": "SAME-SKU"}]}
    session.post.return_value.text = '0|{"IsSuccess":true,"ReturnValue":{"dp":{"DataCount":0,"PageIndex":1,"PageSize":200},"datas":[]}}'
    return session


class IdentityTests(unittest.TestCase):
    def test_identity_is_derived_for_different_companies_and_users(self):
        for company, user in (("910001", "810001"), ("920002", "820002")):
            client = Client(session=test_session(company, user))
            self.assertEqual((client.company_id, client.user_id), (company, user))

    def test_missing_invalid_or_conflicting_erp_identity_stops(self):
        for name, value in (("u_co_id", ""), ("u_co_id", "0"), ("u_co_id", "../other"),
                            ("u_id", "None"), ("u_id", "１２３")):
            session = test_session()
            session.cookies.set(name, value, domain=".erp321.com", path="/")
            with self.subTest(name=name, value=value), self.assertRaises(QueryError):
                session_identity(session)
        session = test_session()
        session.cookies.clear(domain=".erp321.com", path="/", name="u_id")
        with self.assertRaises(QueryError): session_identity(session)
        for name in ("u_co_id", "u_id"):
            session = test_session()
            session.cookies.set(name, "999999", domain="www.erp321.com", path="/")
            with self.subTest(name=name), self.assertRaises(QueryError): session_identity(session)

    def test_other_domains_and_expired_cookies_cannot_select_company(self):
        session = test_session()
        session.cookies.set("u_co_id", "999999", domain="noterp321.com", path="/")
        session.cookies.set("u_id", "999999", domain="www.erp321.com", path="/", expires=int(time.time())-10)
        session.cookies.set("u_co_id", "910001", domain="www.erp321.com", path="/")
        self.assertEqual(session_identity(session).company_id, "910001")

    def test_item_requests_use_each_clients_identity_with_same_sku(self):
        for company, user in (("910001", "810001"), ("920002", "820002")):
            session = http_session(company, user)
            client = Client(session=session)
            rows = client.item_page({})["data"]
            request = session.post.call_args.kwargs
            self.assertEqual(request["json"]["coid"], company)
            self.assertEqual(request["json"]["uid"], user)
            self.assertEqual(request["params"]["owner_co_id"], company)
            self.assertEqual(request["params"]["authorize_co_id"], company)
            self.assertEqual(rows[0]["co_id"], company)

    def test_identity_change_before_or_during_request_stops(self):
        for name in ("u_co_id", "u_id"):
            session = http_session()
            client = Client(session=session)
            session.cookies.set(name, "999999", domain=".erp321.com", path="/")
            with self.subTest(name=name), self.assertRaises(QueryError): client.item_page({})
            session.post.assert_not_called()
            session = http_session()
            client = Client(session=session)
            def changed_response(*args, **kwargs):
                session.cookies.set(name, "999999", domain=".erp321.com", path="/")
                return Mock(status_code=200, json=lambda: {"code": 0, "page": {}, "data": []})
            session.post.side_effect = changed_response
            with self.subTest(name=name), self.assertRaises(QueryError): client.item_page({})

    def test_wrong_company_response_is_rejected(self):
        session = http_session()
        session.post.return_value.json.return_value["data"][0]["co_id"] = "920002"
        with self.assertRaises(QueryError): Client(session=session).item_page({})
        session.post.return_value.text = '0|{"IsSuccess":true,"ReturnValue":{"dp":{"DataCount":1,"PageIndex":1,"PageSize":200},"datas":[{"co_id":"920002","sku_id":"SAME-SKU"}]}}'
        with self.assertRaises(QueryError):
            Client(session=session).all_table_rows(COMBINE_URL, [], "sku_id")

    def test_table_requests_and_document_parameters_are_company_scoped(self):
        session = http_session("920002", "820002")
        client = Client(session=session)
        client._table_page(PURCHASE_ITEM_URL, [], "poi_id", query_params={"po_id": 1, "p_co_id": "920002"})
        self.assertEqual(session.post.call_args.kwargs["params"]["owner_co_id"], "920002")
        with self.assertRaises(QueryError):
            client._table_page(PURCHASE_ITEM_URL, [], "poi_id", query_params={"po_id": 1, "p_co_id": "910001"})

    def test_table_identity_change_after_get_or_post_stops(self):
        for method in ("get", "post"):
            session = http_session()
            client = Client(session=session)
            response = getattr(session, method).return_value
            def changed_response(*args, **kwargs):
                session.cookies.set("u_co_id", "920002", domain=".erp321.com", path="/")
                return response
            getattr(session, method).side_effect = changed_response
            with self.subTest(method=method), self.assertRaises(QueryError):
                client._table_page(PURCHASE_ITEM_URL, [], "poi_id", query_params={"po_id": 1})

    def test_purchase_parameters_and_receipts_follow_current_company(self):
        client = Mock(company_id="920002", user_id="820002")
        client.all_table_rows.side_effect = [[item(co_id="920002")], [receipt(co_id="920002")]]
        items, receipts = read_document(client, "purchase", header(co_id="920002"))
        self.assertEqual(client.all_table_rows.call_args_list[0].kwargs["query_params"]["p_co_id"], "920002")
        rows, _, _ = reconcile_document("purchase", header(co_id="920002"), items, receipts, AS_OF, 90, RULES, "920002")
        self.assertEqual(rows[0]["received_net_qty"], 20)
        for heads, lines, movements in ((header(), items, receipts),
                                       (header(co_id="920002"), [item()], receipts),
                                       (header(co_id="920002"), items, [receipt()])):
            with self.assertRaises(QueryError):
                reconcile_document("purchase", heads, lines, movements, AS_OF, 90, RULES, "920002")

    def test_default_rules_are_isolated_and_missing_company_uses_unknown(self):
        with TemporaryDirectory() as folder, patch("jst_analysis.RULES_ROOT", Path(folder)):
            p = Path(folder)/"910001"/"sku-rules.json"
            p.parent.mkdir()
            p.write_text(json.dumps({"version": 1, "company_id": "910001", "skus": RULES}))
            self.assertEqual(load_rules("910001"), RULES)
            self.assertEqual(load_rules("920002"), {})
            with self.assertRaises(QueryError): load_rules("920002", p)
            with self.assertRaises(QueryError): load_rules("920002", Path(folder)/"missing.json")
            p.write_text('{"version":1,"company_id":"920002","skus":{}}')
            with self.assertRaises(QueryError): load_rules("910001")

    def test_scenarios_cannot_cross_companies(self):
        with TemporaryDirectory() as folder:
            p = Path(folder)/"scenario.json"
            p.write_text('{"version":1,"company_id":"920002","name":"fixture","skus":{}}')
            self.assertEqual(load_scenario("920002", p)["company_id"], "920002")
            with self.assertRaises(QueryError): load_scenario("910001", p)
            self.assertEqual(load_scenario("910001")["skus"], {})

    def test_empty_analytics_still_report_the_current_identity(self):
        client = Mock(company_id="920002", user_id="820002")
        with patch("jst_analysis.read_catalog", return_value=[]):
            result = run_analysis(client, Namespace(command="catalog", sku_rules=None, sku=None))
        self.assertEqual((result["company_id"], result["user_id"]), ("920002", "820002"))
        with patch("jst_supply.read_headers", return_value=[]):
            supply = run_supply(client, options())
        self.assertEqual((supply["company_id"], supply["user_id"]), ("920002", "820002"))
        args = Namespace(command="replenishment-plan", month=None, days=7, horizon=30,
                         scenario_file=None, sku_rules=None, sku=None)
        with patch("jst_replenishment.read_catalog", return_value=[]), \
             patch("jst_replenishment.read_sales", return_value=([], {})), \
             patch("jst_replenishment.run_supply", return_value=supply):
            result = run_plan(client, args)
        self.assertEqual((result["company_id"], result["user_id"]), ("920002", "820002"))
        self.assertFalse(result["execution_ready"])

    def test_new_client_after_company_switch_uses_new_identity(self):
        session = http_session()
        first = Client(session=session)
        session.cookies.set("u_co_id", "920002", domain=".erp321.com", path="/")
        session.cookies.set("u_id", "820002", domain=".erp321.com", path="/")
        second = Client(session=session)
        self.assertEqual((second.company_id, second.user_id), ("920002", "820002"))
        with self.assertRaises(QueryError): first.check_identity()
        with patch.object(second, "orders_in_period", return_value=[]):
            result = second.month_sales()
        self.assertEqual((result["company_id"], result["user_id"]), ("920002", "820002"))


class LauncherTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX shell/symlink入口；Windows使用Python入口")
    def test_launcher_follows_symlink_or_packaged_runtime_without_developer_path(self):
        source = Path(__file__).resolve().parents[1]/"skills/jst-ai-agent/scripts/query.sh"
        for packaged in (False, True):
            with self.subTest(packaged=packaged), TemporaryDirectory(prefix="jst space ") as folder:
                root = Path(folder)/"project"
                skill = root/"skills/jst-ai-agent"
                (skill/"scripts").mkdir(parents=True)
                launcher = skill/"scripts/query.sh"
                launcher.write_text(source.read_text())
                launcher.chmod(0o755)
                runtime = skill/"runtime" if packaged else root
                runtime.mkdir(exist_ok=True)
                entry = runtime/"query.sh"
                entry.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
                entry.chmod(0o755)
                link = Path(folder)/"installed-skill"
                link.symlink_to(skill, target_is_directory=True)
                env = {k: v for k, v in os.environ.items() if k != "JST_AI_HOME"}
                result = subprocess.run([str(link/"scripts/query.sh"), "session-check"],
                                        cwd=folder, env=env, capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout.strip(), "session-check")


if __name__ == "__main__":
    unittest.main()
