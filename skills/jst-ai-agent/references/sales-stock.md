# 销售日明细与库存错配核对

状态：已可执行限定范围的销售日汇总和当前主仓库存核对候选。类型/货权映射仍待业务确认；采购加工到货、库存锁定、多仓和库存历史未验收。不能称为全公司缺货预测或滞销资产诊断。

## 目标与调用

用于回答：近期有哪些 SKU 有订单但主仓实物扣占用后不足、哪些记录有库存但期间内未见有效销售、哪个 SKU 的日订单件数如何变化、哪些字段影响进一步分析。

从 skill 目录调用 `scripts/query.sh`；从项目调用 `./query.sh`。支持：

```sh
scripts/query.sh catalog --limit 10
scripts/query.sh catalog --sku EXAMPLE-SKU
scripts/query.sh sales-lines --month 2026-10 --limit 10
scripts/query.sh sales-daily --days 30 --sku EXAMPLE-SKU
scripts/query.sh stock-sales --days 30 --limit 10
scripts/query.sh stock-sales --month 2026-10 --sku EXAMPLE-SKU
```

默认销售窗口为最近30个上海自然日（含今天的已过时间）；`--days` 为1–90，不能与 `--month` 同用。历史月是自然月，本月截止到查询开始时间；日期均左闭右开。

`--limit` 只限制终端展示行数，范围内所有页仍读取和计算；结果包含 `row_count`、`displayed_row_count` 和 `display_truncated`。需要完整本地白名单结果时使用 `--output /绝对路径/新文件.json`；父目录须存在，已有文件不会覆盖。若加 `--sku`，保存的是该 SKU 的结果，来源数量和候选计数仍说明全量计算范围。

`sales-lines` 是经过既有销售口径过滤后的业务明细投影，不是任意原始订单导出；字段仅含内部订单/明细号、SKU、日期、数量和店铺 ID。库存消耗、取消订单诊断和收入分析不能用它代替各自数据。

## 依赖与指标

引用默认指标卡：`valid_order_qty_v1`、`sales_daily_v1`、`physical_after_order_v1`、`stock_sales_review_v1`。销售过滤复用现有代码，不在本模块重新定义赠品或父单规则。

- 日序列按 SKU，套装按套数。只有本期有有效销售的 SKU 出现在 `sales-daily`；期间未出现的日期补0，限定为此次当前订单页覆盖，不保证归档、缺货需求或库存消耗为0。今天标 `partial_day=true`。
- 普通实物扣占用量：`qty - order_lock`，不含虚拟库存；缺字段为未知，绝不填0。读取时检验 `qty + virtual_qty - order_lock = orderable`，异常停止。
- 组合装使用 ERP `stock`；它是独立装配能力，不是预包成品实物量。共享组件不能在多个套装之间重复承诺，套装与组件不能合计。
- 日均订单量为本期有效数量÷期间实际已过天数。只作为已明确期间的恒定订单需求情景；含已发订单，并非未来新增需求预测。
- 覆盖天数只在启用、自有成品、单位已知、实物字段齐全且无未核实锁定值时提供；为非负实物扣占用量÷上述日均量。没有采购/加工在途、工艺交期和缺货历史，不能给确定缺货日或采购量。

## 分析流程与交付

1. 检查 `ok`/`complete`、范围、读取时间和 `catalog_quality`。查询失败不能发布全量成功结论；字段缺项则保留标记后的候选。
2. 看 `join_checks`：销售 SKU 不在目录时列明未关联项，不能丢掉后称全部匹配。
3. 将 `business_type`/`ownership`/`unit` 和 `type_hint` 分开。名称关键词只是核对提示，不能确认成品或客户货权。
4. 核对三类信号：`order_stock_deficit` 是当前实物扣占用负数；`sales_without_positive_physical_stock` 是期间有有效销售而当前扣占用数非正；`stock_without_period_sales` 是当前有实际库存或组合装装配能力但本期未见销售。信号可以重叠。
5. 先展示有需求且库存信号不足的记录，再列库存无本期销售的候选。判断还要考虑来料、服务、季节品、新品、禁用商品和其他仓库。
6. 回答附 SKU、名称、本期销售数量、实际库存/占用/扣占用量、商品类型与货权、缺项、查询时间和下一步核对。分别展示普通与组合装，不汇总跨单位实物数或金额。

建议先核对实际履约/调拨、采购与加工供给、业务类型；不直接提出按缺口开采购单，不把未见销售等同呆滞，也不把订单占用负数解释成今天刚发生的断货。

## 商品规则的维护

默认按当前公司加载 `config/companies/<company_id>/sku-rules.json`。文件不存在时视为无确认规则，不读取其他公司的规则。业务确认后填入精确 SKU 映射，或通过 `--sku-rules /绝对路径/规则.json` 指定文件。以下为构造模板，`EXAMPLE-SKU` 不是实际商品：

```json
{
  "version": 1,
  "company_id": "当前session-check返回的公司ID",
  "skus": {
    "EXAMPLE-SKU": {
      "business_type": "finished",
      "ownership": "own",
      "unit": "个",
      "source": "商品负责人已确认，日期及依据"
    }
  }
}
```

类型允许 finished/semi_finished/raw_material/packaging/service/unknown；货权允许 own/customer/unknown。单位是确认本 SKU 源数量的计量单位，不是换算指令。`source` 为确认依据；不要用名称猜测批量写成已确认。未知 SKU、公司不匹配、非法类型或空依据会报错。

## 示例与验证

构造示例：实物30、占用10、在售可用数100，期间2.5天有效订单25。实物扣占用量为20，日均10，符合分类等门槛时覆盖情景2天。不能用100计算，也不再次扣25。缺单位、货权或库存时覆盖值为空。

通用 skill 不加载特定企业的历史数量；当前问题重新查询，已保存报告只能作为其 company_id 对应的历史快照。

项目 `tests/test_analysis.py` 验证共用过滤、日序列、虚拟库存、缺字段、单位/货权门槛、套装分离、失配 SKU、规则确认和展示截断等。API完整性沿用既有分页与日期校验。
