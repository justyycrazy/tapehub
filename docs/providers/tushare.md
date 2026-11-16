# Tushare（tushare）

Tushare Pro 官方数据源接入：CN 个股日/周/月三复权 K 线 + CN 指数日/周/月 K 线（无复权）+ CN ETF 日线（无复权）+ CN 期权合约日线（无复权，八所全覆盖）+ 期货单一合约/主力连续日线；期货与主连不参与 auto 路由、仅显式指定可用；无快照/流式。

## 概要

- 接入方式：官方 SDK `tushare`（Tushare Pro），官方文档 <https://tushare.pro/document/2>；股票与指数走模块级函数 `ts.pro_bar`（`asset='E'`/`'I'`，官方枚举 I=沪深指数，doc_id=109），期货/ETF/期权走 pro 对象方法 `pro.fut_daily`/`pro.fund_daily`/`pro.opt_daily`（官方枚举另有 FT=期货——与已声明的 fut_daily 为同一底层接口，SDK 直传定案，无需独立声明；上游函数名仅存在于适配器与 manifest 内部，不暴露到公共 API）；
- SDK：pyproject extras `tushare`（可选依赖，延迟导入；未安装返回 `PROVIDER_NOT_IMPLEMENTED`，不影响服务启动）；
- 凭据：`TAPEHUB_TUSHARE_TOKEN` 经 `ts.pro_api(token)` 注入（禁止写入代码/manifest/日志/异常响应）；未配置时不启用（能力目录仍可见 `enabled=false`），显式请求返回 `PROVIDER_DISABLED`；上游接口可用性受账户积分约束（fut_daily 要求 2000 积分）；环境变量全表见 ../operations.md；
- 变体体系：单接口体系 Provider，变体与 `default_variant` 均为 `tushare`（数据源即上游服务自身，与东方财富无关）；缺省请求即可路由，也可显式传 `variant=tushare`。

## 能力与上游映射

以 `src/tapehub/providers/tushare/manifest.yaml` 的 capabilities/operations 为准（全部 CN 市场、变体 tushare）：

| data_type | asset_type | 上游接口 | 周期 | adjust | 标准字段 |
| --- | --- | --- | --- | --- | --- |
| bars | stock | `ts.pro_bar`（asset='E'） | P1D/P1W/P1M | none/qfq/hfq | open/high/low/close/volume/amount |
| bars | index | `ts.pro_bar`（asset='I'） | P1D/P1W/P1M | none | open/high/low/close/volume/amount |
| bars | etf | `pro.fund_daily` | P1D | none | open/high/low/close/volume/amount |
| bars | 期权三子类（commodity-option/index-option/etf-option） | `pro.opt_daily` | P1D | none | open/high/low/close/volume/amount |
| bars | contract | `pro.fut_daily` | P1D | none | open/high/low/close/volume/amount |
| bars | continuous | `pro.fut_daily` | P1D | none | open/high/low/close/volume/amount |

历史覆盖（history.start）：

- stock 日/周/月：`1999-12-08T00:00:00+08:00`（二分估计粒度 90 天 approx，三周期各自二分收敛、结果一致；真值≈标的上市日 1999-11-10 落在估算窗口内，与 akshare 个股口径同日互证）；
- index 三周期：`1991-01-01T00:00:00+08:00`（实证下界声明——探针 2026-11-15 第 13 轮 P1D raw 直测 1991 全年 255 行；第 15 轮 chain 二分 P1W/P1M era 回溯同至 1991 触底；真实最早 bar ≈1991-01-18 与 baostock 结论互证，chain history_depth 常态复验）；
- etf 日线：`2005-02-23T00:00:00+08:00`（实证下界声明——510050.SH 上市日，2005 全年 213 行；官方口径 fund_daily 起始 2000-01-03 系 LOF/封闭式基金更早数据，未实证不声明；capability 为库级声明，各标的以自身上市日起）；
- 期权三子类日线：库级历史起点未核实，manifest 保持 null（合约级数据随上市起、随摘牌止）；
- contract：`2023-01-17T00:00:00+08:00`（探针标的 CU2401 上市首日，全量模式精确定值 approx=False）——capability 为 asset_type 级声明，各合约数据以自身上市日起、随摘牌止；
- continuous：`1991-01-01T00:00:00+08:00`（二分下限触底，真实可能更早；主连拼接历史深于个股）。

## 标的代码映射

股票 ts_code 后缀（SSE/SZSE 按文档示例核实，BSE 经探针实证收编）：

| exchange 段 | 后缀 | 示例 |
| --- | --- | --- |
| SSE | SH | CN-SSE-600000 → 600000.SH |
| SZSE | SZ | CN-SZSE-000001 → 000001.SZ |
| BSE | BJ | CN-BSE-920000 → 920000.BJ |

BSE 说明：文档 doc_id=14 后缀表列明 `.BJ`，且经探针双证（全市场目录在市标的全部 920 段；920000.BJ 的 daily/adj_factor 均可用）；83/87/43 段老代码已被 920 段替换、不再有效；history.start 沿用 CN stock 库级声明（BSE 标的均 2020 年后上市，各标的数据以自身上市日起）。

期货单一合约 ts_code 后缀（六所探针实证）：SHFE→SHF、DCE→DCE、INE→INE、CFFEX→CFX、CZCE→ZCE、GFEX→GFE。示例：`CN-SHFE-CU2401 → CU2401.SHF`、`CN-DCE-V2601 → V2601.DCE`、`CN-INE-SC2409 → SC2409.INE`、`CN-CFFEX-IF2409 → IF2409.CFX`、`CN-GFEX-LC2706 → LC2706.GFE`。CZCE 在 tushare 为 4 位年月码，与统一标准码同形直通（`CN-CZCE-RM2701 → RM2701.ZCE`；实证 4 位可用、3 位旧写法全空）——非 4 位年月的 CZCE 输入如实透传给上游，查空/报错如实表现，不做静默年份猜测。GFEX 后缀 2026-11-16 探针实证（LC2706.GFE 一周 5 行真实数据；fut_basic exchange=GFEX 在市 154 行，品种 LC/LCL、PD/PDL、PS/PSL、PT/PTL、SI/SIL）。

主力连续（continuous）映射（总览页连续合约格式 XX=主力 / XXL=主连；六所探针实证）：多数交易所映射**品种+L 主连**——`CN-SHFE-CU → CUL.SHF`、`CN-CZCE-SR → SRL.ZCE`、`CN-DCE-M → ML.DCE`、`CN-INE-SC → SCL.INE`、`CN-GFEX-LC → LCL.GFE`（GFEX 主连 2026-11-16 探针实证：LCL.GFE 一周 5 行 × 15 列字段齐全；在市目录中合约码与主连码并列）；CFFEX 例外直拼主力——`CN-CFFEX-IF → IF.CFX`（该所连续后缀 IFL/IFL1-3 为当月/次月/当季/下季月份系列，非主连）。

指数 ts_code 后缀（doc_id=109 官方枚举「I=沪深指数」）：与股票同表——SSE→SH、SZSE→SZ。示例：`CN-SSE-000001 → 000001.SH`（上证指数）、`CN-SZSE-399001 → 399001.SZ`（深证成指）。拒绝范围 {SSE, SZSE}：BSE 指数（如北证 50）不在官方「沪深指数」枚举内，不声明、映射层明确拒绝；中证指数（.CSI 后缀）不在网关标准代码域内，不涉及。

ETF ts_code 后缀（fund_daily 场内基金接口，doc_id=127，沪深同构探针实证）：SSE→SH、SZSE→SZ。示例：`CN-SSE-510050 → 510050.SH`（华夏上证 50ETF）、`CN-SZSE-159915 → 159915.SZ`（易方达创业板 ETF）。拒绝范围 {SSE, SZSE}。

期权 ts_code 后缀（asset_type 三分后按子类查分表，上游同为 pro.opt_daily、后缀码式零变化；opt_daily 八所；六所经 doc_id=159 交易所枚举 + 探针实证，**INE/GFEX 为 2026-11-16 聚焦轮目录+日 K 双证收编**——doc_id=159 文档枚举未列这两所，实测 `opt_basic` 目录可查，文档滞后）：合约符号**原样透传**（不做年份猜测与连字符改写）——SSE→SH（`CN-SSE-10012437 → 10012437.SH`）、SZSE→SZ（`90008083.SZ` 式）、CFFEX→CFX（`CN-CFFEX-IO2611-C-4150 → IO2611-C-4150.CFX`，带连字符中段「品种月-C/P-行权价」，**后缀 .CFX 与该所期货同**）、DCE→DCE（`M2612-C-3500.DCE` 式，4 位月带连字符）、SHFE→SHF（`FU2612C6100.SHF` 式，无连字符）、CZCE→ZCE（`SR701C5900.ZCE` 式，**3 位月码直通**，701=2027-01）、INE→INE（`CN-INE-SC2701C550 → SC2701C550.INE`，无连字符，与 SHFE 同构；目录形态样本 `SC2703P440.INE`）、GFEX→GFE（`CN-GFEX-LC2701-C-90000 → LC2701-C-90000.GFE`，连字符式；目录形态样本 `PS2710-C-30000.GFE`——**期权连字符式与该所期货主连「品种+L」不同构**）。拒绝范围：三子类各自的八所子集（commodity-option 五所 / index-option CFFEX / etf-option 沪深两所，合计八所）之外（如 BSE 无期权；子类×交易所矛盾组合在域层词汇表即拒绝，见 ../api.md）。

拒绝范围：股票 {SSE, SZSE, BSE}、指数/ETF {SSE, SZSE}、期权八所 {SSE, SZSE, CFFEX, DCE, SHFE, CZCE, INE, GFEX} 之外、期货六所 {SHFE, DCE, INE, CFFEX, CZCE, GFEX} 之外的 exchange 明确拒绝（`UnsupportedCapabilityError`）；绝不预填未核实条目（证伪即移除）。

## 周期 / 复权 / 单位映射

- 周期映射：pro_bar freq `P1D→D`、`P1W→W`、`P1M→M`（股票与指数同表）；fut_daily/fund_daily/opt_daily 仅日线（上游接口本身无其他周期——ETF/期权无周/月线接口，SDK 分支与专题导航双证）。日期参数格式 `YYYYMMDD`。
- 复权映射：pro_bar `adj` 枚举 None/qfq/hfq——`none` 时传 Python `None`（官方默认未复权），与网关股票声明默认 qfq 不同，适配器始终显式传参；文档注解「adj 仅日线复权」偏保守，探针实证周/月线复权真实生效。**指数复权不支持**（官方 adj「只针对股票」，上游 asset='I' 分支忽略 adj）、**ETF/期权无复权**（fund_daily/opt_daily 无 adj 参数；ETF 复权需上游 fund_adj 因子接口自算，不在 pro_bar 范围），均仅声明 none——网关 ETF 缺省复权为 qfq（2026-11-15 起，见 ../api.md 默认值约定决策记录），tushare ETF 缺省请求会被能力过滤，显式 `adjust=none` 可达。fut_daily 无复权。
- 单位换算按周期分支（同窗求和互证定案，探针 2026-11-15 第 13 轮，**股票与指数共用**）：**日线**上游 `vol` 手、`amount` 千元 → 换算股（×100）、元（×1000）；**周/月线**上游实数据已是股/元 → 如实透传（×1）——官方文档对周月线声明「手/千元」（doc_id=95/171/172）与实数据矛盾（文档错误），同周/同月日线求和 vs 周/月线单 bar 比率精确 ×100/×1000；期货 `vol` 保持手、`amount` 万元→元（×10000）；ETF 与股票日线同口径（doc_id=127：vol 手/amount 千元，量纲自洽实证）；期权 vol 保持手（张）、`amount` 万元→元（×10000，与期货同款口径，量纲自洽实证）。

## 上游行为定案

- 时间戳口径：`trade_date`（YYYYMMDD）→ 该交易日 00:00 北京时间（+08:00）；标准化后按闭区间 `[start, end]` 精确复核（上游按日期过滤，网关统一时间语义）；统一分页由共享 `paginate_bars` 完成。
- fut_daily 单次上限 2000 条（主连 CUL.SHF 16 年窗口实测恰 2000）：达到上限时 `meta.partial=true` 且 warnings 提示可能截断；上游截断为「保尾截头」——保留最新 2000 根，partial 时缺的是窗口头部更早数据；该接口要求上游账户 2000 积分。pro_bar 未核实单次行数上限，不启用截断判定（partial 恒 false）。
- 空结果（DataFrame/None）统一映射为空列表，按空批次返回、不报错。
- 指数（asset='I'）路由定案：SDK 薄封装直传——pro_bar asset='I' 分支 D→index_daily、W→index_weekly、M→index_monthly（tushare/pro/data_pro.py L151-159，无额外变换，源码定案）；三周期接口直测均有数（探针 2026-11-15 第 13 轮：117/26/6 行）。指数日线要求 2000 积分、数据约 18 点更新（doc_id=95）；指数周/月线要求 600 积分、单次上限 1000 行（doc_id=171/172），pro_bar 未启用截断判定（partial 恒 false），与股票周月线现状一致。
- ETF/期权（fund_daily/opt_daily）定案（探针 2026-11-15 第 14/15 轮）：fund_daily 单次上限 5000 行（partial 语义与 fut_daily 同）、要求 5000 积分（实测账户通过）、亦含 LOF（上游 issue #1891 口径）；opt_daily 单次上限 15000 行（partial 同）、要求 2000 积分（实测通过）、全部历史每日约 17 点更新；期权覆盖八所（2026-11-16 第 17 轮聚焦收编 INE/GFEX，码式与证据见「标的代码映射」）。目录接口 opt_basic 单次返回恰 12000 行，疑为上游单次上限（第 17 轮 INE/GFEX/SHFE 三次同值，待核实；不影响映射与日 K 结论）。fund_daily/opt_daily 直调 pro 对象方法，量额换算与 extensions 见「周期/复权/单位」节。
- 错误指纹：上游积分限频/错误码未在文档中核实（40203 等积分错误码待核实后补充），SDK 异常统一包装 `UPSTREAM_ERROR`，不穿透 API。

## 快照与流式

不参与实时快照与 SSE 流式（未声明 quotes 能力、未实现 stream_quote）。

## 缺省路由参与

当前回退链顺序以 `src/tapehub/config/provider_routing.yaml` 为准，本文不复述 order 列表。参与组合与角色：

- bars CN stock：参与（凭据型回退候选之一）；
- bars CN index：参与（链尾追加·凭据型兜底，2026-11-15 指数扩容；凭据缺失自动跳过）；
- bars CN etf：参与（链尾追加·凭据型兜底，2026-11-15 ETF 扩容）；
- bars CN 期权三子类：参与（ths 之后链尾追加·凭据型兜底，2026-11-15 期权扩容 + 2026-11-16 八所收编 INE/GFEX + asset_type 三分三子类同链，缺省链凭据缺失自动跳过）；
- bars CN contract、bars CN continuous：能力已声明，但链上不写即明确不参与 auto 路由——仅显式 `provider=tushare` 可用。

## extensions 白名单

- stock/index（pro_bar）：`pre_close`（前收盘价）、`change`（涨跌额）、`pct_chg`（涨跌幅）——指数行同列集（doc_id=95 实测与探针 2026-11-15 第 13 轮字段形态一致）；
- etf（fund_daily）：`pre_close`（前收盘价）、`change`（涨跌额）、`pct_chg`（涨跌幅）——与股票日线同列集（doc_id=127 与探针 2026-11-15 第 14 轮字段形态一致）；
- 期货/期权（fut_daily/opt_daily）：`pre_close`（前收盘价）、`settle`（结算价）、`oi`（持仓量）——期权行另含 pre_settle（昨结算）上游字段，白名单未收录（与期货保持对称）。

均仅在存在且非 NaN 时透出（缺失不冒充）。

## 探针依据

- 能力核实与行为实证用例：`probes/cases/tushare.yaml`（探针框架规范见 `probes/README.md`）；
- 各轮运行记录：`probes/results/PROBE_LOG.md`（本地文件，不入库）。
