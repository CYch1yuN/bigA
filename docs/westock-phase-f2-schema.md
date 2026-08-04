# Westock 个股深度能力真实数据 Schema 探测报告（Phase F2）

> 状态：真实响应探测与结构报告（本轮不修改生产代码）
> 日期：2026-08-04
> 数据来源：WorkBuddy 已连接 westock-mcp（cache-export 桥）；全部为真实调用观察，非文档猜测。
> 原始响应留档：仓库外目录（不入 Git）。

## 0. 结论摘要

- 16 项能力真实调用：15 项 recognized、1 项 empty（buyback）。
- 全部 15 项首个非空样本已通过 `scripts/westock_cache_export.py` 导出并 `WestockCacheStore.read()` 回读（envelope 校验通过，fresh）。
- 3 类真实结构形态：**wrapper 单键**（minute/technical/financials/shareholders/margin/block_trade/chip_distribution/risk）、**记录级身份**（forecast/dividend/northbound/reports/announcements/events/lhb）、**双层包装**（financials）。
- 身份字段统一为 `code`（sh600519 前缀格式）或 `symbol`；northbound 附带 `schema` 单位说明字典（**上游声明，尚未独立验证**）。
- 现有标准化器仅能识别 quote/profile/news/fund_flow/filter：
  - **15 项 recognized 能力需要按真实结构校准**（mapping gap 明细见 §5，非「仅 11 项」——除已识别 5 项外的全部能力都需要校准）；
  - **buyback 为 supported-but-empty**：工具存在、可调用，但真实样本无 data 字段；现有 `_cap` 对无 data envelope 的 unavailable 降级行为**基本正确**，无需视为缺失能力。

## 1. 16 项能力探测矩阵

| # | capability | MCP tool | 样本股票 | 结果 | 顶层类型 | 结构形态 |
|---|---|---|---|---|---|---|
| 1 | minute | data_minute | 600519.SH | recognized | dict | wrapper 单键 + 字符串数组 |
| 2 | technical | data_technical | 600519.SH | recognized | dict | wrapper 单键 + 指标分组 |
| 3 | financials | data_finance | 600519.SH | recognized | dict | 双层包装 + 三类报表列表 |
| 4 | forecast | data_consensus | 600519.SH | recognized | dict | 记录级身份 + forecasts 列表 |
| 5 | shareholders | data_shareholder | 600519.SH | recognized | dict | wrapper 单键 + 两类股东列表 |
| 6 | dividend | data_dividend | 000001.SZ（600519 空） | recognized | dict | 记录级身份 + plans 列表 |
| 7 | buyback | data_buyback | 三只均空 | **empty** | dict | 仅 `{"ok":true}` 无 data |
| 8 | margin | data_fund_margin | 600519.SH | recognized | dict | wrapper 单键 + 单记录 |
| 9 | block_trade | data_fund_block | 000001.SZ（600519 限频） | recognized | dict | wrapper 单键 + blockTradingInfos |
| 10 | northbound | data_north_holding | 600519.SH | recognized | dict | 记录级身份 + cur/prev 季度块 |
| 11 | lhb | data_lhb | 全市场（无 code 参数） | recognized | dict | 多股票 5 分类列表 |
| 12 | chip_distribution | data_chip | 600519.SH | recognized | dict | wrapper 单键 + 单记录 |
| 13 | reports | data_report | 600519.SH | recognized | dict | 记录级身份 + 分页 data 列表 |
| 14 | announcements | data_notice | 600519.SH | recognized | dict | 记录级身份 + 分页 data 列表 |
| 15 | events | data_events | 600519.SH | recognized | dict | stocks 列表 + tagIds |
| 16 | risk | data_risk | 300750.SZ（600519/000001 限频） | recognized | dict | wrapper 单键 + 6 分类列表 |

状态统计：**recognized=15，empty=1，unsupported=0，error=0**（600519/000001 的 block_trade/risk 曾遇上游「服务限频」错误，换标的重试后成功，属暂时性上游限频而非能力不可用）。

## 2. 每项脱敏字段结构（wrapper / 身份 / 列表 / 单位）

### 2.1 minute（data_minute）
- 包装：`data.<外层键>`（外层键 = 请求股票 sh600519 前缀格式，可作身份）。
- 分时在 `data.<外层键>.data.data`：**字符串数组**，每行空格分隔 `HHMM 价格 成交量 成交额`（例：`0930 1350.06 235 31726410.00`）；`data.<外层键>.data.date` 为 `YYYYMMDD`。
- 单位：价格为元；成交量为手；成交额为元。
- 附带 `qt` 行情快照数组（位置编码，字段密集，不建议直接标准化）与 `mx_price` 空壳。
- 记录级身份：无（仅外层键可绑定）。

### 2.2 technical（data_technical）
- 包装：`data.<外层键>`；记录内 `code`（前缀格式）可二次绑定。
- 字段：`name`、`date`（YYYY-MM-DD）、`closePrice`（数值）。
- 指标分组：`ma`（MA_5…MA_250、EMA_12/26/50）、`macd`（DIF/DEA/MACD）、`kdj`（KDJ_K/D/J）、`rsi`（RSI_2/6/12/24）、`boll`（BOLL_UPPER/MID/LOWER）、`bias`、`wr`、`dmi`、`other`（OBV/VR/BBI/TRIX/DPO/PSY/ENE/CCI/AR/BR 等）。
- 全部为数值标量；无列表、无分页。

### 2.3 financials（data_finance）
- **双层包装**：`data.code`/`data.msg`（响应状态，code=0 成功）→ `data.data.<外层键>` → `balance` / `cashflow` / `income` 三类列表。
- 身份：外层键（前缀格式）+ 行内 `SecuCode`（前缀格式，真实值为 `sh600519`）。
- 行字段：`EndDate`/`_date`/`date`（YYYY-MM-DD）、`InfoPublDate`（**datetime，带时区**）、`EnterpriseType`、以及 PascalCase 财务科目（`OperatingRevenue`/`TotalProfit`/`BasicEPS`/`CashEquivalents` 等，字符串数值）。
- 单位：金额单位区分来源（见 §4.1）；EPS 为元/股；`*_Q`/`*_TTM` 后缀表单季/TTM（单位未确认，见 §4.1）。
- **定点探测（num=3，600519.SH）**：三表各 **3 行**；按 `EndDate` **倒序**（2026-03-31 → 2025-12-31 → 2025-09-30）；三表报告期完全对齐、**无重复期**；`proposalSn` 类序号无（financials 无）；年报行 `*_TTM` == 累计值（TTM=全年），一季报行 `*_Q` == 累计值（单季=累计，仅观察未确认）。

### 2.4 forecast（data_consensus）
- 无 wrapper；顶层 `data.code`（前缀格式，身份）+ `name` + `targetPrice`（数值）。
- `forecasts` 列表（真实样本 3 项，年度顺序无序：2026/2028/2027）：`year`（int）、`eps`/`revenue`/`netProfit`/`pe`/`pb`/`ps`/`revenueYoy`/`netProfitYoy`（数值）、`institutionCnt`（int）。

### 2.5 shareholders（data_shareholder）
- 包装：`data.<外层键>`；记录内 `code` + `name` + `date`（YYYY-MM-DD）。
- 列表：`top10Shareholders` 与 `top10FloatShareholders`（各 10 行）：`no`（排名）、`name`（股东名，长文本）、`holdShares`（持股数，股）、`holdPct`（持股比例 %）、`holdChange`（变动，股）。

### 2.6 dividend（data_dividend）
- 无 wrapper；顶层 `data.code`（身份）+ `start`/`end`（查询回显）+ `plans` 列表。
- 行字段：`cashDiviRMB`（每 10 股派现，元）、`totalCashDiviComRMB`（总派现，元）、`bonusShareRatio`/`tranAddShareRatio`（送转比）、`exDiviDate`/`rightRegDate`/`reportEndDate`（**YYYYMMDD** 格式）、`dividendPlan`（如「10派3.600元」）、`dividendFlag`/`dividendType`/`procedure`、`proposalSn`（int）。
- **定点探测（all=true，000001.SZ）**：返回与 all=false **完全相同**（2 条 plans）；本次 1 年窗口内**未观察到未实施/预案记录**。实际枚举值：`procedure="方案实施"`、`dividendFlag="是"`、`dividendType="有分红"`、`proposalSn=1`。**不凭字段名称推断枚举的业务含义**（如 `dividendType="有分红"` 仅记录字面值，不判定为确定业务状态）；all=true 是否会返回预案需更多样本验证。

### 2.7 buyback（data_buyback）
- 三只样本（600519/000001/300750）均返回 `{"ok":true}`，**无 data 字段** → empty。

### 2.8 margin（data_fund_margin）
- 包装：`data.<外层键>`；记录内 `code` + `name` + `date`（YYYY-MM-DD）+ `closePrice`/`changePct`（数值）。
- 字段（字符串数值，单位元/%）：`FinanceValue`（融资余额）、`FinanceBuyValue`（融资买入额）、`FinanceRefundValue`、`FinanceValueDOD`（%）、`SecurityValue`（融券余额）、`SecurityValueDOD`（%）、`TradingValue`、`TradingValueDif`。

### 2.9 block_trade（data_fund_block）
- 包装：`data.<外层键>`；记录内 `code` + `name` + `date` + `closePrice`/`changePct`。
- 列表 `blockTradingInfos`：`SerialNumber`、`TradingType`（协议交易等）、`TurnoverPrice`（成交价，元）、`TurnoverValue`（成交额，元）、`CloseDiscountRate`（折价率 %）、`BuySalesDepartment`/`SellSalesDepartment`（买卖营业部，长文本）。

### 2.10 northbound（data_north_holding）
- 无 wrapper；顶层 `data.code`（身份）+ `cur`/`prev` 两个季度块。
- 每块：`date`、`info`（Type/Code/Desc）、`listName`、**`schema`（字段单位说明字典，上游声明）**、`stock` 记录。
- `stock`：`EndDate`（**int YYYYMMDD**）、`HoldingCap`、`HoldingRatio`、`HoldingShares`、`CapChgQ/Y`、`SharesChgQ/Y`、`code`/`name`。
- **定点探测（schema 与实际字段逐项比对，cur/prev 一致）**：
  - schema 10 键 vs stock 10 键；**schema 有而 stock 无**：`StockCode`/`StockName`（schema 用 CamelCase 声明键，实际记录用 `code`/`name`，语义对应但键名不一致）；
  - **stock 有而 schema 无**：`code`/`name`（额外）；
  - 共有 8 个业务字段，schema 单位声明：`HoldingCap`/`CapChgQ`/`CapChgY` 元、`HoldingShares`/`SharesChgQ`/`SharesChgY` 股、`HoldingRatio` %、`EndDate` 披露截止日期；**无单位冲突**；
  - schema 为**上游声明，尚未独立验证**；标准化时仅参考标注，不做数值换算。

### 2.11 lhb（data_lhb）
- 无 code 参数；`data.date` + 5 个分类列表：`jg`（机构，54 行）、`yzb`（游资榜，6 行）、`yyb`（营业部，115 行）、`gslmr`（个股净买入，30 行）、`gslxw`（席位，32 行）。
- 身份：**行内 `code`**（前缀格式）。`yyb.code` 为**分号分隔多股票**（`sz002384;sz300209;sh603459`）；`yzb` 与 `gslxw` 无顶层 code，身份在嵌套 `buyStock`/`sellStock`/`stockList`（`{code,name}`）；`gslmr`/`jg` 顶层单 code。
- 金额单位元；`*Rate` 为 %；`tdDays`（jg 为 int，gslmr 为 str）。
- **缓存与读取语义（明确）**：
  - 缓存 scope 固定 `global`（非个股 scope）；
  - 个股 API 读取 global 缓存后按行过滤，**不复制成个股缓存**；
  - `yyb.code` 分号拆分后逐段完整比较市场代码（`sh600519` 匹配 `600519.SH`，`600519`/`sz600519` 不匹配）；
  - 同时检查嵌套 `stockList`/`buyStock`/`sellStock` 中的 `code`；
  - **无法证明成员关系的记录丢弃**（如 `yyb` 中 code 为空字符串的行）；
  - 过滤后无匹配行 → unavailable。
- **当日样本身份形态统计（真实观察）**：jg 54 行全部单 code；yzb 6 行全部嵌套身份；yyb 115 行 = 单 code 50 + 分号多 code 21 + **无身份 44**（code 为空串）；gslmr 30 行全部单 code；gslxw 32 行全部嵌套身份。合计 237 行。
- **三只样本过滤结果（真实观察）**：600519.SH / 000001.SZ / 300750.SZ 当日 LHB 匹配数均为 0（当日未上榜）；过滤后为空 → unavailable 属正常降级路径。
- 列表上限（见 §6）。

### 2.12 chip_distribution（data_chip）
- 包装：`data.<外层键>`；记录内 `code` + `name` + `date` + `closePrice`。
- 字段：`chipProfitRate`（获利比例 %）、`chipAvgCost`（平均成本，元）、`chipConcentration90`/`chipConcentration70`（集中度 %）。

### 2.13 reports（data_report）
- 分页结构：`data.total_num` / `data.total_page` / `data.data` 列表。
- 行字段：`id`（res 前缀）、`title`（券商+股票+标题长文本）、`time`（`YYYY-MM-DD HH:MM:SS`）、`type`（str）、`symbol`（前缀格式，身份）、`symbols`（数组）、`tzpj`（评级：买入/增持/中性等）。

### 2.14 announcements（data_notice）
- 分页结构同 reports：`total_num`/`total_page`/`data`。
- 行字段：`id`（nos 前缀）、`symbol`（身份）、`title`（长文本）、`time`/`update_time`（`YYYY-MM-DD HH:MM:SS`）、`type`（str）、`url`（**存在但真实样本为空字符串**）、`newstype`（逗号分隔类型码）、`Ftranslate`。

### 2.15 events（data_events）
- `data.date` + `data.stocks` 列表：`code`（身份）、`name`、`tagIds`（事件类型 ID 列表，int）、`tagDescs`（事件描述列表，短文本）。

### 2.16 risk（data_risk）
- 包装：`data.<外层键>`；记录内 `code` + `name` + `date`。
- 6 个分类列表：`bondRating`/`executiveTransfer`/`lawsuit`/`seasonedIssue`/`unlock`（真实样本为空数组）/`leaderChange`（行：`leaderName`/`leaderPosition`/`leaderChangeReason`/`leaderStartDate`）。
- `pledge` 对象：`pledgeNum`（笔数）、`pledgeRatio`（%）、`totalPledge`/`floatPledgedVolume`/`nonFloatPledgedVolume`（万股）、`date`（空字符串）。

## 3. 身份字段来源与绑定矩阵

| capability | 外层键（前缀格式） | 记录内 code | symbol | SecuCode | 多股票响应 | 记录级绑定可行性 |
|---|---|---|---|---|---|---|
| minute | ✔（仅此） | ✘ | ✘ | ✘ | 否 | 仅外层键 |
| technical | ✔ | ✔ | ✘ | ✘ | 否 | 外层键 + code 双重校验 |
| financials | ✔ | ✘ | ✘ | ✔（sh600519 前缀） | 否 | 外层键 + 行 SecuCode |
| forecast | ✘ | ✔（顶层 code） | ✘ | ✘ | 否 | 顶层 code |
| shareholders | ✔ | ✔ | ✘ | ✘ | 否 | 外层键 + code |
| dividend | ✘ | ✔（顶层 code） | ✘ | ✘ | 否 | 顶层 code |
| buyback | — | — | — | — | — | empty |
| margin | ✔ | ✔ | ✘ | ✘ | 否 | 外层键 + code |
| block_trade | ✔ | ✔ | ✘ | ✘ | 否 | 外层键 + code |
| northbound | ✘ | ✔（顶层 + stock.code） | ✘ | ✘ | 否 | 顶层 code + stock.code |
| lhb | ✘ | ✔（行内 code，yyb 为分号多股） | ✘ | ✘ | **是（全市场）** | 行 code 过滤（见 §2.11 LHB 语义） |
| chip_distribution | ✔ | ✔ | ✘ | ✘ | 否 | 外层键 + code |
| reports | ✘ | ✘ | ✔（行内 symbol） | ✘ | 否 | 行 symbol |
| announcements | ✘ | ✘ | ✔（行内 symbol） | ✘ | 否 | 行 symbol |
| events | ✘ | ✔（行内 code） | ✘ | ✘ | 否（单股请求） | 行 code |
| risk | ✔ | ✔ | ✘ | ✘ | 否 | 外层键 + code |

建议规则（本轮仅建议，不实现）：
- 外层键可解析（sh/sz/bj 前缀）且值非空时，必须先与请求 scope 完整一致，冲突 → 整项 unavailable（固定脱敏 warning）。
- 记录内 code/symbol 存在时同规则；冲突 → 单条丢弃或整项 unavailable（按列表语义，建议单条丢弃 + 计数 warning）。
- 市场前缀必须完整比较（sh600519 ≠ 600519.SZ）；纯六位仅适用于 SecuCode 类字段与数字部分比较。
- lhb 为多股票响应：按行 code 过滤（详见 §2.11），无匹配行 → unavailable。
- warning 一律固定脱敏文案，不回显原始值（沿用 Phase F1 规则）。

## 4. 日期 / 数值 / 单位矩阵

| capability | 日期字段与格式 | 数值字段 | 金额单位 | 量/比例单位 |
|---|---|---|---|---|
| minute | data.date `YYYYMMDD`；行内 HHMM | 价格/成交量/成交额（字符串行） | 元 | 手 |
| technical | date `YYYY-MM-DD` | closePrice + 全指标 | 元 | 各指标自有单位（不统一标 %；如 MA/BOLL/SAR 为价格、OBV 为量、VR/CCI 等为指数值，仅 % 后缀指标为百分比） |
| financials | EndDate/date `YYYY-MM-DD`；InfoPublDate 为 datetime（保留完整时间） | 全部科目（字符串） | 金额单位区分来源（见 §4.1） | EPS 元 |
| forecast | 无日期（year int） | targetPrice/eps/revenue/netProfit/pe/pb/ps/Yoy | 元（revenue/netProfit，字段语义推断） | — |
| shareholders | date `YYYY-MM-DD` | holdShares/holdChange | — | 股 / % |
| dividend | exDiviDate 等 `YYYYMMDD` | cashDiviRMB/totalCashDiviComRMB | 元（每10股/总额，字段语义推断） | 元 |
| margin | date `YYYY-MM-DD` | Finance*/Security*/Trading*（字符串） | 元 | % |
| block_trade | date `YYYY-MM-DD` | TurnoverPrice/TurnoverValue | 元 | % |
| northbound | stock.EndDate **int `YYYYMMDD`**；块 date `YYYY-MM-DD` | HoldingCap/CapChg*/SharesChg* | 元（**上游 schema 声明，尚未独立验证**） | 股 / %（同上） |
| lhb | data.date `YYYY-MM-DD` | *Amt / netAmt / bAmt / sAmt | 元 | % |
| chip_distribution | date `YYYY-MM-DD` | chipAvgCost | 元 | % |
| reports | time `YYYY-MM-DD HH:MM:SS`（datetime，不得截断成日期） | 无 | — | — |
| announcements | time/update_time 同上（datetime，不得截断） | 无 | — | — |
| events | data.date `YYYY-MM-DD` | tagIds | — | — |
| risk | leaderStartDate `YYYY-MM-DD`；pledge.date 空串 | pledgeNum/totalPledge/pledgeRatio | — | 万股 / % |

### 4.1 日期与金额单位来源声明

**日期规则拆分（统一输出，不做跨格式推断）**：
- `date`（仅日期）：接受 `YYYY-MM-DD` / `YYYYMMDD` / int `YYYYMMDD` → 统一输出 `YYYY-MM-DD`；
- `datetime`（完整时间）：接受 `YYYY-MM-DD HH:MM:SS`（reports.time、announcements.time/update_time）与带时区 ISO（financials.InfoPublDate，如 `2026-04-25 00:00:00 +0800 CST`）→ **保留完整时间**，不得截断成日期；
- 无法识别 → 字段丢弃（不整项降级，除非日期为核心字段）。

**financials 金额单位来源分级**（禁止基于推断自动换算）：
- **源声明**：无（上游 financials 响应未自带单位字段）；
- **文档声明**：无（未发现 Westock 文档单位声明）；
- **字段语义推断**（标注为推断）：`CashEquivalents`/`TotalAssets`/`OperatingRevenue`/`TotalProfit` 等科目按惯例为元（中国 A 股报表元单位）；`BasicEPS`/`DilutedEPS` 为元/股；
- **未确认**：`*_Q`（单季）与 `*_TTM`（滚动）字段是否与累计值同单位——真实观察：年报行 `NPParentCompanyOwnersTTM` == `NPParentCompanyOwners`（全年 TTM=年报累计），一季报行 `NPParentCompanyOwners_Q` == 累计值（一季报单季=累计），**仅观察未确认**。

**northbound schema 标记**：`schema` 字典为单位说明（元/股/%），为**上游声明，尚未独立验证**；标准化时仅作参考标注，不据此做换算，数值原样输出。

## 5. Mapping Gaps（现有 API 无法识别的真实结构）

> 本轮不修代码，仅记录。现有标准化器已识别：quote / profile / news / fund_flow / filter。

| capability | 现有标准化器 | 真实结构差异（mapping gap） |
|---|---|---|
| minute | `normalize_minute` 期望 `{minutes:[{time,price,volume}]}` 或行列表 | 真实为 `wrapper.data.data` **字符串数组**（空格分隔 HHMM 价格 量 额）+ qt 快照壳；需新解析器 |
| technical | 无 | 指标分组嵌套 dict；需定义指标白名单（ma/macd/kdj/rsi/boll 等） |
| financials | `_norm_financials_summary` 期望扁平或指定字段 | **双层包装** `data.code/msg/data.<key>`；行 PascalCase 科目 + SecuCode 身份；需白名单 + 行上限 |
| forecast | 无 | forecasts 列表年度无序；行字段明确 |
| shareholders | `_norm_shareholder_rows`（期望 shareholders/holder/rows） | 真实键 `top10Shareholders`/`top10FloatShareholders` |
| dividend | 无 | plans 列表 + YYYYMMDD 日期 |
| buyback | 无 | **supported-but-empty**：工具存在、可调用；真实样本无 data 字段；现有 `_cap` 对无 data envelope 的 unavailable 降级行为基本正确 |
| margin | 无 | 单记录对象（非列表）；字符串数值 |
| block_trade | `_norm_item_list`（block_trade 键） | 真实键 `blockTradingInfos`；字段 PascalCase |
| northbound | 无 | cur/prev 块 + schema 说明 + EndDate int |
| lhb | `_norm_item_list`（lhb 键） | 真实为 5 个分类列表（jg/yzb/yyb/gslmr/gslxw）；yyb.code 分号多股；44 行无身份 |
| chip_distribution | `_norm_chip`（期望 points/rows 列表） | 真实为单记录标量对象（chipProfitRate 等），非点序列 |
| reports | `_norm_intel_items`（reports 键） | 真实分页 `total_num/total_page/data`；行含 `symbol`/`symbols`/`tzpj`/`title` |
| announcements | `_norm_intel_items`（announcements 键） | 真实分页结构；行含 `url`（空串）/`newstype`/`update_time` |
| events | `_norm_event_items`（期望 events/items 列表） | 真实为 `stocks` 列表 + `tagIds`/`tagDescs` |
| risk | `_norm_risk_items`（期望 risk 列表/items） | 真实为 6 个分类列表 + pledge 对象；多数为空数组 |

## 6. 推荐标准化规则（本轮建议，不实现）

1. **统一解包**：wrapper 判定复用 Phase F1 `westock_code_to_symbol`（外层唯一键可解析且值为 dict）；financials 需额外剥一层 `data.data`（内层 `code`/`msg` 为响应状态，非业务字段）。
2. **身份绑定**：外层键/记录 code/symbol/SecuCode 存在即校验；冲突 → 单条丢弃或整项 unavailable（固定脱敏 warning）；lhb 按行 code 过滤（见 §2.11）。
3. **字段白名单**：每能力显式字段白名单（§2 所列），未知字段丢弃；无受控字段 → unavailable。
4. **列表上限**：financials 每表 12 期、forecast 30、shareholders 20、dividend 100、block_trade 100、reports/announcements 200、risk 每类 100、technical 单指标组 250；**LHB 过滤后总计 200**；**LHB 原始扫描每类最多 1000，超限裁剪 + warning**。
5. **文本限制**：title/name/股东名等 ≤200（标题）/≤400（文本）；营业部名 ≤200。
6. **日期规则拆分**（见 §4.1）：`date` 接受 `YYYY-MM-DD`/`YYYYMMDD`/int `YYYYMMDD` → 统一输出 `YYYY-MM-DD`；`datetime`（`YYYY-MM-DD HH:MM:SS`、带时区 ISO）→ **保留完整时间，不截断**；不可解析 → 字段丢弃。
7. **数值**：字符串/数值统一 `_as_finite_float`；NaN/Infinity 丢弃。
8. **分页**：reports/announcements 用 `total_num`/`total_page` 反映分页信息；data 列表按上限裁剪。
9. **单位**：保持源数据单位（元/股/手/%），**禁止基于推断自动换算**；字段名白名单内标注单位来源（源声明/文档声明/字段语义推断/未确认）；northbound 用上游 schema 仅作参考标注（未独立验证）。
10. **empty 形态**：`{"ok":true}` 无 data、空数组、空对象 → unavailable + 受控 warning。

## 7. 推荐测试矩阵（本轮建议）

| 场景 | 断言 |
|---|---|
| wrapper 外层键匹配 / 错配 / 不可解析 | 识别 / 整项 unavailable / 结构降级 |
| 记录内 code/symbol/SecuCode 匹配 / 错配 / 非法 | 保留 / 单条丢弃 / 整项 unavailable |
| 双层包装（financials）剥层 | data.data 定位正确 |
| 多股票响应（lhb）行过滤 | 仅目标股票行保留；无匹配 → unavailable |
| 分页结构（reports/announcements） | total_num/total_page 透传 + data 裁剪 |
| 空形态（buyback 无 data / risk 空列表） | unavailable，不 500 |
| date 三格式（YYYY-MM-DD / YYYYMMDD / int YYYYMMDD） | 统一输出 YYYY-MM-DD |
| 带时区 datetime（financials.InfoPublDate 类） | 输出规范 ISO 8601，**保留时区**，不截断 |
| 无时区资讯 datetime（reports.time / announcements.time/update_time 类） | **保留来源时间**（YYYY-MM-DD HH:MM:SS），不附加 UTC，不截断成日期 |
| datetime 截断防护 | 断言输出含完整时分秒，禁止降级为日期 |
| 字段白名单 | 未知字段丢弃；无受控字段 → unavailable |
| 列表超上限 | 裁剪 + warning |
| 长文本 / 超长数值 | 裁剪或丢弃，不 500 |
| warning 脱敏 | 注入 token/路径/URL 不进入响应 |
| 限频错误（上游服务限频） | 记录 error，不伪造成功，不写缓存 |

## 8. 文档声明 vs 真实观察 vs 推断

- **文档声明**：MCP tool schema 参数（`code`/`symbol`/`codes` 等）、CAPABILITY_MAP 的 tool 名与 TTL（来自 westock_bridge.py）。
- **真实观察**：§1–§5 全部字段结构、身份字段来源、日期/单位、empty 形态、限频错误 —— 均为本轮真实调用所得。
- **定点探测已验证**：financials `num=3` → 三表各 3 行、EndDate 倒序、报告期无重复；dividend `all=true` 与默认相同（1 年窗口无预案记录，枚举字面值 3 个）；northbound schema 10 键 vs stock 10 键（StockCode/StockName 键名不一致、8 个业务字段单位声明一致）；LHB 当日样本身份形态统计（jg 54 单 code / yzb 6 嵌套 / yyb 50+21+44 / gslmr 30 单 code / gslxw 32 嵌套，合计 237 行）与三股 0 匹配。
- **推断（尚未验证）**：lhb `yyb.code` 分号多股是否为稳定格式（需更多交易日样本）；risk `pledge.date` 空串在无质押时是否恒为空；shareholders 两列表行数是否恒 10；financials 报表行数是否恒等于 num（且年报/一季报 `*_TTM`/`*_Q` 与累计值的等值关系）；dividend `all=true` 在存在预案的窗口是否返回未实施记录；financials 金额单位（元）为字段语义推断、`*_Q`/`*_TTM` 单位未确认。以上需更多标的/日期样本复核。

## 9. 安全边界

- 原始响应仅存仓库外；本文件不含完整响应、不含真实长文本内容、不含 token/URL/路径/凭据/错误堆栈。
- 缓存导出写入 `state/dashboard/westock/`（被 `.gitignore` 的 `state/` 忽略，不入 Git）。
