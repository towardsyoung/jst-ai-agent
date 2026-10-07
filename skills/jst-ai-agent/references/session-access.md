# 登录身份、公司切换及本地安装

## 当前执行范围

只读工具从当前Chrome会话的 `u_co_id`、`u_id` 自动获取公司和用户。客户正常登录聚水潭并选择公司，无需手填ID、复制Cookie或向模型提供密码。只读公司和用户ID可显示在连接结果中；凭证不得打印、落盘或写入skill。

当前认证实现仅支持macOS Chrome钥匙串。Windows/Linux浏览器认证、一键登录引导、WorkBuddy托管安装/正式连接器尚未实现。普通skill导入不能自动完成这些工作。

## 安装与定位

项目内使用 `./query.sh`；skill内使用 `scripts/query.sh`。入口依次检查 `JST_AI_HOME/query.sh`（如显式指定）、skill的 `runtime/query.sh`（完整交付包）、项目相对位置的query.sh。符号链接安装也按真实目录定位。代码只在项目中维护，发布时附同源运行文件，不维护另一份计算实现。

完整技能包含运行代码和requirements，但不含Python或已安装依赖。在macOS上准备Python 3.12，在技能目录执行：

```sh
JST_PYTHON=/实际安装的/python3.12 sh scripts/setup.sh
scripts/query.sh session-check
```

不要猜Python绝对路径。若环境已有合适的python3，可直接运行 `sh scripts/setup.sh`。安装步骤只创建本工具虚拟环境和依赖，不访问浏览器或聚水潭。仅安装包和业务运行代码可共享；个人.venv、企业规则、案例、报告和凭证不随包分发。

WorkBuddy可导入技能包，但本版本仍需先完成上述本地运行环境安装，并在目标客户端验收脚本执行。这里没有实现WorkBuddy自动托管CLI认证。

## 查询与公司切换

1. 在目标Chrome Profile正常登录聚水潭，选择公司。
2. 初次连接或切换后执行 `scripts/query.sh session-check`，核对返回company_id、user_id和商品接口可读性。此命令不是全数据域权限检查，也不是全量商品计数。
3. 执行所需分析，回答注明公司、期间、时间和口径。浏览器切换到另一公司后重新执行命令，自动识别新身份。

默认Profile为Default。多个Profile时由客户选择目录名，例如：

```sh
scripts/query.sh session-check --chrome-profile "Profile 1"
scripts/query.sh sales-top --chrome-profile "Profile 1" --limit 10
```

不自动扫描其他账号；同一ERP身份Cookie有冲突、缺失、失效时停止，请客户在选定Profile正常重新登录。只读取erp321.com及其子域的未过期Cookie。

每次运行把启动时读取的会话和身份固定在内存；浏览器后来切换不会让这次查询自动换公司。若服务端响应改变本次会话身份，或记录公司不匹配，就停止。下一次命令重新读取浏览器会话。没有自动合并多家公司、跨运行缓存或后台同步。

## 企业规则与报告

商品规则默认定位运行工具的 `config/companies/<company_id>/sku-rules.json`。没有文件即无已确认规则；可继续基础读取，商品类型/货权等保持未知。显式 `--sku-rules` 文件不存在或company_id不匹配时报错。补货情景文件同样必须匹配当前公司，不能从上一家公司复制交期、MOQ、批次或SKU归类。

报告均记录company_id和user_id。用户明确要求跨公司比较时分别查询、标注公司与口径，不能仅凭相同SKU编码关联。默认在售/订单件数定义见company-metrics.md；企业要净销量或实发口径时另行验证。

数据域权限遵循聚水潭当前账号。商品可读不代表订单、采购、加工均可读；新企业的状态、模块和数据完整性继续按实际响应核对。行业参考只有用户提出对应工厂问题时加载。
