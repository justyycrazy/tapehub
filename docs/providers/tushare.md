# Tushare（tushare）

Tushare Pro 官方数据源接入：CN 个股日/周/月三复权 K 线 + 期货单一合约/主力连续日线；期货与主连不参与 auto 路由、仅显式指定可用；无快照/流式。

## 概要

- 接入方式：官方 SDK `tushare`（Tushare Pro），官方文档 <https://tushare.pro/document/2>；股票走模块级函数 `ts.pro_bar`（`asset='E'`），期货走 pro 对象方法 `pro.fut_daily`（上游函数名仅存在于适配器与 manifest 内部，不暴露到公共 API）；
- SDK：pyproject extras `tushare`（可选依赖，延迟导入；未安装返回 `PROVIDER_NOT_IMPLEMENTED`，不影响服务启动）；
- 凭据：`TAPEHUB_TUSHARE_TOKEN` 经 `ts.pro_api(token)` 注入（禁止写入代码/manifest/日志/异常响应）；未配置时不启用（能力目录仍可见 `enabled=false`），显式请求返回 `PROVIDER_DISABLED`；上游接口可用性受账户积分约束（fut_daily 要求 2000 积分）；环境变量全表见 ../operations.md；
- 变体体系：单接口体系 Provider，变体与 `default_variant` 均为 `tushare`（数据源即上游服务自身，与东方财富无关）；缺省请求即可路由，也可显式传 `variant=tushare`。

## 能力与上游映射

以 `src/tapehub/providers/tushare/manifest.yaml` 的 capabilities/operations 为准（全部 CN 市场、变体 tushare）：

| data_type | asset_type | 上游接口 | 周期 | adjust | 标准字段 |
| --- | --- | --- | --- | --- | --- |
| bars | stock | `ts.pro_bar`（asset='E'） | P1D/P1W/P1M | none/qfq/hfq | open/high/low/close/volume/amount |
| bars | contract | `pro.fut_daily` | P1D | none | open/high/low/close/volume/amount |
| bars | continuous | `pro.fut_daily` | P1D | none | open/high/low/close/volume/amount |

历史覆盖（history.start）：

- stock 日/周/月：`1999-12-08T00:00:00+08:00`（二分估计粒度 90 天 approx，三周期各自二分收敛、结果一致；真值≈标的上市日 1999-11-10 落在估算窗口内，与 akshare 个股口径同日互证）；
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

期货单一合约 ts_code 后缀（五所探针实证）：SHFE→SHF、DCE→DCE、INE→INE、CFFEX→CFX、CZCE→ZCE。示例：`CN-SHFE-CU2401 → CU2401.SHF`、`CN-DCE-V2601 → V2601.DCE`、`CN-INE-SC2409 → SC2409.INE`、`CN-CFFEX-IF2409 → IF2409.CFX`。CZCE 在 tushare 为 4 位年月码，与统一标准码同形直通（`CN-CZCE-RM2701 → RM2701.ZCE`；实证 4 位可用、3 位旧写法全空）——非 4 位年月的 CZCE 输入如实透传给上游，查空/报错如实表现，不做静默年份猜测。广期所 `.GFE` 未核实，不声明。

主力连续（continuous）映射（总览页连续合约格式 XX=主力 / XXL=主连；五所探针实证）：多数交易所映射**品种+L 主连**——`CN-SHFE-CU → CUL.SHF`、`CN-CZCE-SR → SRL.ZCE`、`CN-DCE-M → ML.DCE`、`CN-INE-SC → SCL.INE`；CFFEX 例外直拼主力——`CN-CFFEX-IF → IF.CFX`（该所连续后缀 IFL/IFL1-3 为当月/次月/当季/下季月份系列，非主连）。

拒绝范围：股票 {SSE, SZSE, BSE}、期货 {SHFE, DCE, INE, CFFEX, CZCE} 之外的 exchange 明确拒绝（`UnsupportedCapabilityError`）；绝不预填未核实条目（证伪即移除）。

## 周期 / 复权 / 单位映射

- 周期映射：pro_bar freq `P1D→D`、`P1W→W`、`P1M→M`；fut_daily 仅日线（上游接口本身无其他周期）。日期参数格式 `YYYYMMDD`。
- 复权映射：pro_bar `adj` 枚举 None/qfq/hfq——`none` 时传 Python `None`（官方默认未复权），与网关股票声明默认 qfq 不同，适配器始终显式传参；文档注解「adj 仅日线复权」偏保守，探针实证周/月线复权真实生效。fut_daily 无复权。
- 单位换算按周期分支（同窗求和互证定案，探针 2026-11-15 第 13 轮）：**日线**上游 `vol` 手、`amount` 千元 → 换算股（×100）、元（×1000）；**周/月线**上游实数据已是股/元 → 如实透传（×1）——官方文档对周月线声明「手/千元」（doc_id=95/171/172）与实数据矛盾（文档错误），同周/同月日线求和 vs 周/月线单 bar 比率精确 ×100/×1000；期货 `vol` 保持手、`amount` 万元→元（×10000）。

## 上游行为定案

- 时间戳口径：`trade_date`（YYYYMMDD）→ 该交易日 00:00 北京时间（+08:00）；标准化后按闭区间 `[start, end]` 精确复核（上游按日期过滤，网关统一时间语义）；统一分页由共享 `paginate_bars` 完成。
- fut_daily 单次上限 2000 条（主连 CUL.SHF 16 年窗口实测恰 2000）：达到上限时 `meta.partial=true` 且 warnings 提示可能截断；上游截断为「保尾截头」——保留最新 2000 根，partial 时缺的是窗口头部更早数据；该接口要求上游账户 2000 积分。pro_bar 未核实单次行数上限，不启用截断判定（partial 恒 false）。
- 空结果（DataFrame/None）统一映射为空列表，按空批次返回、不报错。
- 错误指纹：上游积分限频/错误码未在文档中核实（40203 等积分错误码待核实后补充），SDK 异常统一包装 `UPSTREAM_ERROR`，不穿透 API。

## 快照与流式

不参与实时快照与 SSE 流式（未声明 quotes 能力、未实现 stream_quote）。

## 缺省路由参与

当前回退链顺序以 `src/tapehub/config/provider_routing.yaml` 为准，本文不复述 order 列表。参与组合与角色：

- bars CN stock：参与（凭据型回退候选之一）；
- bars CN contract、bars CN continuous：能力已声明，但链上不写即明确不参与 auto 路由——仅显式 `provider=tushare` 可用。

## extensions 白名单

- stock（pro_bar）：`pre_close`（前收盘价）、`change`（涨跌额）、`pct_chg`（涨跌幅）；
- 期货（fut_daily）：`pre_close`（前收盘价）、`settle`（结算价）、`oi`（持仓量）。

均仅在存在且非 NaN 时透出（缺失不冒充）。

## 探针依据

- 能力核实与行为实证用例：`probes/cases/tushare.yaml`（探针框架规范见 `probes/README.md`）；
- 各轮运行记录：`probes/results/PROBE_LOG.md`（本地文件，不入库）。
