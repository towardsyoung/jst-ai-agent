# 默认指标与已验证接入

## 身份、范围及适用性

公司和用户由当前登录会话自动识别，每次查询固定身份并校验返回记录的公司；不使用固定企业ID。会话、Profile、安装和切换说明见 [会话接入](session-access.md)。时区默认 Asia/Shanghai。

下方只读契约曾在初始企业账号验证；通用工具在每次执行时继续核对分页、公司、字段及已知状态。其他企业的权限、模块、状态、数据完整性可能不同，遇到未验收结构就停止或标明缺项，不宣称所有企业的数据域都已实机验收。企业案例与历史数据不随公开仓库或技能包发布。

默认在售与有效订单件数是本工具明确的分析口径，新企业回答时需要说明；不代表平台唯一业务定义。企业要店铺上架、实发或售后净销量时另定义并验证，不能复用当前指标冒充。

## 已核实接口及源字段

核实日期：2026-10-05；首两批扩展于2026-10-07核实。接口和字段需要在后续调用时继续验证，不能仅靠本文认定未变化。

| 数据 | 入口 | 已核实字段/方法 |
| --- | --- | --- |
| 普通商品及主仓库存 | `https://apiweb.erp321.com/webapi/ItemApi/ItemSku/GetPageListV2` | POST，`sku_type=1`，`pageAction=2` 总数；`sku_id,i_id,co_id,enabled,sku_type,qty,virtual_qty,order_lock,orderable` |
| 组合装及库存 | `https://www.erp321.com/app/item/CombineSku/combinesku.aspx` | `JTable1 / LoadDataToJSON`；`sku_id,i_id,co_id,sku_type,enabled,v_stock,stock`；响应可见 `combine_skus`，组件结构尚未建立验证后的映射 |
| 当前订单及商品明细 | `https://www.erp321.com/app/order/order/list.aspx` | `JTable1 / LoadDataToJSON`；`o_id,co_id,type,src_status,order_date,items`；明细 `oi_id,sku_id,i_id,name,qty,is_gift,item_status,sku_type` |
| 当前采购/加工未结、收货及原料 | 见 [采购与加工源契约](supply-review.md) 六个固定页面 | `po_id/poi_id/ioi_id`；SKU量差与收货按SKU核对；原料按单号关联；日期独立标来源 |

网页查询回调先 GET 隐藏表单，再 POST 固定方法。`__VIEWSTATE` 等及会话校验值保持在内存。订单按 `o_id`、组合装按 `sku_id` 稳定排序；显式开启计数，关闭增量翻页模式。解析返回 JSON，忽略可执行脚本字段。

商品、订单、组合装每页核对数量、页码、唯一键和公司，读完复查总数；订单逐条检查日期范围。已有统计不会保存原始订单，CLI 只输出汇总与商品/内部订单号。订单可含客户信息，新增流程先投影必要字段再持久化。

当前验证的是主仓、当前订单页面及上述指标。历史月份命令存在，但归档订单入口、最早覆盖日期、跨月拆合单原始日期和全仓库存均未验收。

## 已实现的默认指标

**在售 SKU 数**：已启用、可用数大于 0；普通和组合装均计入，分开展示。普通商品用 `orderable`：主仓实际库存 + 虚拟库存 − 订单占有数。组合装用 `v_stock`：各子商品可用数 ÷ 用量，取最小值。组合装 `stock` 则用子商品实际库存减占有数计算，不能混用。在售不代表店铺实际上架。

**月订单销量**：按后台 `order_date`，默认本月 1 日 00:00 至查询开始时间，左闭右开；历史月为自然月。排除待付款和赠品；保留有效尚未发货订单。不要只用 `is_paid` 判断：已发货账期订单可能为 false。

当前代码接纳有效订单状态 `WaitConfirm,WaitFConfirm,Question,Delivering,Sent,OuterSent,WaitOuterSent,WaitDeliver,Lock,Finished`，排除 `WaitPay,Cancelled,Split,Merged,Delete,Disabled,SentCancelled` 及禁用单。保留有效拆合单结果，排除各层父单。接纳普通销售和天猫分销销售类型，普通单可带供销标签；初始欠款、换货、补发排除。商品明细排除失效状态、`Replaced`、禁用、赠品和非正数数量。新状态或类型先核对，不自动归类。

销量按 SKU 汇总，同件数并列；`--limit` 限行数，可能截断最后一组并列。套装按销售套数，不展开组件。当前日期按后台现值，跨月拆合单不追溯原父单。`valid_order_count` 是有正数非赠品明细参与销量的订单数。

## 需要新定义的指标

| 指标 | 必须补充的语义/数据 |
| --- | --- |
| 售后净销量 | 完成退款/退货的数量、关联原明细、退货归属日期；区分仅退款与实物退回 |
| 库存消耗 | 发货/出库时点、赠品/换货/补发及其他领用；这些消耗不能因销量口径排除而遗漏 |
| 销售额/收入 | 成交分摊、折扣、运费、税、退款、确认时点；标价不能充当成交收入 |
| 毛利 | 真实出库成本或企业确认成本口径、退款成本、采购/加工等费用；采购价字段不足以证明利润 |
| 周转/滞销 | 连续库存历史、实际出库/成本、单位及期间；单个当前库存快照不能计算平均库存周转率 |
| 缺货/补货 | 实际可履约库存、已分配订单、有效在途及 ETA、供应商交期、BOM、MOQ/包装/预算约束 |

输出这些指标时重新声明口径；不要静默改变已有月销量和在售定义。

## 销售与库存指标卡

新增 `catalog`、`sales-lines`、`sales-daily`、`stock-sales` 复用上述接口，流程见 [销售与库存核对](sales-stock.md)。库存公式不满足时停止；缺实际/占用字段不补零。

| 指标ID/版本 | 粒度和定义 | 依赖、缺项及验证 |
| --- | --- | --- |
| valid_order_qty_v1 | SKU×期间有效订单数量，保持上方默认全部过滤规则 | 复用valid_sales_lines；未知状态、缺赠品、重复等仍停止；sales-lines输出过滤后的投影 |
| sales_daily_v1 | SKU×上海日期；期间连续，今天标未完日；只输出本期销售SKU | 范围内分页及日期校验；零不保证归档/缺货需求为0；与同范围月排行核对 |
| physical_after_order_v1 | 普通qty-order_lock；组合装ERP stock；允许负值 | 缺字段未知；不含虚拟，不再扣本期订单；锁定/多仓未验收 |
| stock_sales_review_v1 | 当前目录关联本期销售，输出扣占用不足/无正数/有库存无本期销售三个可重叠信号 | 类型、货权、单位、失配、禁用和锁定缺项可见；仅核对候选 |
| coverage_order_scenario_v1 | 非负实物扣占用量÷本期订单日均，按实际已过天数计算 | 仅启用、自有成品、单位明确、字段齐全且无未验收锁定值；缺需求返回空；无未来供给和波动，仅情景 |

`complete=true` 表示限定范围分页校验通过；`catalog_quality`/`quality_issues` 另描述业务缺项，不能视为全SKU均可正式判断。`source_reads` 分别记录时刻，非事务快照。展示限行/SKU过滤不改变全量读取和关联，来源及候选计数注明全目录范围。

## 采购、加工及补货情景

`purchase-lines`、`manufacture-lines`、`supply-review` 支持当前非归档未结、收货及原料单号关联；状态、数量/日期定义、单位和货权门槛见 [采购与加工核对](supply-review.md)。账面未结不是实物在途或工序在制，源日期不自动成为可销售日期。

`replenishment-plan` 支持完整日直接订单基线、向前数量验证、逐日缺口与参数化补货/补产情景。指标版本和来源参数要求见 [情景执行模块](replenishment-plan.md)。商品规则及情景公司必须匹配当前身份，无确认参数不编交期、MOQ或采购量；始终 `execution_ready=false`。
