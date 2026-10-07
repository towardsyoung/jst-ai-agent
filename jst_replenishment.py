"""当前只读事实上的订单需求与补货/补产情景；参数与确认事实分开。"""
from argparse import Namespace
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_CEILING
import json
from pathlib import Path
from uuid import uuid4

from jushuitan import LOCAL_TZ, QueryError, now, number, timestamp
from jst_analysis import load_rules, numeric, read_catalog, read_sales, sales_daily
from jst_supply import run_supply

D = Decimal


def rounded(value):
    return numeric(value.quantize(D("0.0001"))) if value is not None else None


def validate_plan_options(args):
    if args.month is not None:
        raise QueryError("补货情景使用当前库存及最近完整日，不接受历史月份。")
    if args.days is not None and not 7 <= args.days <= 90:
        raise QueryError("补货情景 --days 应为7到90个完整日。")
    if args.horizon is not None and not 1 <= args.horizon <= 365:
        raise QueryError("--horizon 应为1到365日。")


def load_scenario(company_id, path=None):
    if not path:
        return {"name": "无额外参数", "skus": {}, "arrivals": []}
    try:
        data = json.loads(Path(path).expanduser().read_text())
    except (OSError, ValueError) as exc:
        raise QueryError("情景文件不可读或不是有效JSON。") from exc
    if (not isinstance(data, dict) or data.get("version") != 1 or data.get("company_id") != company_id
            or not isinstance(data.get("name"), str) or not data["name"].strip()
            or not isinstance(data.get("skus"), dict) or not isinstance(data.get("arrivals", []), list)):
        raise QueryError("情景文件需version=1、与当前会话一致的company_id、名称、skus及可选arrivals数组。")
    for sku, policy in data["skus"].items():
        if not isinstance(sku, str) or not sku or not isinstance(policy, dict):
            raise QueryError("情景SKU或参数结构无效。")
        if (policy.get("basis") not in {"assumption", "confirmed"} or not isinstance(policy.get("source"), str)
                or not policy["source"].strip()):
            raise QueryError("每项情景参数必须声明basis和来源source。")
        if policy.get("mode") not in {"purchase", "manufacture"}:
            raise QueryError("参数mode应为purchase或manufacture；不自动选择买入或补产。")
        allowed = {"basis", "source", "mode", "lead_days", "review_days", "safety_days",
                   "pack_multiple", "moq", "sku_assumptions"}
        if set(policy) - allowed: raise QueryError("情景参数包含未知字段。")
        for key, minimum in (("lead_days", 0), ("review_days", 1), ("safety_days", 0)):
            if key in policy and (type(policy[key]) is not int or not minimum <= policy[key] <= 365):
                raise QueryError("%s需为范围内整数天数。" % key)
        if policy.get("lead_days", 0) + policy.get("review_days", 0) > 365:
            raise QueryError("交期加复核周期不能超过365日。")
        for key in ("pack_multiple", "moq"):
            if key in policy:
                value = number(policy[key], key)
                if value < 0 or (key == "pack_multiple" and value == 0):
                    raise QueryError("包装倍数须正数，MOQ须非负。")
        assumed = policy.get("sku_assumptions", {})
        if not isinstance(assumed, dict) or set(assumed) - {"business_type", "ownership", "unit"}:
            raise QueryError("商品假设仅允许类型、货权和单位。")
        if assumed and policy["basis"] != "assumption":
            raise QueryError("商品假设只能声明assumption；确认商品规则请使用sku-rules。")
        if (assumed.get("business_type", "finished") != "finished" or assumed.get("ownership", "own") != "own"
                or ("unit" in assumed and (not isinstance(assumed["unit"], str) or not assumed["unit"].strip()))):
            raise QueryError("本版只支持自有成品的直接订单情景；单位不得为空。")
    seen = set()
    for arrival in data.get("arrivals", []):
        required = {"kind", "document_id", "line_id", "batch_id", "qty", "available_date",
                    "unit", "stock_scope", "basis", "source"}
        if not isinstance(arrival, dict) or set(arrival) != required:
            raise QueryError("到货批次字段不完整或含未知字段。")
        if (arrival["kind"] not in {"purchase", "manufacture"} or arrival["basis"] not in {"assumption", "confirmed"}
                or arrival["stock_scope"] != "main_public"
                or any(type(arrival[k]) is not int or arrival[k] <= 0 for k in ("document_id", "line_id"))
                or any(not isinstance(arrival[k], str) or not arrival[k].strip() for k in ("batch_id", "unit", "source"))
                or number(arrival["qty"], "arrival qty") <= 0):
            raise QueryError("到货批次的来源、主仓范围、单位、标识或数量无效。")
        try:
            parsed = date.fromisoformat(arrival["available_date"])
            if parsed.isoformat() != arrival["available_date"]: raise ValueError
        except (ValueError, TypeError) as exc:
            raise QueryError("available_date须为上海可用日期YYYY-MM-DD。") from exc
        key = tuple(arrival[k] for k in ("kind", "document_id", "line_id", "batch_id"))
        if key in seen: raise QueryError("到货批次重复，不能重复计供给。")
        seen.add(key)
    return {**data, "arrivals": data.get("arrivals", [])}


def effective_product(item, policy):
    effective = dict(item)
    assumptions = {}
    for field, value in policy.get("sku_assumptions", {}).items():
        actual = item.get(field)
        if actual not in (None, "", "unknown") and actual != value:
            raise QueryError("%s的商品假设与已有%s冲突。" % (item["sku_id"], field))
        if actual in (None, "", "unknown"):
            effective[field] = value
            assumptions[field] = value
    return effective, assumptions


def demand_baseline(series):
    values = [number(x["qty"], "daily qty") for x in series]
    if any(x.get("partial_day") for x in series) or any(v < 0 for v in values):
        raise QueryError("训练序列含未完日或负数订单量。")
    days = len(values)
    if days < 7: raise QueryError("不足7个完整日，不能生成本版需求情景。")
    for index in range(1, days):
        if date.fromisoformat(series[index]["date"]) - date.fromisoformat(series[index-1]["date"]) != timedelta(days=1):
            raise QueryError("训练日历不连续，不填缺失日期为零。")
    window = min(28, days)
    rate = sum(values[-window:], D(0)) / window
    comparisons = {str(w): rounded(sum(values[-w:], D(0))/w) for w in (7, 28, 56) if days >= w}
    folds = []
    for origin in range(window, days - 6, 7):
        forecast = sum(values[origin-window:origin], D(0)) / window * 7
        actual = sum(values[origin:origin+7], D(0))
        folds.append((forecast, actual))
    actual_sum = sum((a for _, a in folds), D(0))
    error_sum = sum((abs(p-a) for p, a in folds), D(0))
    backtest = {"fold_count": len(folds), "test_horizon_days": 7,
                "mae_week_qty": rounded(error_sum/len(folds)) if folds else None,
                "bias_week_qty": rounded(sum((p-a for p, a in folds), D(0))/len(folds)) if folds else None,
                "wape_percent": rounded(error_sum/actual_sum*100) if actual_sum else None,
                "basis": "以当前订单页重建的数量做向前验证；未重建当时订单状态、库存及ETA，不是补货策略回测"}
    return rate, {"history_days": days, "baseline_window_days": window, "mean_daily_order_qty": rounded(rate),
                  "comparison_daily_means": comparisons, "history_order_qty": rounded(sum(values, D(0))),
                  "active_sale_days": sum(v > 0 for v in values), "demand_backtest": backtest}


def arrival_batches(supply_rows, scenario, catalog, as_of):
    sources = {(r["kind"], r["document_id"], r["line_id"]): r for r in supply_rows}
    if len(sources) != len(supply_rows): raise QueryError("供给明细重复。")
    allocated, accepted, batches, checks = defaultdict(Decimal), defaultdict(Decimal), defaultdict(list), []
    for arrival in scenario["arrivals"]:
        key = tuple(arrival[k] for k in ("kind", "document_id", "line_id"))
        row = sources.get(key)
        if row is None: raise QueryError("情景到货引用当前未结范围不存在的单据明细，请刷新批次规则。")
        sku = row["sku_id"]
        if sku not in catalog: raise QueryError("到货SKU不在当前目录，不能确定数量单位。")
        qty = number(arrival["qty"], "arrival qty")
        allocated[key] += qty
        if allocated[key] > number(row["remaining_qty"], "remaining qty"):
            raise QueryError("到货批次数量合计超过来源账面未收量，不能重复分配。")
        product, _ = effective_product(catalog[sku], scenario["skus"].get(sku, {}))
        reasons = []
        if row["eligible_book_remaining_qty"] is None: reasons.append("source_remaining_needs_review")
        if product["unit"] != arrival["unit"]: reasons.append("arrival_unit_unconfirmed_or_mismatch")
        if date.fromisoformat(arrival["available_date"]) < as_of.date(): reasons.append("arrival_date_past_due")
        if product["ownership"] != "own": reasons.append("arrival_ownership_unconfirmed")
        if product["business_type"] != "finished" or product["sku_type"] != "normal" or not product["enabled"]:
            reasons.append("arrival_product_inapplicable")
        checks.append({**arrival, "sku_id": sku, "included": not reasons, "exclusion_reasons": reasons})
        if not reasons:
            accepted[key] += qty
            batches[sku].append({**arrival, "sku_id": sku})
    unresolved = []
    for key, row in sources.items():
        remaining = number(row["remaining_qty"], "remaining qty")
        if remaining <= 0: continue
        unallocated = remaining - accepted[key]
        if unallocated > 0 or row["eligible_book_remaining_qty"] is None:
            unresolved.append({"kind": row["kind"], "document_id": row["document_id"], "line_id": row["line_id"],
                               "sku_id": row["sku_id"], "remaining_qty": rounded(remaining),
                               "unallocated_qty": rounded(unallocated), "erp_expected_date": row["expected_date"],
                               "reasons": row["quality_issues"] + ["usable_batch_date_and_main_scope_not_confirmed"]})
    return batches, checks, unresolved


def simulate(as_of, stock, rate, batches, days):
    end = as_of + timedelta(days=days)
    arrivals = defaultdict(Decimal)
    for batch in batches: arrivals[batch["available_date"]] += number(batch["qty"], "arrival qty")
    balance, cursor, timeline = stock, as_of, []
    risk = as_of.date().isoformat() if stock < 0 else None
    max_gap, total_arrivals = max(-stock, D(0)), D(0)
    while cursor < end:
        day_end = min(end, datetime.combine(cursor.date()+timedelta(days=1), datetime.min.time(), LOCAL_TZ))
        duration = D(str((day_end-cursor).total_seconds()))/86400
        arriving = arrivals[cursor.date().isoformat()]
        total_arrivals += arriving
        balance += arriving
        demand = rate*duration
        balance -= demand
        if balance < 0 and risk is None: risk = cursor.date().isoformat()
        gap = max(-balance, D(0))
        max_gap = max(max_gap, gap)
        timeline.append({"date": cursor.date().isoformat(), "duration_days": rounded(duration),
                         "arrival_qty": rounded(arriving), "new_order_demand_qty": rounded(demand),
                         "projected_qty": rounded(balance), "shortage_qty": rounded(gap)})
        cursor = day_end
    return {"first_gap_date": risk, "max_gap_qty": rounded(max_gap), "end_projected_qty": rounded(balance),
            "arrival_qty_in_period": rounded(total_arrivals), "new_order_demand_qty": rounded(rate*days),
            "timeline": timeline}


def plan_product(item, series, supply_rows, batches, policy, as_of, horizon, largest_order_qty):
    rate, baseline = demand_baseline(series)
    effective, assumed = effective_product(item, policy)
    problems = []
    if effective["sku_type"] != "normal": problems.append("kit_component_bom_required")
    if not effective["enabled"]: problems.append("disabled_sku")
    if effective["business_type"] != "finished": problems.append("finished_type_unconfirmed_or_inapplicable")
    if effective["ownership"] != "own": problems.append("ownership_unconfirmed_or_customer")
    if not effective["unit"]: problems.append("unit_unconfirmed")
    if effective["physical_after_order_qty"] is None: problems.append("physical_stock_missing")
    if effective["stock_lock_present"]: problems.append("stock_lock_unverified")
    risks = ["current_order_page_only", "direct_orders_exclude_gifts_replacements_and_component_demand",
             "main_public_stock_not_verified_all_warehouse_fulfillment", "no_stockout_history"]
    total = sum((number(x["qty"], "daily qty") for x in series), D(0))
    largest_share = largest_order_qty/total if total else None
    if largest_share is not None and largest_share >= D("0.5"): risks.append("large_order_dominance")
    if baseline["active_sale_days"]/baseline["history_days"] <= 0.3: risks.append("intermittent_or_no_order_demand")
    pending = [r for r in supply_rows if number(r["remaining_qty"], "remaining qty") > 0 and
               sum((number(b["qty"], "arrival qty") for b in batches if b["kind"] == r["kind"] and
                    b["document_id"] == r["document_id"] and b["line_id"] == r["line_id"]), D(0))
               < number(r["remaining_qty"], "remaining qty")]
    if pending: risks.append("pending_existing_supply_not_deducted")
    result = {"sku_id": item["sku_id"], "name": item["name"], "sku_type": item["sku_type"],
              "unit": effective["unit"], "unit_source": "assumption" if "unit" in assumed else item.get("unit_source"),
              "stock_scope": "主仓公有", "physical_after_order_qty": item["physical_after_order_qty"],
              "business_type": effective["business_type"], "ownership": effective["ownership"],
              "sku_assumptions": assumed, "sku_rule_source": item.get("rule_source"),
              "policy": policy or None, "baseline": baseline, "largest_order_share": rounded(largest_share),
              "source_supply_line_count": len(supply_rows), "unresolved_supply_line_count": len(pending),
              "included_arrival_batches": batches,
              "data_issues": problems, "limitations": risks, "execution_ready": False,
              "analysis_status": "needs_data" if problems else "scenario",
              "horizon_simulation": None, "target_window_days": None, "raw_replenishment_qty": None,
              "rounded_replenishment_qty": None, "gap_before_new_supply_qty": None,
              "latest_order_date_scenario": None, "suggested_available_date_scenario": None,
              "risk_resolved_by_total_quantity": None}
    if problems: return result
    stock = number(item["physical_after_order_qty"], "physical_after_order_qty")
    result["horizon_simulation"] = simulate(as_of, stock, rate, batches, horizon)
    missing = [key for key in ("lead_days", "review_days", "safety_days") if key not in policy]
    result["missing_parameters"] = missing
    if missing:
        result["analysis_status"] = "scenario_needs_parameters"
        return result
    window = policy["lead_days"]+policy["review_days"]
    target = simulate(as_of, stock, rate, batches, window)
    before_new = simulate(as_of, stock, rate, batches, policy["lead_days"])
    safety = rate*policy["safety_days"]
    # 保留负A0以呈现当前承诺缺口，不把它截成0，也不再扣本期历史订单。
    cutoff = as_of+timedelta(days=window)
    arrival_total = sum((number(b["qty"], "arrival qty") for b in batches
                         if datetime.combine(date.fromisoformat(b["available_date"]), datetime.min.time(), LOCAL_TZ) < cutoff), D(0))
    raw = max(D(0), rate*window+safety-stock-arrival_total)
    adjusted = None
    if "pack_multiple" in policy and "moq" in policy:
        multiple = number(policy["pack_multiple"], "pack_multiple")
        adjusted = ((max(raw, number(policy["moq"], "moq"))/multiple).to_integral_value(rounding=ROUND_CEILING)
                    *multiple) if raw > 0 else D(0)
    else: result["missing_parameters"].extend(key for key in ("pack_multiple", "moq") if key not in policy)
    risk = result["horizon_simulation"]["first_gap_date"]
    result.update(target_window_days=window, target_simulation=target, safety_qty_scenario=rounded(safety),
                  raw_replenishment_qty=numeric(raw), rounded_replenishment_qty=numeric(adjusted) if adjusted is not None else None,
                  gap_before_new_supply_qty=before_new["max_gap_qty"],
                  latest_order_date_scenario=(date.fromisoformat(risk)-timedelta(days=policy["lead_days"])).isoformat() if risk else None,
                  suggested_available_date_scenario=(as_of.date()+timedelta(days=policy["lead_days"])).isoformat(),
                  risk_resolved_by_total_quantity=False if before_new["max_gap_qty"] > 0 else None,
                  analysis_status="assumption_scenario" if assumed or policy["basis"] == "assumption" or
                                  any(b["basis"] == "assumption" for b in batches) else "parameterized_scenario")
    return result


def run_plan(client, args):
    validate_plan_options(args)
    scenario = load_scenario(client.company_id, args.scenario_file)
    started = now().replace(microsecond=0)
    end = started.replace(hour=0, minute=0, second=0)
    start = end-timedelta(days=args.days if args.days is not None else 56)
    rules = load_rules(client.company_id, args.sku_rules)
    reads = []
    read_start = now()
    catalog = read_catalog(client, rules)
    reads.append({"entity": "catalog_and_stock", "started_at": timestamp(read_start), "finished_at": timestamp(now())})
    products = {r["sku_id"]: r for r in catalog}
    if set(scenario["skus"]) - set(products) or (args.sku and args.sku not in products):
        raise QueryError("情景或指定SKU不在当前目录。")
    read_start = now()
    lines, sales_stats = read_sales(client, start, end)
    daily = {r["sku_id"]: r for r in sales_daily(lines, start, end)}
    reads.append({"entity": "sales", "started_at": timestamp(read_start), "finished_at": timestamp(now())})
    # 内部复用第二批全范围核对，不将销售期间或展示SKU传给供给读取。
    supply = run_supply(client, Namespace(command="supply-review", kind=None, document_id=None, age_days=None,
                                          month=None, days=None, sku=None, sku_rules=args.sku_rules))
    reads.extend(supply["source_reads"])
    as_of = now().replace(microsecond=0)
    batches, arrival_checks, unresolved = arrival_batches(supply["rows"], scenario, products, as_of)
    by_sku = defaultdict(list)
    for row in supply["rows"]: by_sku[row["sku_id"]].append(row)
    order_totals = defaultdict(lambda: defaultdict(Decimal))
    for line in lines: order_totals[line["sku_id"]][line["order_id"]] += number(line["qty"], "qty")
    candidates = set(daily) | set(by_sku) | set(scenario["skus"])
    candidates.update(r["sku_id"] for r in catalog if r["physical_after_order_qty"] is not None and r["physical_after_order_qty"] < 0)
    if args.sku: candidates.add(args.sku)
    unmatched = sorted(candidates-set(products))
    unmatched_demand = [{"sku_id": sku, "name": daily[sku]["name"], "sku_type": daily[sku]["sku_type"],
                         "history_order_qty": daily[sku]["qty"], "analysis_status": "catalog_match_required",
                         "reason": "当前目录未匹配，不参与库存/补货计算"} for sku in unmatched if sku in daily]
    empty_series = [{"date": (start+timedelta(days=i)).date().isoformat(), "qty": 0, "partial_day": False}
                    for i in range((end-start).days)]
    results = []
    for sku in sorted(candidates & set(products)):
        if sku in daily and daily[sku]["sku_type"] not in (None, products[sku]["sku_type"]):
            raise QueryError("需求与当前目录SKU类型不一致。")
        results.append(plan_product(products[sku], daily[sku]["daily"] if sku in daily else empty_series,
                                    by_sku[sku], batches[sku], scenario["skus"].get(sku, {}), as_of,
                                    args.horizon if args.horizon is not None else 30,
                                    max(order_totals[sku].values(), default=D(0))))
    results.sort(key=lambda r: (r["horizon_simulation"] is None,
                               (r["horizon_simulation"] or {}).get("first_gap_date") or "9999-12-31",
                               -r["baseline"]["history_order_qty"], r["sku_id"]))
    full_count = len(results)
    statuses = dict(Counter(r["analysis_status"] for r in results))
    issues = dict(Counter(issue for r in results for issue in r["data_issues"]))
    if args.sku:
        results = [r for r in results if r["sku_id"] == args.sku]
        arrival_checks = [r for r in arrival_checks if r["sku_id"] == args.sku]
        unresolved = [r for r in unresolved if r["sku_id"] == args.sku]
    return {"ok": True, "complete": True, "execution_ready": False, "company_id": client.company_id,
            "user_id": client.user_id,
            "run_id": str(uuid4()), "capability": args.command, "contract_version": "replenishment_scenario_v1",
            "started_at": timestamp(started), "finished_at": timestamp(now()), "as_of": timestamp(as_of),
            "source_reads": reads, "training_start": timestamp(start), "training_end_exclusive": timestamp(end),
            "horizon_days": args.horizon if args.horizon is not None else 30, "scenario_name": scenario["name"],
            "metric": "有效直接SKU订单均值的恒定新增需求情景，不是实物消耗或确定缺货预测",
            "coverage": "当前目录/主仓公有，当前订单页最近完整日，当前非归档未结采购加工；未验收归档/全仓/锁定/组件需求",
            "candidate_scope": "本期有效销售、未结供给、当前负库存、情景参数或指定SKU的并集",
            "full_candidate_count": full_count, "status_counts": statuses, "data_issue_counts": issues,
            "counts_scope": "筛选前完整候选范围；--sku仅限制结果行与供给核对输出", "sku_filter": args.sku,
            "sales_checks": sales_stats, "join_checks": {"catalog_count": len(catalog), "sold_sku_count": len(daily),
                                                        "unmatched_skus": unmatched},
            "unmatched_demand": unmatched_demand,
            "supply_checks": {"header_counts": supply["header_counts"], "line_count": supply["row_count"],
                              "receipt_count": supply["receipt_row_count"], "quality_counts": supply["quality_counts"]},
            "arrival_checks": arrival_checks, "unresolved_supply": unresolved,
            "rows": results, "row_count": len(results),
            "warnings": ["基线仅覆盖当前订单页的有效下单量，排除赠品/换补发及套装组件；未作缺货校正，不是完整库存消耗。",
                         "只读当前主仓公有扣占用值；不含虚拟，不再扣历史订单；多仓/冻结/履约边界仍需核对。",
                         "没有确认商品规则或显式商品假设时不生成库存时序/补货数量；假设不会写入sku-rules或ERP。",
                         "源单据预计日期不自动成为可用到货；只纳入有来源、单位和主仓范围的批次数量，负数冲回等待核对供给隔离。",
                         "按上海可用日期先入库再消耗；当前及末日按剩余/经过时间折算，未模拟日内到货时刻，不承诺精确断货时点。",
                         "历史误差仅验证订单数量基线，无当时库存/状态/ETA，不能称完整补货策略回测或统计置信区间。",
                         "数量、日期是参数化情景；预算、产能、工艺/BOM和现场批次未验收，不能直接作为采购或员工排产指令。",
                         "各数据域读取时刻不同；as_of用于情景起点，不表示所有事实在此时事务一致。"]}
