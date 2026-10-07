# 补货与补产情景：第三批执行模块

当前可执行：复用日销量、主仓公有扣占用量、未结采购/加工核对；计算恒定新增订单需求、逐日缺口及有参数来源的补货/补产量。结果始终是情景，`execution_ready=false`，不是已确认采购清单或逐工序排程。

## 调用与数据门槛

```sh
./query.sh replenishment-plan --limit 10
./query.sh replenishment-plan --days 56 --horizon 30 --sku EXAMPLE-SKU
./query.sh replenishment-plan --scenario-file /绝对路径/当前公司的情景.json --output reports/plan-new.json
```

从skill目录用 `scripts/query.sh`，其他目录用项目绝对路径。`--days` 默认56，范围7–90，取上海今天00:00之前的完整日；与sales-daily的“含今天”口径不同。`--horizon` 默认30，范围1–365；不接受历史`--month`或采购筛选参数，当前库存不能冒充历史库存。`--limit`只限制显示，`--sku`在全量读取后限制输出；计数注明筛选前范围。

完整读取当前目录及请求期间有效订单，并复用supply-review当前非归档未结采购/加工核对。候选为本期有效销售、未结供给、当前负扣占用库存、参数及指定SKU的并集；不是所有目录记录都需要采购。销售/供给SKU不在目录时列 `join_checks.unmatched_skus`，不伪称全部已匹配。

普通商品须启用、自有成品、单位明确、库存字段齐全且无未核实锁定值，才能进入数值情景。否则保留基线、源记录和缺项，库存模拟/建议量为空。已确认规则用`--sku-rules`；未知类型/单位/货权可通过情景文件的`sku_assumptions`进行显式条件试算，只能标assumption、不写回确认规则，不能覆盖已知客户货权、非成品或不同单位。缺实际库存不能假设0，禁用和未核实锁定仍阻断。

组合装缺已验证组件BOM时不生成采购/补产量。本版普通SKU只模拟直接SKU新增订单，未加入赠品/换补发、套装组件及领用消耗；这些遗漏、当前订单页归档覆盖和缺货限制销量均在limitations中可见，不能称公司完整需求。

## 参数与批次来源

默认不假定公司交期、安全库存、MOQ或预计到货；无参数仍给事实及数据准备清单。参数文件version=1、与当前会话一致的company_id、name、skus、可选arrivals。每个SKU包含mode=purchase/manufacture、basis=assumption/confirmed、source，以及用于计算的lead_days、review_days、safety_days；包装倍数pack_multiple和MOQ缺失时不输出取整后的建议。交期+复核周期不超过365日。

采购mode指取得该成品至可销售的完整交期；manufacture指该SKU补产至可销售的完整周期。不得把胚体采购交期当成成品补产周期，也不能由单号关联推导原料需求或工艺。安全量采用本情景日均×safety_days；它是参数，不是已回测服务水平库存。

项目 `config/replenishment-scenario-example.json` 是唯一示例参数文件，明确标注EXAMPLE-SKU分类/货权/单位、30日补产、7日复核、7日安全、批量1、MOQ0均为开发假设，未由任何企业确认；其中company_id=910001与EXAMPLE-SKU都是构造标识，使用前必须按当前公司和真实SKU另建文件。工具不会默认使用它。后续业务确认参数另建有真实source的文件；已确认商品规则仍单独维护。

到货批次必须引用实时读取的kind、document_id、line_id，再给batch_id、qty、available_date（上海可销售日期）、unit、stock_scope=main_public、basis、source。日期指可进入同一主仓公有履约范围的日期，不只是运输到厂。可以按同明细分多批，数量合计不得超出账面剩余，重复批次拒绝。

以下只是构造批次结构，单号1不是实际单据：

```json
{
  "kind": "purchase", "document_id": 1, "line_id": 11,
  "batch_id": "第一批", "qty": 100, "available_date": "2026-11-01",
  "unit": "个", "stock_scope": "main_public", "basis": "assumption",
  "source": "仅构造示例，假设该批可销售且进入同一主仓范围"
}
```

ERP协议/预约/预计完成日期不自动作为可销售批次日期，也不覆盖全部剩余；无对应批次就列未确认供给。失效/作废提示、负数冲回待核对、重复SKU未分摊、收货差异等使有效账面剩余为空时，情景参数也不能强行纳入。单位不符、货权未确认、已过预计可用日的批次排除，并保留其全部剩余为待核对。引用当前不存在单据或超配数量停止成功。

## 指标卡

| ID/版本 | 定义与验证 |
| --- | --- |
| direct_order_baseline_v1 | 最近min(28,完整历史天数)日有效直接订单数量均值；同时展示可用的7/28/56日均值。今天不训练；连续日历、负数/未完日校验；零仅限当前订单页覆盖 |
| demand_baseline_backtest_v1 | 每7日向前滚动，用此前窗口均值预测随后7日总量；输出fold_count、周总量MAE、预测减实际的偏差、WAPE；分母0不适用。无当时状态/库存/ETA历史，不是完整补货策略回测 |
| supply_gap_scenario_v1 | `P_t=A0+累计指定可用批次-累计恒定新增订单需求`；A0使用普通qty-order_lock，不含虚拟，不重扣本期订单。保留负A0及当前承诺缺口 |
| replenishment_qty_scenario_v1 | `max(0, 日均×(L+R)+日均×安全天数-A0-期间纳入批次)`；未来L+R窗口可与展示horizon不同，另保留target_simulation |
| pack_adjusted_scenario_v1 | 正建议量先满足MOQ，再向上按pack_multiple取整；原始0不强制采购。Decimal内部计算，不先按展示精度舍入后订货 |

模拟从查询结束后的as_of起，按上海日期划分，当天剩余及末日已过部分按时间折算，需求总量恰好为日均×天数。指定可用日在该日先入库再消耗；未模拟日内入库时刻，因此first_gap_date及最迟下单日仅是日级情景。初始负A0立即呈现。各源读取时刻不同，as_of不是事务一致快照。

核心交付为first_gap_date、max_gap_qty、每日timeline、raw_replenishment_qty、rounded_replenishment_qty、gap_before_new_supply_qty、latest_order_date_scenario及参数来源。`gap_before_new_supply_qty`是在按当前参数交期可取得新供给前的最大缺口；即使窗口总量足够，也保留此前缺口。存在此前缺口时risk_resolved_by_total_quantity=false；其余不默认承诺风险消失。

缺交期/复核/安全参数时建议量为空；缺包装/MOQ时仅原始情景量可算，取整量为空。库存/商品门槛不满足时模拟也为空。analysis_status区分needs_data、scenario_needs_parameters、assumption_scenario、parameterized_scenario，所有状态仍execution_ready=false。

## 分析流程

1. 先读元信息、join_checks和data_issue_counts，说明训练范围、今日排除、当前库存时刻及直接订单消耗边界。
2. 看7/28/56均值、active_sale_days、largest_order_share与误差。大单占比≥50%或销售日≤30%只提示需求不稳定，不自动当成长期规律；没有库存历史不把低销量解释为低需求。
3. 先处理当前负扣占用，再看未结供给及arrival_checks。注明未确认供给尚未扣除，防止条件建议被误用造成重复采购。
4. 对数值情景同时检查horizon_simulation和L+R的target_simulation。展示总量、最早缺口及交期前缺口；不能仅给窗口末日够用。
5. 给SKU×当前主仓的候选动作及需要确认的资料。补产先核对仍履约的加工批次、剩余工序和可销售日期；不能按数量直接分派员工。预算、产能和全厂共享组件未验收时不称整份清单可执行。

## 示例与验收

构造案例：1月1日开始，净库存50、直接日需求10，1月20日可用到货300，交期20、复核10、安全0。未来30日需求300，窗口总量无需新增采购，但1月6日已经出现缺口，到更早供给能到达前最大缺口140。必须给加急/调拨/调整承诺等待核对选项，不能说“在途够，没问题”。

负库存例：A0=-10、日均10、交期1、复核1、安全0、无批次，则原始情景量30；历史销售不再扣一次。当前负数已经体现承诺缺口，不能先截为0而只建议20。

MOQ例：原始20、MOQ25、包装12，取整36；原始0仍为0。缺单位、货权、库存、锁定语义或组件BOM，数量输出为空。冲回待核对或单位不符的到货不能用来消除缺口，仍列unresolved_supply。

项目 `tests/test_replenishment.py` 覆盖完整日/无泄漏数量验证、晚到供给、负库存、时间折算、取整精度、缺数据门槛、假设冲突、未知供给与重复/超配批次。真实第三批报告在项目reports中生成；其中假设参数与ERP读取事实分别标注，现问必须重查。
