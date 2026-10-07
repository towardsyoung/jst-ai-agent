"""白名单业务读取、销售日计算及库存错配候选；不保存原始订单。"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
import json
from itertools import chain
from pathlib import Path
from uuid import uuid4

from jushuitan import (COMBINE_URL, LOCAL_TZ, QueryError, month_window,
                      now, number, timestamp, valid_sales_lines)

RULES_ROOT = Path(__file__).parent / "config" / "companies"
BUSINESS_TYPES = {"finished", "semi_finished", "raw_material", "packaging", "service", "unknown"}
OWNERSHIPS = {"own", "customer", "unknown"}
ITEM_FIELDS = ["sku_id", "i_id", "name", "enabled", "sku_type", "unit", "c_id",
               "qty", "virtual_qty", "order_lock", "orderable", "lock_qty"]


def numeric(value):
    return int(value) if value == value.to_integral() else float(value)


def optional_number(value, field):
    return None if value is None else number(value, field)


def validate_options(args):
    if args.days is not None and (not 1 <= args.days <= 90 or args.month is not None):
        raise QueryError("--days 应为1到90，并且不能与 --month 同用。")
    if args.command == "catalog" and (args.days is not None or args.month is not None):
        raise QueryError("catalog 是当前目录快照，不接受销售期间参数。")
    if args.command in {"sales-lines", "sales-daily"} and args.sku_rules:
        raise QueryError("商品规则用于 catalog 和 stock-sales。")


def load_rules(company_id, path=None):
    explicit_path = path is not None
    path = Path(path).expanduser() if path else RULES_ROOT / company_id / "sku-rules.json"
    if not explicit_path and not path.exists() and not path.is_symlink():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise QueryError("商品规则文件不可读或不是有效 JSON。") from exc
    if (not isinstance(data, dict) or data.get("company_id") != company_id or
            data.get("version") != 1 or not isinstance(data.get("skus"), dict)):
        raise QueryError("商品规则版本、公司或 skus 结构不正确。")
    for sku, rule in data["skus"].items():
        if (not isinstance(sku, str) or not sku or not isinstance(rule, dict) or
                rule.get("business_type", "unknown") not in BUSINESS_TYPES or
                rule.get("ownership", "unknown") not in OWNERSHIPS or
                not isinstance(rule.get("source"), str) or not rule["source"].strip() or
                ("unit" in rule and (not isinstance(rule["unit"], str) or not rule["unit"].strip()))):
            raise QueryError("商品规则需要有效类型/货权/单位及确认依据 source。")
    return data["skus"]


def type_hint(name):
    if any(x in name for x in ("代发费", "快递费", "运费", "加工费")):
        return "service"
    if "来料" in name:
        return "customer_material"
    if any(x in name for x in ("白胎", "素坯", "胚", "坯", "半成品")):
        return "material_or_semi_finished"
    if any(x in name for x in ("包装", "包材", "证书")):
        return "packaging"
    return "unknown"


def read_catalog(client, rules):
    normal = client.all_items({"sku_type": 1, "enabled": "", "c_id": "", "orderBy": "",
                               "queryFlds": ITEM_FIELDS})
    combine = client.all_table_rows(COMBINE_URL, [{"k": "enabled", "v": "0,1,-1", "c": "@="}], "sku_id")
    result, seen = [], set()
    for kind, row in chain((("normal", r) for r in normal), (("combine", r) for r in combine)):
        sku = row["sku_id"]
        if sku in seen or not row.get("i_id") or row.get("sku_type") != kind:
            raise QueryError("商品目录编码重叠、款式缺失或类型异常。")
        seen.add(sku)
        enabled_values = {1: True, 0: False, -1: False} if kind == "normal" else {
            "启用": True, "禁用": False, "未启用": False, "删除": False}
        if row.get("enabled") not in enabled_values:
            raise QueryError("商品启用字段出现未知状态。")
        rule = rules.get(sku, {})
        item = {"sku_id": sku, "i_id": row["i_id"], "name": row.get("name") or sku,
                "sku_type": kind, "enabled": enabled_values[row["enabled"]],
                "business_type": rule.get("business_type", "unknown"),
                "ownership": rule.get("ownership", "unknown"),
                "unit": rule.get("unit") or row.get("unit") or None,
                "unit_source": "local_confirmed_rule" if rule.get("unit") else
                               "erp" if row.get("unit") else "missing",
                "rule_source": rule.get("source"), "category_id": row.get("c_id"),
                "type_hint": type_hint(row.get("name") or ""), "stock_scope": "主仓公有"}
        if kind == "normal":
            values = {k: optional_number(row.get(k), k) for k in
                      ("qty", "virtual_qty", "order_lock", "orderable")}
            qty, virtual, lock, available = (values[k] for k in
                                            ("qty", "virtual_qty", "order_lock", "orderable"))
            if None not in (qty, virtual, lock, available) and abs(qty + virtual - lock - available) > Decimal("0.0001"):
                raise QueryError("库存字段不符合已核实公式，停止相关分析。")
            net = qty - lock if qty is not None and lock is not None else None
            # 锁定字段目前为空字符串；语义未验收，不把该数量称为全仓可履约量。
            item.update({k: numeric(v) if v is not None else None for k, v in values.items()})
            item["physical_after_order_qty"] = numeric(net) if net is not None else None
            item["stock_basis"] = "主仓实际库存 qty 减订单占有 order_lock；不含虚拟库存"
            item["stock_lock_present"] = row.get("lock_qty") not in (None, "", 0)
        else:
            stock = optional_number(row.get("stock"), "stock")
            virtual_stock = optional_number(row.get("v_stock"), "v_stock")
            item.update(qty=None, virtual_qty=None, order_lock=None,
                        orderable=numeric(virtual_stock) if virtual_stock is not None else None,
                        physical_after_order_qty=numeric(stock) if stock is not None else None,
                        stock_lock_present=False,
                        stock_basis="ERP组合装 stock：子商品实际库存减占有后÷用量的最小值；独立装配能力")
        result.append(item)
    if set(rules) - seen:
        raise QueryError("商品规则含当前目录不存在的 SKU，请核对规则。")
    return result


def read_sales(client, start, end):
    orders = client.orders_in_period(start, end)
    excluded = Counter()
    lines = []
    for order, item, qty in valid_sales_lines(orders, excluded):
        lines.append({"order_id": order["o_id"], "line_id": item["oi_id"],
                      "order_date": order["order_date"], "sku_id": item["sku_id"],
                      "i_id": item["i_id"], "name": item.get("name") or item["sku_id"],
                      "sku_type": item.get("sku_type"), "qty": numeric(qty),
                      "shop_id": order.get("shop_id")})
    return lines, {"queried_order_count": len(orders), "valid_order_count": len({r["order_id"] for r in lines}),
                   "valid_line_count": len(lines), "excluded": dict(excluded)}


def sales_daily(lines, start, end):
    totals, meta = defaultdict(lambda: defaultdict(Decimal)), {}
    for line in lines:
        date = datetime.fromisoformat(line["order_date"])
        date = date.replace(tzinfo=LOCAL_TZ) if date.tzinfo is None else date.astimezone(LOCAL_TZ)
        day = date.strftime("%Y-%m-%d")
        totals[line["sku_id"]][day] += number(line["qty"], "qty")
        meta[line["sku_id"]] = {k: line[k] for k in ("sku_id", "i_id", "name", "sku_type")}
    dates, cursor = [], start.replace(hour=0, minute=0, second=0, microsecond=0)
    while cursor < end:
        dates.append(cursor.strftime("%Y-%m-%d"))
        cursor += timedelta(days=1)
    rows = []
    for sku in sorted(totals):
        daily = [{"date": day, "qty": numeric(totals[sku].get(day, Decimal(0))),
                  "partial_day": day == end.strftime("%Y-%m-%d") and end.time().isoformat() != "00:00:00"}
                 for day in dates]
        rows.append({**meta[sku], "qty": numeric(sum(totals[sku].values(), Decimal(0))), "daily": daily})
    return rows


def stock_sales(catalog, daily, start, end):
    sales = {r["sku_id"]: r for r in daily}
    if len(sales) != len(daily) or len({r["sku_id"] for r in catalog}) != len(catalog):
        raise QueryError("关联输入含重复 SKU。")
    unmatched = sorted(set(sales) - {r["sku_id"] for r in catalog})
    elapsed = Decimal(str((end - start).total_seconds())) / Decimal(86400)
    result = []
    for item in catalog:
        record = sales.get(item["sku_id"])
        if record and record["sku_type"] not in (None, item["sku_type"]):
            raise QueryError("销售与商品目录类型不一致。")
        qty = number(record["qty"], "sales_qty") if record else Decimal(0)
        stock = optional_number(item["physical_after_order_qty"], "physical_after_order_qty")
        physical = optional_number(item["qty"], "qty") if item["sku_type"] == "normal" else stock
        confirmed = item["business_type"] == "finished" and item["ownership"] == "own"
        issues = []
        if item["business_type"] == "unknown": issues.append("business_type_unconfirmed")
        if item["ownership"] == "unknown": issues.append("ownership_unconfirmed")
        if not item["unit"]: issues.append("unit_missing")
        if stock is None: issues.append("stock_missing")
        if item["stock_lock_present"]: issues.append("stock_lock_unverified")
        if not item["enabled"]: issues.append("disabled_sku")
        # 非成品、自有货权未确认或单位不明时，只输出核对候选。
        flags = []
        if (item["business_type"] not in {"service", "packaging", "raw_material", "semi_finished"} and
                item["ownership"] != "customer"):
            if stock is not None and stock < 0: flags.append("order_stock_deficit")
            if qty > 0 and stock is not None and stock <= 0: flags.append("sales_without_positive_physical_stock")
            if qty == 0 and physical is not None and physical > 0: flags.append("stock_without_period_sales")
        rate = qty / elapsed if elapsed > 0 else None
        coverage = (max(stock, Decimal(0)) / rate if confirmed and item["enabled"] and item["unit"] and
                    not item["stock_lock_present"] and stock is not None and rate and rate > 0 else None)
        result.append({**item, "period_sales_qty": numeric(qty),
                       "average_daily_order_qty": round(float(rate), 4) if rate is not None else None,
                       "coverage_days_scenario": round(float(coverage), 2) if coverage is not None else None,
                       "analysis_status": "scenario" if confirmed and not issues else "needs_review",
                       "candidate_flags": flags, "quality_issues": issues,
                       "last_order_date": max((d["date"] for d in record["daily"] if d["qty"] > 0), default=None)
                                          if record else None})
    result.sort(key=lambda r: (-bool(r["candidate_flags"]), -r["period_sales_qty"], r["sku_id"]))
    return result, unmatched


def run_analysis(client, args):
    started = now().replace(microsecond=0)
    result = {"ok": True, "complete": True, "run_id": str(uuid4()), "company_id": client.company_id,
              "user_id": client.user_id,
              "capability": args.command, "contract_version": "1", "started_at": timestamp(started),
              "warnings": [], "coverage": {}, "source_reads": []}
    catalog, daily = None, None
    if args.command in {"catalog", "stock-sales"}:
        read_start = now()
        catalog = read_catalog(client, load_rules(client.company_id, args.sku_rules))
        result["source_reads"].append({"entity": "catalog_and_stock", "started_at": timestamp(read_start),
                                       "finished_at": timestamp(now()), "row_count": len(catalog)})
        result["catalog_quality"] = {"row_count": len(catalog), "by_type": dict(Counter(r["sku_type"] for r in catalog)),
                                     "missing_unit_count": sum(not r["unit"] for r in catalog),
                                     "missing_stock_count": sum(r["physical_after_order_qty"] is None for r in catalog),
                                     "unconfirmed_business_type_count": sum(r["business_type"] == "unknown" for r in catalog),
                                     "unconfirmed_ownership_count": sum(r["ownership"] == "unknown" for r in catalog)}
        result["coverage"]["stock"] = "当前主仓公有；组合装是独立装配能力，不得与组件相加；锁定/多仓未验收"
    if args.command != "catalog":
        if args.month:
            start, end = month_window(args.month, started)
        else:
            end = started
            start = started.replace(hour=0, minute=0, second=0) - timedelta(days=(args.days or 30) - 1)
        result.update(period_start=timestamp(start), period_end_exclusive=timestamp(end),
                      metric_version="valid_order_qty_v1", date_basis="order_date：上海时区后台当前下单时间",
                      metric="有效订单商品件数；排除待付款/赠品等，套装按套数，不扣售后；不是出库消耗")
        read_start = now()
        lines, stats = read_sales(client, start, end)
        result.update(stats)
        result["source_reads"].append({"entity": "sales", "started_at": timestamp(read_start),
                                       "finished_at": timestamp(now()), "row_count": stats["queried_order_count"]})
        result["coverage"]["sales"] = "当前订单页在请求期间内完整分页；归档/全历史覆盖未验收"
        result["warnings"].append("零销量仅表示本次当前订单页范围内无符合口径的明细，不能直接判为滞销。")
        if args.command == "sales-lines": result["rows"] = lines
        else:
            daily = sales_daily(lines, start, end)
            result["rows"] = daily
    if args.command == "catalog": result["rows"] = catalog
    if args.command == "stock-sales":
        rows, unmatched = stock_sales(catalog, daily, start, end)
        result["rows"] = rows
        result["join_checks"] = {"catalog_row_count": len(catalog), "joined_row_count": len(rows),
                                  "sold_sku_count": len(daily), "unmatched_sales_skus": unmatched}
        result["candidate_counts"] = dict(Counter(flag for r in rows for flag in r["candidate_flags"]))
        result["candidate_counts_scope"] = "全目录 SKU 核对候选；各标识可重叠，含未确认类型/货权，不是成品缺货或呆滞结论"
        result["warnings"].extend(["候选需核对商品类型、单位及货权；没有成本/库龄证据，不能认定呆滞资产或资金占用。",
                                   "本命令未纳入采购/加工在途和交期；覆盖天数仅为订单需求恒定、无未来供给的情景，不是承诺缺货日。",
                                   "库存是当前值，销售是历史期间；已扣占用，不再次扣该期间订单。历史月份不对应历史库存。"])
        if unmatched: result["warnings"].append("部分销售SKU不在当前目录，未参与库存关联，见 join_checks。")
    if args.sku:
        result["rows"] = [r for r in result["rows"] if r["sku_id"] == args.sku]
        result["display_filter"] = {"sku_id": args.sku}
    result["row_count"] = len(result["rows"])
    result["finished_at"] = timestamp(now())
    return result


def present_result(result, args):
    if args.output:
        try:
            path = Path(args.output).expanduser().resolve()
            if path.exists():
                raise QueryError("输出文件已存在，请换一个文件名以保留历史报告。")
            with path.open("x") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
                f.write("\n")
        except OSError as exc:
            raise QueryError("结果文件保存失败，请检查目录和权限。") from exc
    preview = {**result, "rows": result["rows"][:args.limit],
               "displayed_row_count": min(args.limit, len(result["rows"])),
               "display_truncated": len(result["rows"]) > args.limit}
    for field in ("receipt_rows", "document_checks", "material_rows", "arrival_checks", "unresolved_supply", "unmatched_demand"):
        if field in result:
            preview[field] = result[field][:args.limit]
            preview["displayed_" + field + "_count"] = len(preview[field])
            preview[field + "_display_truncated"] = len(result[field]) > args.limit
    if args.output: preview["report_path"] = str(path)
    print(json.dumps(preview, ensure_ascii=False, indent=2))
