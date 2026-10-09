# 登录身份、公司切换及本地安装

## 平台与认证选择

公司、用户从本次会话的 `u_co_id`、`u_id` 自动识别。客户无需配置公司ID、复制Cookie或向模型提供密码。只读身份可显示在连接结果中。

| 平台 | 默认方式（`--auth auto`） | 客户首次操作 |
| --- | --- | --- |
| Windows | Playwright 专用 Chromium 浏览器 | 运行 `browser-login`，正常登录并选公司，关闭专用浏览器 |
| Linux 桌面 | 同上 | 同上；需图形桌面与浏览器系统库 |
| macOS | 原有 Chrome 钥匙串方式 | 在日常 Chrome 正常登录；默认 Default Profile |
| macOS 可选 | `--auth browser` 专用浏览器 | setup 加 `--browser`；登录后查询始终加 `--auth browser` |

Windows/Linux 不读取日常 Chrome 的加密 Cookie 数据库，不更改浏览器安全设置。专用浏览器使用独立目录，与客户日常 Chrome 的登录状态分别维护。正常登录后会话随浏览器保存在本地原生配置目录；查询仅在内存中提取ERP域凭证，不导出明文 Cookie、storage_state、密码或鉴权配置文件。

代码已实现三平台分支、入口和安装；本机可验证共用浏览器路径，Windows/Linux 客户端上的真实登录与 WorkBuddy 执行仍须在目标系统验收。不要把离线分支测试表述为三平台实机通过。

## 安装与定位

技能目录包含 `scripts/`、`runtime/`（交付包）。从这个目录执行以下命令；其他工作目录使用脚本绝对路径。依次定位显式 `JST_AI_HOME`、技能内 runtime、项目相对位置；符号链接按真实目录定位，不依赖开发者路径。

准备 Python 3.12 或更新版本。完整包包含源码和依赖清单，不含 Python、虚拟环境或浏览器二进制。安装需联网下载依赖与专用浏览器。

Windows PowerShell：

```powershell
py -3 scripts/setup.py
py -3 scripts/query.py browser-login
py -3 scripts/query.py session-check
```

若 `py` 不存在且 `python --version` 是3.12或更高，把 `py -3` 换成 `python`。多版本时可用 `py -3.12` 或已确认的解释器绝对路径；勿猜安装位置。

Linux 桌面：

```sh
python3 scripts/setup.py
python3 scripts/query.py browser-login
python3 scripts/query.py session-check
```

Ubuntu/Debian 如提示缺少浏览器系统库，在确认管理员权限和系统适配后运行 `runtime/.venv/bin/python -m playwright install-deps chromium`，再重试。源码项目中的虚拟环境在项目根 `.venv/`。安装器不自动提权或修改系统软件；其他发行版按 Playwright 支持范围核对。首次登录需要桌面会话，没有桌面的服务器暂不提供扫码/远程登录方案。

macOS 默认复用日常 Chrome：

```sh
python3 scripts/setup.py
scripts/query.sh session-check
```

macOS 选择专用浏览器：

```sh
python3 scripts/setup.py --browser
python3 scripts/query.py browser-login
python3 scripts/query.py session-check --auth browser
```

安装只创建独立 `.venv`、安装依赖与浏览器，不读取账号或业务数据。macOS/Linux 的 `sh scripts/setup.sh` 是 Python 安装器的兼容入口，可用 `JST_PYTHON` 指定已确认的解释器。

## WorkBuddy 接入

1. 导入**包含 runtime 的新版本技能包**。旧版本 v0.1.0 仅支持 macOS 认证，导入旧包不会获得此能力。
2. 让 WorkBuddy 定位已安装的本技能目录和 Python 3.12+，按上方对应平台运行 setup.py。不要写死个人技能路径，也不要自动下载或执行来历不明的安装命令。
3. Windows/Linux：让 WorkBuddy 运行 browser-login，在弹出的专用浏览器由客户正常登录、验证和选择公司。完成后**关闭整个专用浏览器窗口**，不是只关登录页面；命令最多等待10分钟。
4. 运行 session-check，确认返回 company_id、user_id 及商品接口权限，再开始自然语言分析。商品可读不证明所有数据域可读。

WorkBuddy 导入技能仍不等于自动安装 Python 或获得浏览器登录，须完成一次初始化。登录失效、换账号或切换公司，重新运行 browser-login；普通查询不会弹窗，不会代用户处理验证码。

## 查询、公司切换与退出

所有业务命令一致。Windows 使用 `py -3 scripts/query.py`，Linux 使用 `python3 scripts/query.py`；macOS/Linux 原来的 `scripts/query.sh` 也可用。例如：

```powershell
py -3 scripts/query.py onsale-count
py -3 scripts/query.py sales-top --limit 10
py -3 scripts/query.py stock-sales --days 30
```

macOS Chrome 多Profile时由客户显式选 `--chrome-profile "Profile 1"`，不扫描其他账号。该参数不能用于专用浏览器。

专用浏览器中的公司与日常Chrome互相独立；切换公司必须在所用认证浏览器中完成，再执行 session-check。每次查询固定启动时身份，响应身份改变或记录公司不符就停止；下一次命令重新读取会话。不合并多公司、不后台同步。

专用浏览器状态目录为 Windows `%LOCALAPPDATA%\jst-ai-agent\browser-profile`、Linux `${XDG_DATA_HOME:-~/.local/share}/jst-ai-agent/browser-profile`、macOS `~/Library/Application Support/jst-ai-agent/browser-profile`。不要共享、打包、提交Git或上传该目录。关闭浏览器后删除该专用目录可清除本机保留的登录状态，ERP在线会话撤销仍按聚水潭账号管理操作。

## 企业规则与报告

商品规则使用运行工具的 `config/companies/<company_id>/sku-rules.json`；没有文件保持未知。显式规则和情景文件必须声明当前company_id，不能沿用上一家公司参数。报告记录公司、用户与数据时间；同SKU编码不直接证明跨公司商品相同。

状态未知、身份冲突、失效或权限不足时停止，不把部分数据当全量。只向聚水潭目标接口发送凭证；报错不输出浏览器原始异常、Cookie或原始订单。默认指标与数据覆盖见 company-metrics.md。
