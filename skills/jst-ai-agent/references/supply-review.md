# 采购与加工未结核对

本模块已可执行。目标是确认账面未收、识别需要跟进的单据，支持后续补货/补产分析的数据准备；尚不提供可承诺的实物在途、逐工序排产或真实积压金额。

## 入口与范围

从项目运行，或使用 skill 的 `scripts/query.sh`：

```sh
./query.sh supply-review --limit 10
./query.sh purchase-lines --document-id 1 --limit 10
./query.sh manufacture-lines --document-id 1 --limit 10
./query.sh supply-review --kind manufacture --age-days 120
./query.sh supply-review --kind manufacture --sku EXAMPLE-SKU --output reports/sku-supply-new.json
```

默认完整读取当前非归档的待审核、已确认、外部确认中单据，不按最近下单日期截断旧未结单。`--month/--days` 不适用；`--age-days` 默认90，范围1–3650，仅为旧单跟进参数，不代表工艺交期。指定单号时核对该单实际状态；`supply-review --document-id` 必须同时指定 `--kind purchase/manufacture`。当前支持普通采购和传统组装/拆分/改码加工入口；新通仓加工未验收。

请求范围内逐单读取 SKU、收货及加工原料；超过500张单据先停止，改为指定单号核对。`--sku` 在完整读取后限制商品及对应收货输出，同时保留这些加工单的全部原料；这只是单据级关系。`header_counts/document_checks` 仍是请求范围，不能误报成目标SKU单据数。`--limit` 只限制终端各明细数组；`--output` 保存完整白名单 JSON，不覆盖已有文件。

## 源契约（2026-10-07 核实）

所有页面均为 `www.erp321.com`，读取方法为 `JTable1 / LoadDataToJSON`，沿用内存表单、本次会话自动识别的公司和用户校验。只解析JSON，不执行返回脚本。

| 实体 | 页面路径 | 唯一键、关联及数量语义 |
| --- | --- | --- |
| 采购单头 | `/app/scm/purchase/purchasemode.aspx` | `po_id`；状态源过滤 `WaitConfirm,Confirmed,OuterConfirming`；非归档 |
| 加工单头 | `/app/scm/manufacture/manufacture.aspx` | `po_id`；相同未结状态；加工类型、新通仓标识额外校验 |
| 采购SKU | `/app/scm/purchase/purchaseitem.aspx` | `poi_id`，检查 `po_id`；`qty`采购量，`ioQty`采购入库减采购退货，`diffQty=qty-净入库` |
| 加工产出SKU | `/app/scm/manufacture/manufactureItem.aspx`，`type=sku` | `poi_id`，检查 `po_id`；`qty`加工量，`ioQty`已入库，`diffQty=qty-已入库` |
| 关联收货 | `/app/scm/purchase/PurchaseInItemV2.aspx` | `ioi_id`与`io_id`；请求指定来源 `po_id`，响应按SKU核对；目前已验收采购进仓/加工进仓 |
| 加工原料 | `/app/scm/manufacture/manufactureItemRM.aspx`，`type=rm` | `poi_id`及`po_id`；`qty`加工原料表的加工数量，`out_qty`已出库，`in_qty`进货仓库存 |

回调采购单头 `qty_count` 当前是空占位，页面另外补充它；未接入该补充方法。不要填0或假称已完成采购单头总数对账。返回 `header_qty_matches=null`、`header_qty_unavailable`，依据 SKU 与收货核对账面剩余。加工单头有值时另与全部产出SKU数量求和核对。头明细不一致则保留异常、不给有效剩余；未知状态、分页/编号/公司异常、未验收退货类型或收货净量为负时停止成功统计。已核实进仓类型的负数数量参与有符号求和，并标记待复核，不能按绝对值累加入库。

`receipt_rows` 是已发生记录，不能再次计为未来到货。收货响应不带 `poi_id`；同单重复SKU只能核对SKU总量，逐行归属未验证时 `eligible_book_remaining_qty=null`，不擅自按比例分摊。全范围重复收货唯一键停止，防止双算。

加工原料只按 `po_id` 与同单全部产出关联，不推导一对一BOM系数。`issued_qty` 是账面出库，不证明工序实耗；`incoming_warehouse_stock_qty` 是当前库存，不是该单采购入库、在制或产出。实际消耗、版本BOM、损耗/返工及工序批次须另接。

## 指标卡与日期

| ID/版本 | 粒度、公式及缺项 |
| --- | --- |
| received_net_qty_v1 | 单据×SKU行；`qty-diffQty`，`ioQty`非空时必须相等；两个必需量缺失则停止，不把空入库字段当0 |
| book_remaining_qty_v1 | 保留有符号`diffQty`；展示`remaining_qty=max(0,diffQty)`；负剩余另标超收 |
| eligible_book_remaining_qty_v1 | 已确认、无作废词提示/失效状态、无收货异常/负数冲回待核对/对账差异/未分摊重复SKU的账面正剩余；其余返回空。缺单位/货权/ETA仍须另审，不能把它当作可履约量 |
| supply_review_v1 | 当前单据逐SKU及收货核对；候选旗标可重叠，行数与去重单据数分别展示 |

日期独立保留：采购 `delivery_date` 是协议到货；`min_plan_arrive_date` 是预约最早预计到货；加工 `delivery_date` 是预计加工完成。输出分别为 `agreement_or_completion_date`、`booking_min_expected_date`，并给核对展示用的 `expected_date` 及来源。采购 `plan_arrive_qty` 是协议到货量，加工同名字段是预计完成量；采购 `expect_arrive_qty` 是预计预约到货量。单个日期不能覆盖全部剩余，需要批次到货量与日期才能进入供给时序。

`age_days` 从单据业务日期算起，`days_since_modified` 从修改日期算起；两者都不是车间暂停时长。`old_open_document` 看正剩余与年龄；`past_planned_date` 看正剩余及有来源的计划日期；`partial_receipt` 看正净收且仍有正剩余；`zero_balance_open_document` 看零剩余但仍未结。超收另标质量项。备注只保存“作废/取消/红冲词提示”布尔判定的结果，不保存原文，也不替代正式状态。

## 分析流程与交付

1. 根据用户问题选采购、加工或指定单号；说明非归档当前状态覆盖，不声明全历史。
2. 读取并核对数量、状态、收货关联。先复核作废词、超收、对账差异、重复SKU和零剩余未关，再看旧单及计划日期。
3. 按SKU确认类型、单位、货权；费用项、包材、来料不自动算销售成品或公司资产。使用带依据的 `config/companies/<当前company_id>/sku-rules.json`；无依据保持未知。
4. 为跟进清单列单号、SKU、状态、订购/加工量、已收、剩余、计划日期来源、年龄、最近变更、异常和下一步核对。数量按SKU原录入数值展示，单位未确认就注明，不跨SKU求总件数/资产。
5. 跨库存/销售分析时再加载 sales-stock.md 与 replenishment.md。已收量属于历史，不重复计供给；账面原料不能直接视为未来成品。确认批次实际状态、可销售完成日期及可用数量后才进入补产情景。

`ok=true/complete=true` 表示请求范围读取完整、必需契约校验通过，质量差异仍可存在并可见。`source_reads` 记录各域时段；完成后复查头状态/数量/修改日期，变化就停止重试。仍不是跨域事务快照。报告生成跟进建议，不自行改状态、关单、追供应商或扣罚员工。

## 示例与验收

构造示例：采购50、两次收货9和11，净收20、账面剩余30。不是采购50加库存20等于70份供给；如果备注提示作废，保留30作为待核对账面量，但有效剩余返回空。某行超收2，展示有符号剩余-2及超收，不当成未来供给或自动冲销。

回归覆盖分批收货、空入库字段、缺必需量、缺头总数、头明细差异、备注作废、未知状态/退货类型/负净收停止、已核实进仓负数冲回净量、收货差异、重复SKU、超收、未关单、读取时变化、原料库存隔离和输出限行。目前支持已核实进仓类型的有符号净量，不将数字冲回猜成具体财务红冲或采购退货。其他退货类型、负净收以停止并要求核实为验收边界。
