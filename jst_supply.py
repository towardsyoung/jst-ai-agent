"""采购/加工账面未结与收货核对。仅投影必要字段，不保存原始单据。"""
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal

from jushuitan import (LOCAL_TZ, PURCHASE_URL, MANUFACTURE_URL,
                      PURCHASE_ITEM_URL, MANUFACTURE_ITEM_URL, MANUFACTURE_MATERIAL_URL, RECEIPT_URL,
                      QueryError, now, number, timestamp)
from jst_analysis import numeric, load_rules

OPEN_FILTER = [{"k": "status", "v": "WaitConfirm,Confirmed,OuterConfirming", "c": "@="}]
OPEN_STATUSES = {"待审核", "已确认", "外部确认中"}
OTHER_STATUSES = {"已完成", "完成", "作废", "已作废", "被合并", "待收货", "归档"}
RECEIVE_STATUSES = {"未入库", "部分入库", "全部入库"}


def date_value(value, required=False):
    if value in (None, "", "0001-01-01", "0001-01-01 00:00:00"):
        if required: raise QueryError("单据缺少有效日期。")
        return None
    try:
        value = datetime.fromisoformat(value)
        return value.replace(tzinfo=LOCAL_TZ) if value.tzinfo is None else value.astimezone(LOCAL_TZ)
    except (ValueError, TypeError) as exc:
        raise QueryError("单据返回未知日期格式。") from exc


def optional_qty(value, field):
    return None if value in (None, "") else numeric(number(value, field))


def void_hint(row):
    # 仅保留核对标记，避免把备注里的联系人/地址写入报告。
    return any(word in str(row.get("remark") or "") for word in ("作废", "取消", "红冲"))


def header_signature(rows):
    return sorted((r["po_id"], r.get("status"), r.get("receive_status"), r.get("modified"),
                   str(r.get("qty_count")), void_hint(r), r.get("is_archive")) for r in rows)


def read_headers(client, kind, document_id=None):
    url, size = (PURCHASE_URL, 200) if kind == "purchase" else (MANUFACTURE_URL, 500)
    filters = [{"k": "po_id", "v": str(document_id), "c": "="}] if document_id else OPEN_FILTER
    rows = client.all_table_rows(url, filters, "po_id", size=size)
    if len(rows) > 500:
        raise QueryError("未结单超过500条，请使用 --document-id 分单核对。")
    if document_id and not rows:
        raise QueryError("当前非归档页面未找到指定单据。")
    for row in rows:
        if (document_id and row["po_id"] != document_id) or (not document_id and row.get("status") not in OPEN_STATUSES):
            raise QueryError("采购/加工接口没有应用指定筛选。")
        if row.get("status") not in OPEN_STATUSES | OTHER_STATUSES or row.get("receive_status") not in RECEIVE_STATUSES:
            raise QueryError("采购/加工出现未知单据或入库状态。")
        if row.get("is_archive") is not False:
            raise QueryError("归档标识缺失或命中归档单，当前入口不支持归档。")
        if row.get("type", "po") != "po" and kind == "purchase":
            raise QueryError("出现非采购订单业务，需另核实。")
        date_value(row.get("po_date"), required=True)
    return rows


def read_document(client, kind, header):
    po_id = header["po_id"]
    if kind == "purchase":
        url = PURCHASE_ITEM_URL
        params = {"po_id": po_id, "p_co_id": client.company_id, "p_owner_co_id": client.company_id,
                  "all_data": "true", "archive": "false"}
    else:
        url = MANUFACTURE_ITEM_URL
        params = {"po_id": po_id, "type": "sku", "archive": "false"}
        if header.get("manufacture_type") not in {"组装加工", "拆分加工", "改码加工"} or header.get("new_tc_mo") is True:
            raise QueryError("加工类型使用未验收明细入口，停止核对。")
    items = client.all_table_rows(url, [], "poi_id", size=500, query_params=params)
    if not items:
        raise QueryError("采购/加工单缺少SKU明细。")
    for item in items:
        if item.get("po_id") != po_id or not item.get("sku_id"):
            raise QueryError("采购/加工明细缺少SKU或单号不匹配。")
    receipts = client.all_table_rows(RECEIPT_URL, [], "ioi_id", size=500,
                                    query_params={"po_id": po_id, "p_co_id": client.company_id})
    if any(r.get("po_id", po_id) != po_id for r in receipts):
        raise QueryError("入库记录单号不匹配。")
    return items, receipts


def read_materials(client, header, rules):
    rows = client.all_table_rows(MANUFACTURE_MATERIAL_URL, [], "poi_id", size=500,
                                query_params={"po_id": header["po_id"], "type": "rm", "archive": "false"})
    result = []
    for row in rows:
        if row.get("po_id") != header["po_id"] or not row.get("sku_id"):
            raise QueryError("加工原料明细单号不匹配或缺少SKU。")
        qty = number(row.get("qty"), "material qty")
        if qty < 0: raise QueryError("加工原料数量为负，需另核实业务。")
        rule = rules.get(row["sku_id"], {})
        result.append({"document_id": header["po_id"], "line_id": row["poi_id"], "sku_id": row["sku_id"],
                       "i_id": row.get("i_id"), "name": row.get("name") or row["sku_id"],
                       "planned_material_qty": numeric(qty),
                       "issued_qty": optional_qty(row.get("out_qty"), "out_qty"),
                       "incoming_warehouse_stock_qty": optional_qty(row.get("in_qty"), "in_qty"),
                       "unit": rule.get("unit"), "ownership": rule.get("ownership", "unknown"),
                       "rule_source": rule.get("source"), "conversion_link_level": "document",
                       "source_basis": "qty=加工原料表加工数量；out_qty=已出库；in_qty=进货仓库存，均不是产出入库或工序实耗"})
    return result


def reconcile_document(kind, header, items, receipts, as_of, age_days, rules, company_id):
    if str(header.get("co_id")) != company_id or any(str(r.get("co_id")) != company_id for r in items):
        raise QueryError("采购/加工单头或明细公司与本次登录身份不匹配。")
    totals, net_receipts, movement_rows = defaultdict(Decimal), defaultdict(Decimal), []
    sku_counts = Counter(r["sku_id"] for r in items)
    seen, negative_receipt_skus = set(), set()
    for receipt in receipts:
        identity = (receipt.get("io_id"), receipt.get("ioi_id"))
        if not all(identity) or identity in seen or str(receipt.get("co_id")) != company_id:
            raise QueryError("入库记录缺少唯一键、重复或公司不匹配。")
        seen.add(identity)
        sku = receipt.get("sku_id")
        if not sku: raise QueryError("入库记录缺少SKU。")
        movement_type = receipt.get("type")
        if movement_type not in {"采购进仓", "加工进仓"}:
            raise QueryError("尚未核实的收货单据类型：%s。" % movement_type)
        if movement_type != ("采购进仓" if kind == "purchase" else "加工进仓"):
            raise QueryError("入库业务类型与采购/加工来源不一致。")
        qty = number(receipt.get("qty"), "receipt qty")
        if qty < 0:
            negative_receipt_skus.add(sku)
        if type(receipt.get("error_in")) is not bool:
            raise QueryError("入库异常标识缺失或出现未知值。")
        net_receipts[sku] += qty
        movement_rows.append({"kind": kind, "document_id": header["po_id"], "io_id": identity[0],
                              "ioi_id": identity[1], "sku_id": sku, "type": movement_type,
                              "qty": numeric(qty), "negative_quantity": qty < 0,
                              "created": timestamp(date_value(receipt.get("created"), required=True)),
                              "wms_co_id": receipt.get("wms_co_id"), "warehouse_id": receipt.get("wh_id"),
                              "warehouse": receipt.get("warehouse"), "error_in": receipt.get("error_in")})
    if any(qty < 0 for qty in net_receipts.values()):
        raise QueryError("关联收货净量为负，尚未验收该业务。")
    parsed = []
    for item in items:
        qty = number(item.get("qty"), "ordered qty")
        # ioQty为空时不推断零：已核实diffQty=qty-净入库，可由两字段核算。
        balance = number(item.get("diffQty"), "diffQty")
        received = qty - balance
        if item.get("ioQty") not in (None, "") and abs(received - number(item["ioQty"], "ioQty")) > Decimal("0.0001"):
            raise QueryError("明细数量、差异数与净入库字段不一致。")
        if qty < 0: raise QueryError("采购/加工明细数量为负，需核实业务。")
        totals[item["sku_id"]] += received
        parsed.append((item, qty, received, balance))
    mismatched = {sku for sku in set(totals) | set(net_receipts)
                  if abs(totals[sku] - net_receipts[sku]) > Decimal("0.0001")}
    ordered_sum = sum((p[1] for p in parsed), Decimal(0))
    header_qty = optional_qty(header.get("qty_count"), "header qty_count")
    header_matches = abs(ordered_sum - Decimal(str(header_qty))) <= Decimal("0.0001") if header_qty is not None else None
    po_date = date_value(header["po_date"], required=True)
    modified = date_value(header.get("modified"))
    age = (as_of.date() - po_date.date()).days
    lines = []
    for item, qty, received, balance in parsed:
        sku = item["sku_id"]
        rule = rules.get(sku, {})
        issues = []
        if header_matches is False: issues.append("header_line_qty_mismatch")
        if header_matches is None: issues.append("header_qty_unavailable")
        if sku in mismatched: issues.append("receipt_net_mismatch")
        if sku_counts[sku] > 1: issues.append("receipt_reconciled_by_sku_not_line")
        if header["status"] != "已确认": issues.append("document_not_confirmed")
        if void_hint(header): issues.append("remark_void_hint")
        if header.get("status") in {"作废", "已作废", "被合并"}: issues.append("invalid_document")
        if any(r.get("error_in") for r in receipts if r.get("sku_id") == sku): issues.append("receipt_error_flag")
        if sku in negative_receipt_skus: issues.append("negative_receipt_present")
        if rule.get("ownership") != "own": issues.append("ownership_unconfirmed_or_customer")
        if not rule.get("unit"): issues.append("unit_unconfirmed")
        if balance < 0: issues.append("over_received")
        if received < 0: issues.append("negative_net_received")
        agreement_date = date_value(item.get("delivery_date"))
        booking_date = date_value(item.get("min_plan_arrive_date")) if kind == "purchase" else None
        expected_date = booking_date or agreement_date
        if not expected_date and balance > 0: issues.append("expected_date_missing")
        expected_source = ("min_plan_arrive_date：预约最早预计到货" if booking_date else
                           "delivery_date：协议到货" if agreement_date and kind == "purchase" else
                           "delivery_date：预计加工完成" if agreement_date else None)
        blocked = {"header_line_qty_mismatch", "receipt_net_mismatch", "receipt_reconciled_by_sku_not_line",
                   "document_not_confirmed", "remark_void_hint", "invalid_document", "receipt_error_flag",
                   "negative_net_received", "negative_receipt_present"}
        eligible = max(balance, Decimal(0)) if not blocked.intersection(issues) else None
        flags = []
        if age >= age_days and balance > 0: flags.append("old_open_document")
        if expected_date and balance > 0 and expected_date.date() < as_of.date(): flags.append("past_planned_date")
        if received > 0 and balance > 0: flags.append("partial_receipt")
        if balance == 0 and header["status"] in OPEN_STATUSES: flags.append("zero_balance_open_document")
        if void_hint(header): flags.append("void_hint_needs_check")
        if sku in negative_receipt_skus: flags.append("negative_receipt_needs_check")
        if sku in mismatched or header_matches is False: flags.append("reconciliation_difference")
        lines.append({"kind": kind, "document_id": header["po_id"], "line_id": item["poi_id"],
                      "sku_id": sku, "i_id": item.get("i_id"), "name": item.get("name") or sku,
                      "document_date": timestamp(po_date), "status": header["status"],
                      "receive_status": header["receive_status"], "manufacture_type": header.get("manufacture_type"),
                      "supplier_id": header.get("seller_id"), "wms_co_id": header.get("wms_co_id"),
                      "receipt_wms_co_id": header.get("receipt_wms_co_id"),
                      "ordered_qty": numeric(qty), "received_net_qty": numeric(received),
                      "signed_remaining_qty": numeric(balance), "remaining_qty": numeric(max(balance, Decimal(0))),
                      "eligible_book_remaining_qty": numeric(eligible) if eligible is not None else None,
                      "receipt_net_for_sku": numeric(net_receipts[sku]), "receipt_reconciled": sku not in mismatched,
                      "unit": rule.get("unit"), "ownership": rule.get("ownership", "unknown"),
                      "business_type": rule.get("business_type", "unknown"), "rule_source": rule.get("source"),
                      "expected_date": timestamp(expected_date) if expected_date else None,
                      "expected_date_source": expected_source,
                      "agreement_or_completion_date": timestamp(agreement_date) if agreement_date else None,
                      "booking_min_expected_date": timestamp(booking_date) if booking_date else None,
                      "agreement_or_completion_qty": optional_qty(item.get("plan_arrive_qty"), "plan_arrive_qty"),
                      "booking_expected_qty": optional_qty(item.get("expect_arrive_qty"), "expect_arrive_qty"),
                      "age_days": age, "modified_at": timestamp(modified) if modified else None,
                      "days_since_modified": (as_of.date() - modified.date()).days if modified else None,
                      "quality_issues": issues, "candidate_flags": flags})
    return lines, movement_rows, {"kind": kind, "document_id": header["po_id"], "header_qty_matches": header_matches,
                                 "header_ordered_qty": header_qty, "line_ordered_qty": numeric(ordered_sum),
                                 "line_count": len(items), "receipt_count": len(receipts),
                                 "mismatched_skus": sorted(mismatched)}


def validate_supply_options(args):
    if args.month or args.days is not None:
        raise QueryError("未结单按当前业务状态查询，不接受销售期间参数。")
    if args.kind and args.command != "supply-review":
        raise QueryError("--kind 只用于 supply-review。")
    kind = args.kind or ("purchase" if args.command == "purchase-lines" else
                         "manufacture" if args.command == "manufacture-lines" else "all")
    if args.document_id is not None and (args.document_id <= 0 or kind == "all"):
        raise QueryError("--document-id 应为正整数，并且必须指定采购或加工类型。")
    if not 1 <= (90 if args.age_days is None else args.age_days) <= 3650:
        raise QueryError("--age-days 应为1到3650，仅作旧单核对阈值。")
    return kind


def run_supply(client, args):
    from uuid import uuid4
    kind = validate_supply_options(args)
    started = now()
    rules = load_rules(client.company_id, args.sku_rules)
    rows, receipts, checks, reads, materials = [], [], [], [], []
    header_counts = {}
    seen_receipts = set()
    for domain in ("purchase", "manufacture") if kind == "all" else (kind,):
        read_start = now()
        headers = read_headers(client, domain, args.document_id)
        header_counts[domain] = len(headers)
        for header in headers:
            items, movements = read_document(client, domain, header)
            lines, movements, check = reconcile_document(domain, header, items, movements, started,
                                                         args.age_days or 90, rules, client.company_id)
            if domain == "manufacture":
                material_rows = read_materials(client, header, rules)
                check["material_line_count"] = len(material_rows)
                materials.extend(material_rows)
            for movement in movements:
                key = movement["ioi_id"]
                if key in seen_receipts: raise QueryError("同一收货明细关联多个单据，停止以免重复计供给。")
                seen_receipts.add(key)
            rows.extend(lines)
            receipts.extend(movements)
            checks.append(check)
        final_headers = read_headers(client, domain, args.document_id)
        if header_signature(headers) != header_signature(final_headers):
            raise QueryError("读取明细期间单据状态或数量变化，请重试。")
        reads.append({"entity": domain, "started_at": timestamp(read_start), "finished_at": timestamp(now()),
                      "document_count": len(headers)})
    if args.sku:
        rows = [r for r in rows if r["sku_id"] == args.sku]
        receipts = [r for r in receipts if r["sku_id"] == args.sku]
        # 输出目标SKU对应加工单的全部原料，保留单据级关联而非假设逐行BOM。
        relevant_documents = {r["document_id"] for r in rows if r["kind"] == "manufacture"}
        materials = [r for r in materials if r["document_id"] in relevant_documents]
    rows.sort(key=lambda r: (-bool(r["candidate_flags"]), -r["age_days"], r["kind"], r["document_id"], r["line_id"]))
    return {"ok": True, "complete": True, "run_id": str(uuid4()), "company_id": client.company_id,
            "user_id": client.user_id,
            "capability": args.command, "contract_version": "supply_review_v1",
            "started_at": timestamp(started), "finished_at": timestamp(now()), "source_reads": reads,
            "coverage": "当前非归档待审核/已确认/外部确认中；指定单号时核对该单状态，不代表全历史",
            "document_id_filter": args.document_id, "sku_filter": args.sku,
            "header_counts": header_counts, "document_checks": checks, "receipt_rows": receipts,
            "material_rows": materials, "material_row_count": len(materials),
            "document_counts_scope": "完整单据筛选范围；SKU筛选只限制输出商品及收货行",
            "receipt_row_count": len(receipts), "rows": rows, "row_count": len(rows),
            "candidate_counts": dict(Counter(flag for r in rows for flag in r["candidate_flags"])),
            "quality_counts": dict(Counter(flag for r in rows for flag in r["quality_issues"])),
            "age_review_threshold_days": args.age_days or 90,
            "warnings": ["有效账面剩余不等于可按期履约的实物供给；缺单位/货权、锁定及工艺信息不能直接作采购或排产承诺。",
                         "收货按来源单号和SKU核对净量，已入库不再作为未来在途；重复SKU明细不做未经验证的逐行分摊。",
                         "协议到货、预约最早到货和预计加工完成分别标来源，不能把单个日期覆盖到全部剩余量。",
                         "单据年龄/修改间隔不是车间停滞时长；备注作废词只提示核对，不替代正式状态。",
                         "加工原料仅按单号关联产出；已出库不证明实际工序耗用，进货仓库存不计为该单产出或未来供给。",
                         "默认90日只是跟进旧单的筛选参数，不是工艺或供应商交期；不按跨SKU数量计算资产金额。"]}
