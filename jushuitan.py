#!/usr/bin/env python3
"""聚水潭只读查询：每次运行从本机 Chrome 会话识别公司和用户。"""
import argparse
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
from html.parser import HTMLParser
import json
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

CHROME_COOKIES = Path.home() / "Library/Application Support/Google/Chrome/Default/Cookies"
ITEM_URL = "https://apiweb.erp321.com/webapi/ItemApi/ItemSku/GetPageListV2"
ORDER_URL = "https://www.erp321.com/app/order/order/list.aspx"
COMBINE_URL = "https://www.erp321.com/app/item/CombineSku/combinesku.aspx"
PURCHASE_URL = "https://www.erp321.com/app/scm/purchase/purchasemode.aspx"
MANUFACTURE_URL = "https://www.erp321.com/app/scm/manufacture/manufacture.aspx"
PURCHASE_ITEM_URL = "https://www.erp321.com/app/scm/purchase/purchaseitem.aspx"
MANUFACTURE_ITEM_URL = "https://www.erp321.com/app/scm/manufacture/manufactureItem.aspx"
MANUFACTURE_MATERIAL_URL = "https://www.erp321.com/app/scm/manufacture/manufactureItemRM.aspx"
RECEIPT_URL = "https://www.erp321.com/app/scm/purchase/PurchaseInItemV2.aspx"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
INVALID_ORDER_STATUSES = {"WaitPay", "Cancelled", "Split", "Merged", "Delete", "Disabled", "SentCancelled"}
VALID_ORDER_STATUSES = {"WaitConfirm", "WaitFConfirm", "Question", "Delivering", "Sent", "OuterSent",
                        "WaitOuterSent", "WaitDeliver", "Lock", "Finished"}
INVALID_ITEM_STATUSES = INVALID_ORDER_STATUSES | {"Replaced"}


class QueryError(Exception):
    pass


@dataclass(frozen=True)
class SessionIdentity:
    company_id: str
    user_id: str


def session_cookie(session, name):
    # 多个 ERP 域若留下不同身份，不猜测哪个是当前公司，也不读取其他站点。
    values = {cookie.value for cookie in session.cookies
              if cookie.name == name and not cookie.is_expired() and
              (cookie.domain.lstrip(".") == "erp321.com" or
               cookie.domain.lstrip(".").endswith(".erp321.com"))}
    if len(values) != 1:
        raise QueryError("聚水潭会话身份缺失或冲突，请在所选 Chrome Profile 重新登录目标公司。")
    return values.pop()


def session_identity(session):
    company_id = session_cookie(session, "u_co_id")
    user_id = session_cookie(session, "u_id")
    if any(not re.fullmatch(r"[1-9][0-9]*", value) for value in (company_id, user_id)):
        raise QueryError("聚水潭会话中的公司或用户编号无效，请重新登录。")
    return SessionIdentity(company_id, user_id)


def number(value, field):
    try:
        result = Decimal(str(value))
        if not result.is_finite():
            raise InvalidOperation
        return result
    except InvalidOperation as exc:
        raise QueryError("返回数据缺少有效的 %s，不能完成统计。" % field) from exc


def now():
    return datetime.now(LOCAL_TZ)


def timestamp(value):
    return value.isoformat(timespec="seconds")


class OrderForm(HTMLParser):
    def __init__(self):
        super().__init__()
        self.fields = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("name") in {
                "__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"}:
            self.fields[attrs["name"]] = attrs.get("value", "")


def parse_callback(text):
    try:
        prefix, content = text.split("|", 1)
        if not prefix.isdigit() or not 0 <= int(prefix) <= len(content):
            raise ValueError
        envelope = json.loads(content[int(prefix):])
        if envelope.get("IsSuccess") is not True or envelope.get("GotoLogin"):
            raise QueryError("查询接口拒绝请求，请检查 Chrome 登录状态和查询权限。")
        result = envelope["ReturnValue"]
        if isinstance(result, str):
            result = json.loads(result)
        if not isinstance(result, dict) or "dp" not in result or "datas" not in result:
            raise ValueError
        return result
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise QueryError("查询返回结构发生变化，未生成统计结果。") from exc


def month_window(month=None, clock=None):
    clock = clock or now()
    if month is not None and not re.fullmatch(r"\d{4}-\d{2}", month):
        raise QueryError("月份格式应为 YYYY-MM。")
    try:
        start = datetime.strptime(month or clock.strftime("%Y-%m"), "%Y-%m").replace(tzinfo=LOCAL_TZ)
    except ValueError as exc:
        raise QueryError("月份格式应为 YYYY-MM。") from exc
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    if start > clock:
        raise QueryError("不能查询尚未开始的月份。")
    return start, min(end, clock.replace(microsecond=0))


def valid_sales_lines(orders, excluded):
    """共用已确认的销售过滤规则，返回订单、明细及数量。"""
    seen_items = set()
    for order in orders:
        order_type = order.get("type") or ""
        if any(t in order_type for t in ("初始欠款", "补发订单", "换货订单")):
            excluded["non_sales_orders"] += 1
            continue
        if not {"普通订单", "天猫分销订单"} & set(order_type.split(",")):
            raise QueryError("出现尚未确认的订单类型，未生成销量排名。")
        status = order.get("src_status")
        if status in INVALID_ORDER_STATUSES or order.get("is_disabled") is True:
            excluded["invalid_or_unpaid_orders"] += 1
            continue
        if status not in VALID_ORDER_STATUSES:
            raise QueryError("订单出现尚未确认的状态 %s，未生成销量排名。" % status)
        items = order.get("items")
        if not isinstance(items, list) or not items:
            raise QueryError("有效订单缺少商品明细，未生成销量排名。")
        for item in items:
            status = item.get("item_status")
            if status in INVALID_ITEM_STATUSES or item.get("is_disabled") is True:
                excluded["invalid_items"] += 1
                continue
            if status not in VALID_ORDER_STATUSES | {None, "", "None"}:
                raise QueryError("商品明细出现尚未确认的状态，未生成销量排名。")
            gift = item.get("is_gift")
            if gift is True or gift == 1:
                excluded["gift_items"] += 1
                continue
            if gift is not False and gift != 0:
                raise QueryError("商品明细缺少明确的赠品标识，未生成销量排名。")
            if not item.get("oi_id") or not item.get("sku_id") or not item.get("i_id"):
                raise QueryError("订单商品明细缺少唯一标识或商品编码。")
            identity = (order["co_id"], item["oi_id"])
            if identity in seen_items:
                raise QueryError("订单明细出现重复，请重新查询以免重复统计。")
            seen_items.add(identity)
            qty = number(item.get("qty"), "qty")
            if qty <= 0:
                excluded["nonpositive_qty_items"] += 1
                continue
            yield order, item, qty


def sales_summary(orders, limit=10):
    """统计有效普通销售订单；按当前 ERP 下单时间归月，保留有效拆合单。"""
    totals, metadata, order_ids = {}, {}, {}
    valid_orders, excluded = set(), Counter()
    for order, item, qty in valid_sales_lines(orders, excluded):
        sku = item["sku_id"]
        totals[sku] = totals.get(sku, Decimal(0)) + qty
        metadata[sku] = {"sku_id": sku, "i_id": item["i_id"],
                         "name": item.get("name") or sku, "sku_type": item.get("sku_type")}
        order_ids.setdefault(sku, set()).add(order["o_id"])
        valid_orders.add(order["o_id"])
    ranking, rank, previous = [], 0, None
    sorted_skus = sorted(totals, key=lambda sku: (-totals[sku], sku))
    for sku in sorted_skus[:limit]:
        qty = totals[sku]
        if qty != previous:
            rank += 1
            previous = qty
        ranking.append({"rank": rank, **metadata[sku], "qty": int(qty) if qty == qty.to_integral() else float(qty),
                        "order_count": len(order_ids[sku]), "order_ids": sorted(order_ids[sku])})
    grand_total = sum(totals.values(), Decimal(0))
    return {"valid_order_count": len(valid_orders), "sold_sku_count": len(totals),
            "total_qty": int(grand_total) if grand_total == grand_total.to_integral() else float(grand_total),
            "excluded": dict(excluded), "ranking": ranking,
            "ranking_scope": "按 SKU 排序；同件数并列；套装按套装 SKU 件数，不展开组件"}


def chrome_session(cookie_path=CHROME_COOKIES):
    """只读取 erp321.com 域的 Cookie，不导出数据库或持久化明文。"""
    if sys.platform != "darwin":
        raise QueryError("当前登录接入仅支持 macOS Chrome；Windows/Linux 浏览器认证尚未实现。")
    try:
        result = subprocess.run(
            ["/usr/bin/security", "-q", "find-generic-password", "-w",
             "-a", "Chrome", "-s", "Chrome Safe Storage"],
            capture_output=True, timeout=20, check=True,
        )
        key = hashlib.pbkdf2_hmac("sha1", result.stdout.strip(), b"saltysalt", 1003, 16)
        with closing(sqlite3.connect(cookie_path.as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
            version = int(connection.execute("SELECT value FROM meta WHERE key='version'").fetchone()[0])
            rows = connection.execute(
                "SELECT host_key,name,value,encrypted_value,path,is_secure,expires_utc,"
                "top_frame_site_key FROM cookies "
                "WHERE host_key='erp321.com' OR host_key LIKE '%.erp321.com'"
            ).fetchall()
        session = requests.Session()
        for host, name, value, encrypted, path, secure, expires, partition in rows:
            # 分区 Cookie 不能直接转成普通 Cookie。
            if partition:
                continue
            expiry = int(expires / 1000000 - 11644473600) if expires else None
            if expiry is not None and expiry <= time.time():
                continue
            if not value and encrypted:
                if encrypted[:3] != b"v10":
                    raise QueryError("Chrome Cookie 加密格式变化，需要更新会话读取方式。")
                plain = unpad(AES.new(key, AES.MODE_CBC, iv=b" " * 16).decrypt(encrypted[3:]), 16)
                if version >= 24:
                    if plain[:32] != hashlib.sha256(host.encode()).digest():
                        raise QueryError("Chrome Cookie 域名校验失败。")
                    plain = plain[32:]
                value = plain.decode("utf-8")
            session.cookies.set(name, value, domain=host, path=path,
                                secure=bool(secure), expires=expiry)
        session_identity(session)
        session.headers.update({"Origin": "https://src.erp321.com",
                                "Referer": "https://src.erp321.com/",
                                "User-Agent": "Mozilla/5.0"})
        return session
    except QueryError:
        raise
    except (subprocess.SubprocessError, sqlite3.Error, OSError, ValueError, UnicodeError) as exc:
        # 不输出钥匙串、Cookie 或响应原文。
        raise QueryError("无法读取本机 Chrome 的聚水潭会话，请检查登录状态和钥匙串访问。") from exc


class Client:
    def __init__(self, session=None):
        self.session = session if session is not None else chrome_session()
        self.identity = session_identity(self.session)
        self.table_forms = {}

    @property
    def company_id(self):
        return self.identity.company_id

    @property
    def user_id(self):
        return self.identity.user_id

    def check_identity(self):
        if session_identity(self.session) != self.identity:
            raise QueryError("查询期间聚水潭公司或用户发生变化，未生成统计结果，请重新查询。")

    def item_page(self, data, page=1, size=50, action=1):
        self.check_identity()
        payload = {"ip": "", "uid": self.user_id, "coid": self.company_id,
                   "page": {"currentPage": page, "pageSize": size, "pageAction": action},
                   "data": data}
        try:
            response = self.session.post(
                ITEM_URL, params={"__from": "web_component", "owner_co_id": self.company_id,
                                  "authorize_co_id": self.company_id},
                json=payload, timeout=(10, 40), allow_redirects=False,
            )
            if response.status_code != 200:
                raise QueryError("聚水潭查询失败，HTTP 状态 %s。" % response.status_code)
            result = response.json()
            self.check_identity()
        except (requests.RequestException, ValueError) as exc:
            raise QueryError("聚水潭请求失败或返回非 JSON，请检查网络和 Chrome 登录状态。") from exc
        if not isinstance(result, dict) or result.get("code") != 0:
            raise QueryError("聚水潭拒绝查询，请检查登录状态和查询权限。")
        if not isinstance(result.get("page"), dict) or not isinstance(result.get("data"), list):
            raise QueryError("商品返回结构发生变化，未生成统计结果。")
        if any(not isinstance(row, dict) or str(row.get("co_id")) != self.company_id
               for row in result["data"]):
            raise QueryError("商品返回公司与本次登录身份不匹配，未生成统计结果。")
        return result

    def all_items(self, data, size=200):
        total = self.item_page(data, size=size, action=2)["page"].get("count")
        if type(total) is not int or total < 0:
            raise QueryError("商品总数接口没有返回有效总数。")
        if total > 200000:
            raise QueryError("商品数量超出本地查询上限，请先缩小范围。")
        rows, seen = [], set()
        for page in range(1, (total + size - 1) // size + 1):
            result = self.item_page(data, page=page, size=size)
            batch = result.get("data")
            if (result.get("page", {}).get("currentPage") != page or
                    not isinstance(batch, list) or len(batch) != min(size, total - len(rows))):
                raise QueryError("商品分页不完整，未生成统计结果，请重新查询。")
            for item in batch:
                if not isinstance(item, dict):
                    raise QueryError("商品数据结构异常。")
                sku = item.get("sku_id")
                if not sku or sku in seen or str(item.get("co_id")) != self.company_id:
                    raise QueryError("商品分页出现重复、缺少编码或公司不匹配。")
                seen.add(sku)
                rows.append(item)
        final_total = self.item_page(data, size=size, action=2)["page"].get("count")
        if final_total != total:
            raise QueryError("查询期间商品数量发生变化，请重新查询。")
        return rows

    def onsale_count(self):
        started = now()
        rows = self.all_items({"sku_type": 1, "enabled": "1", "c_id": "", "orderBy": "",
                               "queryFlds": ["sku_id", "i_id", "name", "enabled", "qty",
                                             "order_lock", "orderable", "virtual_qty", "sku_type"]})
        onsale = []
        for item in rows:
            if item.get("enabled") != 1 or not item.get("i_id") or item.get("sku_type") != "normal":
                raise QueryError("商品启用状态或款式字段与查询条件不一致。")
            if number(item.get("orderable"), "orderable") > 0:
                onsale.append(item)
        combine = self.all_table_rows(COMBINE_URL, [{"k": "enabled", "v": "1", "c": "@="}], "sku_id")
        combine_onsale = []
        for item in combine:
            if item.get("enabled") != "启用" or not item.get("i_id") or item.get("sku_type") != "combine":
                raise QueryError("组合装启用状态或商品类型与查询条件不一致。")
            if number(item.get("v_stock"), "v_stock") > 0:
                combine_onsale.append(item)
        if {r["sku_id"] for r in rows} & {r["sku_id"] for r in combine}:
            raise QueryError("普通商品与组合装出现重复编码，未生成在售总数。")
        groups = {}
        for name, all_rows, available, field in [
                ("normal", rows, onsale, "orderable"),
                ("combine", combine, combine_onsale, "v_stock")]:
            groups[name] = {"enabled_sku_count": len(all_rows), "onsale_sku_count": len(available),
                           "onsale_style_count": len({r["i_id"] for r in available}),
                           "enabled_without_available_stock": len(all_rows) - len(available),
                           "available_stock_field": field}
        return {"ok": True, "complete": True, "company_id": self.company_id, "user_id": self.user_id,
                "metric": "已启用且聚水潭可用库存大于0的 SKU，包含普通商品和组合装",
                "stock_scope": "普通商品采用主仓公有可用数 orderable（实际+虚拟-订单占有）；"
                               "组合装采用 v_stock（各子商品可用数÷用量，取最小值）",
                "started_at": timestamp(started), "finished_at": timestamp(now()),
                "enabled_sku_count": len(rows) + len(combine),
                "onsale_sku_count": len(onsale) + len(combine_onsale),
                "groups": groups}

    def order_page(self, filters, page=1, size=200):
        return self._table_page(ORDER_URL, filters, "o_id", page, size)

    def _table_page(self, url, filters, key, page=1, size=200, query_params=None):
        self.check_identity()
        # 固定页面及 LoadDataToJSON；单据明细表单按单号隔离，禁止任意方法。
        if url not in {ORDER_URL, COMBINE_URL, PURCHASE_URL, MANUFACTURE_URL,
                       PURCHASE_ITEM_URL, MANUFACTURE_ITEM_URL, MANUFACTURE_MATERIAL_URL, RECEIPT_URL}:
            raise QueryError("不支持的查询页面。")
        params = {"_c": "jst-epaas", "epaas": "true"}
        if url in {PURCHASE_URL, MANUFACTURE_URL, PURCHASE_ITEM_URL, MANUFACTURE_ITEM_URL, MANUFACTURE_MATERIAL_URL, RECEIPT_URL}:
            params.update(owner_co_id=self.company_id, authorize_co_id=self.company_id)
        if query_params:
            if url not in {PURCHASE_ITEM_URL, MANUFACTURE_ITEM_URL, MANUFACTURE_MATERIAL_URL, RECEIPT_URL}:
                raise QueryError("该查询页面不接受单据参数。")
            allowed = {"po_id", "p_co_id", "p_owner_co_id", "all_data", "archive", "type"}
            if set(query_params) - allowed or type(query_params.get("po_id")) is not int or query_params["po_id"] <= 0:
                raise QueryError("单据查询参数无效。")
            for field in ("p_co_id", "p_owner_co_id"):
                if field in query_params and str(query_params[field]) != self.company_id:
                    raise QueryError("单据查询公司不匹配。")
            expected_type = "rm" if url == MANUFACTURE_MATERIAL_URL else "sku"
            if (query_params.get("archive", "false") != "false" or query_params.get("type", expected_type) != expected_type
                    or query_params.get("all_data", "true") != "true"):
                raise QueryError("不支持的归档或加工明细类型。")
            params.update(query_params)
        elif url in {PURCHASE_ITEM_URL, MANUFACTURE_ITEM_URL, MANUFACTURE_MATERIAL_URL, RECEIPT_URL}:
            raise QueryError("单据明细必须指定po_id。")
        form_key = (url, tuple(sorted(params.items())))
        try:
            if form_key not in self.table_forms:
                response = self.session.get(
                    url, params=params,
                    timeout=(10, 40), allow_redirects=False,
                )
                self.check_identity()
                if response.status_code != 200:
                    raise QueryError("查询页面不可访问，请检查 Chrome 登录状态。")
                form = OrderForm()
                form.feed(response.text)
                if "__VIEWSTATE" not in form.fields:
                    raise QueryError("查询页面登录失效或结构变化。")
                self.table_forms[form_key] = form.fields
            fields = dict(self.table_forms[form_key])
            token = session_cookie(self.session, "u_cid")
            if not token:
                raise QueryError("会话校验信息缺失，请重新登录 Chrome。")
            fields.update({"__ajax_token": "1." + token + "-", "_jt_page_size": str(size),
                           "_jt_page_count_enabled": "true", "_jt_page_increament_enabled": "false",
                           "__CALLBACKID": "JTable1",
                           "__CALLBACKPARAM": json.dumps({"Method": "LoadDataToJSON",
                               "Args": [str(page), json.dumps(filters), json.dumps({key: False})]})})
            response = self.session.post(
                url, params={**params, "ts___": int(time.time() * 1000), "am___": "LoadDataToJSON"},
                data=fields, headers={"Origin": "https://www.erp321.com",
                    "Referer": url, "X-Requested-With": "XMLHttpRequest"},
                timeout=(10, 40), allow_redirects=False,
            )
            self.check_identity()
            if response.status_code != 200:
                raise QueryError("查询失败，HTTP 状态 %s。" % response.status_code)
            return parse_callback(response.text)
        except requests.RequestException as exc:
            raise QueryError("请求失败，未生成统计结果，请检查网络。") from exc

    def all_table_rows(self, url, filters, key, size=200, query_params=None):
        rows, seen, page, expected = [], set(), 1, None
        def fetch(page=1):
            if query_params is None:
                return self._table_page(url, filters, key, page=page, size=size)
            return self._table_page(url, filters, key, page=page, size=size, query_params=query_params)
        while True:
            result = fetch(page)
            dp, batch = result["dp"], result["datas"]
            if not isinstance(dp, dict) or not isinstance(batch, list):
                raise QueryError("分页结构异常。")
            total = dp.get("DataCount")
            if type(total) is not int or not 0 <= total <= 100000:
                raise QueryError("查询总数无效或超出本地查询上限。")
            expected = total if expected is None else expected
            if (total != expected or dp.get("PageIndex") != page or dp.get("PageSize") != size or
                    len(batch) != min(size, expected - len(rows))):
                raise QueryError("分页不完整或查询期间总数发生变化，未生成统计结果。")
            for row in batch:
                if not isinstance(row, dict):
                    raise QueryError("分页数据结构异常。")
                identity = row.get(key)
                if not identity or identity in seen or str(row.get("co_id")) != self.company_id:
                    raise QueryError("分页出现重复、缺少编号或公司不匹配。")
                seen.add(identity)
                rows.append(row)
            if len(rows) == expected:
                break
            page += 1
        if fetch()["dp"].get("DataCount") != expected:
            raise QueryError("查询期间数据数量发生变化，请重新查询。")
        return rows

    def orders_in_period(self, start, end):
        filters = [{"k": "order_date", "v": start.strftime("%Y-%m-%d %H:%M:%S"), "c": ">="},
                   {"k": "order_date", "v": end.strftime("%Y-%m-%d %H:%M:%S"), "c": "<"}]
        rows = self.all_table_rows(ORDER_URL, filters, "o_id")
        for order in rows:
            try:
                date = datetime.fromisoformat(order["order_date"])
                date = date.replace(tzinfo=LOCAL_TZ) if date.tzinfo is None else date.astimezone(LOCAL_TZ)
            except (ValueError, KeyError, TypeError) as exc:
                raise QueryError("订单缺少有效的下单时间。") from exc
            if not start <= date < end:
                raise QueryError("订单接口没有正确应用月份筛选，未生成销量排名。")
        return rows

    def month_sales(self, month=None, limit=10):
        started = now()
        start, end = month_window(month, started)
        rows = self.orders_in_period(start, end)
        summary = sales_summary(rows, limit)
        return {"ok": True, "complete": True, "company_id": self.company_id, "user_id": self.user_id,
                "metric": "该月下单的有效销售订单商品件数，不计待付款和赠品",
                "month": start.strftime("%Y-%m"), "period_start": timestamp(start),
                "period_end_exclusive": timestamp(end), "started_at": timestamp(started),
                "finished_at": timestamp(now()), "queried_order_count": len(rows),
                "date_basis": "order_date：后台下单时间；拆合单日期沿用后台当前值",
                "refund_basis": "订单件数口径；不另减售后退货数量",
                **summary}


def main():
    parser = argparse.ArgumentParser(description="聚水潭只读查询：自动识别当前登录公司及用户")
    parser.add_argument("command", choices=["session-check", "onsale-count", "sales-top",
                                            "catalog", "sales-lines", "sales-daily", "stock-sales",
                                            "purchase-lines", "manufacture-lines", "supply-review",
                                            "replenishment-plan"])
    parser.add_argument("--month", help="YYYY-MM，默认本月（上海时间）")
    parser.add_argument("--limit", type=int, default=10, help="最多展示1到100行，默认10；不截断全量计算")
    parser.add_argument("--days", type=int, help="分析销售天数；一般含今天默认30，补货情景用完整日默认56")
    parser.add_argument("--sku", help="新分析只展示指定SKU，计算前仍完整读取请求范围")
    parser.add_argument("--sku-rules", help="商品类型、货权、单位的已确认本地映射 JSON")
    parser.add_argument("--output", help="新分析完整结果保存为本地 JSON，终端只显示 --limit 行")
    parser.add_argument("--kind", choices=["purchase", "manufacture", "all"], help="未结单业务类型，默认两类")
    parser.add_argument("--document-id", type=int, help="核对指定采购或加工单号")
    parser.add_argument("--age-days", type=int, help="旧单核对阈值，默认90日；不是工艺交期")
    parser.add_argument("--horizon", type=int, help="补货情景未来天数，默认30，范围1–365")
    parser.add_argument("--scenario-file", help="补货/补产参数与到货批次 JSON，必须声明来源及假设/确认")
    parser.add_argument("--chrome-profile", default="Default", help="Chrome Profile 目录名，如 Default 或 Profile 1")
    args = parser.parse_args()
    client = None
    try:
        if not 1 <= args.limit <= 100:
            raise QueryError("排行榜展示数量应在1到100之间。")
        if args.month is not None:
            month_window(args.month)
        new_commands = {"catalog", "sales-lines", "sales-daily", "stock-sales"}
        supply_commands = {"purchase-lines", "manufacture-lines", "supply-review"}
        plan_commands = {"replenishment-plan"}
        if args.command not in plan_commands and any((args.horizon is not None, args.scenario_file)):
            raise QueryError("--horizon/--scenario-file 只用于 replenishment-plan。")
        if args.command not in supply_commands and any((args.kind, args.document_id is not None, args.age_days is not None)):
            raise QueryError("采购加工参数只用于 purchase-lines、manufacture-lines、supply-review。")
        if args.command not in new_commands | supply_commands | plan_commands and any((args.days is not None, args.sku,
                                                    args.sku_rules, args.output)):
            raise QueryError("新增分析参数只用于 catalog、sales-lines、sales-daily、stock-sales。")
        if args.command in new_commands:
            from jst_analysis import validate_options
            validate_options(args)
        if args.command in supply_commands:
            from jst_supply import validate_supply_options
            validate_supply_options(args)
        if args.command in plan_commands:
            from jst_replenishment import validate_plan_options
            validate_plan_options(args)
        if args.chrome_profile != "Default" and not re.fullmatch(r"Profile [1-9][0-9]*", args.chrome_profile):
            raise QueryError("--chrome-profile 应为 Default 或 Profile N 的目录名。")
        cookie_path = CHROME_COOKIES.parent.parent / args.chrome_profile / "Cookies"
        client = Client(session=chrome_session(cookie_path))
        if args.command in plan_commands:
            from jst_replenishment import run_plan
            from jst_analysis import present_result
            present_result(run_plan(client, args), args)
            return 0
        elif args.command in supply_commands:
            from jst_supply import run_supply
            from jst_analysis import present_result
            present_result(run_supply(client, args), args)
            return 0
        elif args.command in new_commands:
            from jst_analysis import run_analysis, present_result
            result = run_analysis(client, args)
            present_result(result, args)
            return 0
        elif args.command == "sales-top":
            result = client.month_sales(args.month, args.limit)
        elif args.command == "onsale-count":
            result = client.onsale_count()
        else:
            response = client.item_page({"sku_type": 1, "enabled": "1", "c_id": "", "orderBy": "",
                                         "queryFlds": ["sku_id", "i_id", "name", "enabled"]})
            result = {"ok": True, "company_id": client.company_id, "user_id": client.user_id,
                      "rows": len(response["data"]), "page": response["page"]}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except QueryError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.session.close()
    return 0


if __name__ == "__main__":
    # CLI 加载分析模块时复用同一份 QueryError、过滤规则与运行上下文。
    sys.modules["jushuitan"] = sys.modules[__name__]
    sys.exit(main())
