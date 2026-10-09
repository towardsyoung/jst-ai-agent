# jst-ai-agent · 聚水潭 AI 经营分析助手

以聚水潭 ERP 作为数据底座，引入规范化业务指标与 AI 深度分析，帮电商运营与管理者更高效地做决策。

---

## 为什么做这个项目？

### 业务痛点
聚水潭 ERP 沉淀了海量的商品、订单、库存和供应链数据，但日常使用中痛点很明显：
- **功能繁杂、导表低效**：查在售、拉销量、对库存错配，往往需要在不同菜单间跳转，反复导出 Excel 手动加工；
- **口径难对齐**：普通商品与组合装、虚拟可用与实际占用、订单销量与售后净销常常混淆，跨部门沟通成本高；
- **AI 无法直接落地**：直接把杂乱的数据喂给大模型容易产生“数字幻觉”，让 AI 算数经常算错，难以真正指导业务。

### 设计理念
在 AI 时代，经营分析应该更智能、更敏捷：
1. **以聚水潭 ERP 为数据底座**：通过已核实的后台只读接口读取业务数据，免手动复制 Cookie。macOS 可复用日常 Chrome；Windows/Linux 使用专用浏览器正常登录。
2. **代码硬核算数，杜绝 AI 幻觉**：分页请求、状态清洗、指标聚合和库存公式全部由本地 Python 脚本确定性计算，保证数据 100% 准确。
3. **AI 专注经营分析与决策建议**：通过预设的 Skill 规范，让大模型基于精准的计算结果，做异动归因、风险预警和补货建议，真正成为懂业务的经营参谋。
4. **安全纯只读**：不开放任何写接口，不改动任何线上数据；查询凭证只在内存中使用，专用浏览器的原生登录状态保留在用户本机独立目录。

---

## 核心能力一览

| 经营分析场景 | 命令行入口 | AI 能帮你看清什么 |
| :--- | :--- | :--- |
| **在售盘点** | `./query.sh onsale-count` | 当前有多少在售 SKU？普通商品和组合装分别有多少？ |
| **爆款排行** | `./query.sh sales-top` | 本月或指定月份销量 Top 商品有哪些？有效订单件数是多少？ |
| **日销趋势** | `./query.sh sales-daily` | 某个 SKU 近 30 天每日销量走势如何？是否有异常断崖或暴涨？ |
| **产销错配** | `./query.sh stock-sales` | 哪些商品卖得好但库存告急？哪些占着库存却毫无动销？ |
| **资料核查** | `./query.sh catalog` | 商品资料是否规范？主仓库存、单位、货权是否缺失？ |
| **供应链跟进** | `./query.sh supply-review` | 哪些采购单/加工单长期未结案？是否存在交期拖延或超期风险？ |
| **单据明细核对** | `./query.sh purchase-lines`<br>`./query.sh manufacture-lines` | 指定采购单的实收对账；加工单产出与投料关联核对。 |
| **补货决策试算** | `./query.sh replenishment-plan` | 结合真实日销基线与交期，未来哪天会断货？建议补多少？ |

---

## 安装与首次连接

### 1. 环境准备
准备 Python 3.12 或更高版本。源码项目可先克隆；WorkBuddy 用户可直接导入含 `runtime/` 的完整技能包，然后按下方说明初始化。

```bash
git clone https://github.com/towardsyoung/jst-ai-agent.git
cd jst-ai-agent

# macOS/Linux 安装到独立虚拟环境；Windows 使用 py -3 替代 python3
python3 skills/jst-ai-agent/scripts/setup.py
```

### 2. 按平台连接聚水潭

| 平台 | 默认登录方式 | 说明 |
| --- | --- | --- |
| Windows | 独立 Chromium 登录目录 | 首次运行 browser-login；与日常 Chrome 分开 |
| Linux 桌面 | 独立 Chromium 登录目录 | 需图形桌面及浏览器系统依赖 |
| macOS | 日常 Chrome + 钥匙串 | 默认 Default Profile；也可选择专用浏览器 |

Windows（在源码项目根目录，PowerShell）：

```powershell
py -3 skills/jst-ai-agent/scripts/setup.py
py -3 skills/jst-ai-agent/scripts/query.py browser-login
py -3 skills/jst-ai-agent/scripts/query.py session-check
```

Linux 桌面：

```sh
python3 skills/jst-ai-agent/scripts/query.py browser-login
python3 skills/jst-ai-agent/scripts/query.py session-check
```

在弹出的专用浏览器中正常登录、完成验证码并选择公司，然后**关闭整个专用浏览器窗口**。后续查询自动读取该专用会话；登录失效、换账号或切换公司时再次运行 `browser-login`。不需要填写公司和用户ID，也不用发送密码或Cookie。

macOS：在日常 Chrome 登录聚水潭并进入目标公司，再执行：

然后执行连接检查：
```bash
./query.sh session-check
```
*成功后将显示当前登录的 `company_id`、`user_id` 以及接口连通状态。*

> macOS 可用 `--chrome-profile "Profile 1"` 指定 Chrome 目录。若要专用浏览器，先运行 setup.py `--browser`，执行 browser-login，后续查询加 `--auth browser`。Windows/Linux 无需该参数。

首次安装需联网下载依赖和浏览器。Ubuntu/Debian 缺系统库时，按权限运行 `.venv/bin/python -m playwright install-deps chromium`；安装器不会自动提权。没有桌面的 Linux 服务器暂不提供首次登录方案。详细路径、排障与状态清理见 [登录与安装说明](skills/jst-ai-agent/references/session-access.md)。

### 3. 开始分析

下例使用 macOS/Linux 的 shell 入口。Windows 将 `./query.sh` 替换成 `py -3 skills/jst-ai-agent/scripts/query.py`，其余命令和口径一致。

```bash
# 1. 统计当前在售商品数
./query.sh onsale-count

# 2. 查看本月销量 Top 10
./query.sh sales-top --limit 10

# 3. 查看指定历史月份（如 2026 年 8 月）Top 20
./query.sh sales-top --month 2026-08 --limit 20

# 4. 分析近 30 天销售与当前主仓库存错配（找出缺货与滞销候选）
./query.sh stock-sales --days 30 --limit 10

# 5. 查看跟进中、超期的采购与加工单据
./query.sh supply-review --limit 10

# 6. 保存分析报告为本地 JSON（不覆盖历史文件）
./query.sh stock-sales --days 30 --output reports/stock-sales-202610.json
```

---

## 搭配 AI 客户端使用（WorkBuddy / Claude 等）

本项目核心包含了一套结构化分析规范 [`skills/jst-ai-agent/SKILL.md`](skills/jst-ai-agent/SKILL.md)。在支持 Agent Skill 的客户端中加载后，你可以直接用自然语言发起询问：

- **问库存与动销**：“帮我排查一下最近 30 天有稳定销量，但主仓库存撑不到一周的商品。”
- **问大促表现**：“对比上个月和这个月 Top 10 爆款的走势，有哪些新品跑出来了？”
- **问供应链排期**：“查看有哪些加工单已经超过 30 天还没入库，分别卡在哪家工厂？”
- **试算补货**：“针对这款核心商品，以日常日均销量计算，生产周期 20 天，建议什么时间点补货、补多少批次？”

AI 客户端按系统选择 Python 或 shell 查询入口，获取计算结果，再结合上下文输出洞察、风险排查建议与行动清单。

### WorkBuddy 导入说明
1. 从 [v0.2.0 下载页面](https://github.com/towardsyoung/jst-ai-agent/releases/tag/v0.2.0) 下载 `jst-ai-agent.zip`；旧版 v0.1.0 不包含 Windows/Linux 认证。源码也可运行 `python3 scripts/build_skill.py` 打包；
2. 在 WorkBuddy 客户端中选择“添加技能 → 上传技能”导入；
3. 让 WorkBuddy 定位技能目录，执行一次对应平台初始化。解压后的技能目录中路径为 `scripts/setup.py`，不再加 `skills/jst-ai-agent/`：

```powershell
# Windows，在导入后的技能目录执行
py -3 scripts/setup.py
py -3 scripts/query.py browser-login
py -3 scripts/query.py session-check
```

Linux 用 `python3` 替换 `py -3`；macOS 默认只需 setup.py 和 session-check。客户在专用浏览器亲自完成正常登录，再让 WorkBuddy 核对当前公司。之后可以直接问“本月销量最好的商品”“哪些商品需要核对补货”。技能导入不自动安装 Python，系统须先有3.12+。不要向聊天提供登录凭证。

**验证范围**：共用浏览器适配、三平台路径与安装分支、交付包及业务计算已纳入测试；新增 GitHub Actions 的 Windows/Linux/macOS 检查矩阵。Windows/Linux 真实聚水潭登录和各平台 WorkBuddy 执行仍需目标电脑验收。

---

## 核心设计与数据规范

为保证数据准确与业务合规，本项目在底层做好了清晰的口径定义：

- **多公司自动隔离**：从所用认证浏览器的会话识别公司和用户。切换公司后重新查询，企业规则按 `company_id` 隔离。
- **在售口径区分**：普通商品依据主仓真实可下单库存（`orderable > 0`），组合装依据子商品最小可装配数（`v_stock > 0`），避免虚假展示。
- **订单销量口径**：统计有效销售订单，严格剔除未付款、已取消、赠品、换货补发及拆合父单，反映真实的商品销售流速。
- **决策支持定位**：补货与供应链分析旨在给出清晰的数据参照与风险缺口推演，作为运营管理者的决策助手。

---

## 安全与隐私声明

- **本地会话**：查询提取的凭证只在内存中使用，仅发送到聚水潭目标接口，不导出明文登录文件、不交给模型。专用浏览器正常保存本地登录状态，该原生配置目录不得共享、提交Git或随技能分发；关闭浏览器后删除专用目录可清除本机状态。
- **只读零风险**：代码仅包含聚水潭数据的只读读取与统计逻辑，不提供任何修改库存、改动价格或新建修改单据的写操作。
- **代码开源透明**：所有数据抓取和计算逻辑均位于本仓库开源脚本中，欢迎审查与交流。

---

## 项目结构

```text
jst-ai-agent/
├── README.md               # 项目说明
├── query.sh                # 核心查询命令行入口
├── jushuitan.py            # 会话识别与聚水潭只读接口请求
├── jst_browser.py          # Windows/Linux及可选macOS专用浏览器登录
├── jst_analysis.py         # 销量清洗、日均序列与库存错配计算
├── jst_supply.py           # 采购、加工与收货核对模块
├── jst_replenishment.py    # 补货情景与缺口试算
├── skills/jst-ai-agent/    # AI Agent 技能配置与标准化分析流程
│   ├── SKILL.md            # Skill 主入口
│   ├── scripts/            # 跨平台setup.py/query.py与shell兼容入口
│   └── references/         # 各数据域详细口径与业务规则
└── tests/                  # 业务计算单元测试
```

---

## License

MIT License. 欢迎提 Issue 与 PR 交流改进。
