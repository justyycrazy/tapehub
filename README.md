# tapehub

通用行情数据网关：对外提供统一的 HTTP RESTful API，内部通过适配器接入多个行情数据源（akshare、tushare、tickflow、alphafeed、tqsdk）。

**第一版只实现「历史 K 线查询」这一条业务链路，不建设行情数据存储平台。**

```text
统一请求参数
→ 参数校验
→ Provider 能力匹配与路由
→ Provider 内部操作路由
→ 转换为 Provider 专用参数
→ 调用 Provider
→ 转换为统一数据模型
→ 排序与分页
→ 返回统一响应
```

## 第一版边界

已实现：

- `GET /health`：健康检查（供其他项目配置 `base_url` 后测试连接）；
- `GET /v1/capabilities`：全部 Provider 能力目录；
- `GET /v1/providers/{provider}/capabilities`：单个 Provider 能力；
- `GET /v1/market/bars`：统一历史 K 线查询；
- Provider 注册、能力匹配与公共路由；Provider 内部二级操作路由的扩展机制；
- 统一错误处理；
- **akshare Provider 已接入**（接口签名按官方文档核实，见下文）；
- **TickFlow Provider 已接入**（REST API + 官方 Python SDK，API Key 从 `.env` 读取）；
- **AlphaFeed Provider 已接入**（REST API + 官方 Python SDK，API Key 从 `.env` 读取）；
- **Tushare Provider 已接入**（官方 Python SDK `pro_bar`/`fut_daily`，Token 从 `.env` 读取）；
- **TqSdk Provider 已接入**（官方 Python SDK `get_kline_serial`，快期账户凭据从 `.env` 读取）；
- **Twelve Data Provider 已接入**（外汇与加密货币，官方 Python 库 `twelvedata`，API Key 从 `.env` 读取）；
- 基于 Fake Provider 与 Mock SDK 的完整链路验证与单元/契约测试。

明确不实现（当前版本）：

- 数据库、Redis、行情缓存、CSV/Parquet 等行情落盘
  （实时链路的**进程内最新快照**除外——那是订阅状态，不是持久化）；
- 定时任务、全量转储 Worker、消息队列、双向 WebSocket；
- 逐笔、标的元数据等其它查询接口（tqsdk 实时快照与 SSE 流式订阅
  属本期交付范围，限 `TAPEHUB_DEPLOYMENT_MODE=single` 模式）；
- 多进程/分布式部署（`TAPEHUB_DEPLOYMENT_MODE` 为守门字段，当前仅支持
  `single`，其他值启动即失败；未来以组件级配置形式提供，不预设）；
- Docker 与前端页面。

## 快速开始

使用 [uv](https://docs.astral.sh/uv/) 管理项目，Python 3.12。

```bash
# 安装 uv（任选其一）
# pipx install uv
# powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

uv sync --dev                    # 同步依赖（自动准备 Python 3.12）
uv sync --dev --extra akshare    # 安装 akshare SDK（可选依赖）
uv sync --dev --extra tickflow   # 安装 TickFlow 官方 SDK（可选依赖）
uv sync --dev --extra alphafeed  # 安装 AlphaFeed 官方 SDK（可选依赖）
uv sync --dev --extra tushare    # 安装 Tushare 官方 SDK（可选依赖）
uv sync --dev --extra tqsdk      # 安装 TqSdk 官方 SDK（可选依赖）
uv sync --dev --extra twelvedata # 安装 Twelve Data 官方库（可选依赖）

uv run pytest          # 运行测试（不依赖任何真实 SDK 与网络）
uv run ruff check .    # 静态检查
uv run uvicorn tapehub.api.app:app --reload   # 启动服务（默认 127.0.0.1:8000）
```

Provider SDK 均为**可选依赖**：不安装时服务正常启动，对应请求返回 `PROVIDER_NOT_IMPLEMENTED`（TickFlow/AlphaFeed 未配置 API Key、Tushare 未配置 Token、TqSdk 未配置快期账户时同样如此，能力目录不受影响）。

## 连接测试（base_url + /health）

```http
GET http://tapehub:8000/health
```

正常返回 HTTP 200：

```json
{
  "status": "ok",
  "service": "tapehub",
  "version": "0.1.0",
  "api_version": "v1",
  "timestamp": "2026-01-01T00:00:00Z"
}
```

调用方可同时校验 **HTTP 200** 与 **`service == "tapehub"`**。该接口不访问任何上游 Provider，也不需要鉴权。

## API 示例

### 能力目录

```http
GET /v1/capabilities?asset_type=stock&interval=P1D
GET /v1/providers/akshare/capabilities
```

只读取本地能力目录，不实时访问上游；支持可选过滤参数 `provider`、`data_type`、`market`、`asset_type`、`interval`、`variant`；每个 Provider 条目携带 `default_variant`（「Provider × 资产类型」级缺省变体，variant 缺省时的解析依据）。`enabled`（是否启用）与 `runtime_status`（运行状态，第一版恒为 `unknown`）是两个不同概念。

### 历史 K 线

```http
GET /v1/market/bars?provider=akshare&asset_type=stock&instrument=CN-SSE-600000&interval=P1D&start=2026-01-01&end=2026-01-31&offset=0&limit=100
```

参数：`provider`（默认 `auto`）、`asset_type`（必填）、`instrument`（必填）、`interval`（必填）、`start`/`end`（必填）、`adjust`（可选）、`variant`（可选，缺省按 Provider 声明的 `default_variant` 解析，`meta.variant` 回显实际值）、`offset`（默认 0）、`limit`（默认 500）。

成功响应（节选）：

```json
{
  "data": {
    "instrument": "CN-SSE-600000",
    "asset_type": "stock",
    "interval": "P1D",
    "bars": [
      {
        "timestamp": "2026-01-05T00:00:00Z",
        "open": 10.0, "high": 10.1, "low": 9.9, "close": 10.05,
        "volume": 1000000.0, "amount": 10050000.0,
        "extensions": {"pct_change": 0.5, "turnover_rate": 0.8}
      }
    ]
  },
  "meta": {
    "request_id": "req_...",
    "provider": "akshare",
    "source_time": null,
    "received_at": "2026-01-01T00:00:00Z",
    "offset": 0, "limit": 100, "count": 20, "partial": false
  },
  "warnings": []
}
```

缺失数值以 `null` 返回，不用 `0` 冒充；Provider 特有字段只能通过 `extensions` 受控透出（如 akshare 的 `pct_change`、`turnover_rate`、`hold`、`settle`）。

## 请求规范

### instrument 标准代码

格式为 `{market}-{exchange}-{symbol}`，全部大写，例如 `CN-SSE-600000`。

- `market`、`exchange` 由服务端解析，调用方无需重复传递；
- **不接受** `CN.SSE.600000`、`600000` 等非标准格式；Provider 专用代码（如 akshare 的 `sh600000`、`RB0`）由适配器转换；
- 第一版只校验标准格式，不通过标的元数据验证真实资产类型。

### asset_type 资产类型体系

`asset_type` 是**必填**字段，不做默认推断，也不根据代码格式猜测。取值按以下分类体系（category → asset_type）：

| 分类 | asset_type | 说明 |
| --- | --- | --- |
| securities 证券 | `stock` | 个股 |
| securities 证券 | `index` | 指数 |
| securities 证券 | `etf` | ETF |
| futures 期货 | `contract` | 单一合约 |
| futures 期货 | `continuous` | 主力连续 |
| futures 期货 | `weighted-index` | 加权 |
| options 期权 | `option` | 期权 |
| forex 外汇 | `forex` | 外汇 |
| crypto 加密货币 | `crypto` | 加密货币 |

缺失或未知取值返回统一参数错误 `INVALID_REQUEST`。

### variant 数据源变体

同一 Provider 下同一资产类型往往有多个上游接口（如 akshare 个股有东方财富/新浪/腾讯三个源）。统一请求通过可选参数 `variant` 指定，取值为**数据源名**而非上游函数名：

| variant | 含义 |
| --- | --- |
| `eastmoney` | 东方财富（仅 akshare 声明） |
| `sina` | 新浪（仅 akshare 声明） |
| `tencent` | 腾讯（仅 akshare 声明） |
| `tqsdk` | TqSdk（快期 Shinny Tech 官方 SDK 包名，背后为天勤行情平台） |
| `tushare` | Tushare（独立数据服务体系） |
| `alphafeed` | AlphaFeed（独立数据服务体系） |
| `tickflow` | TickFlow（独立数据服务体系） |
| `twelvedata` | Twelve Data（外汇/加密货币数据服务） |

- **变体名反映真实数据源**：仅 akshare 的上游确为东方财富/新浪/腾讯；
  TqSdk、Tushare、AlphaFeed、TickFlow、Twelve Data 与东方财富无关，
  变体与其 Provider 同名（即各自的上游服务/包名）；
- **`variant` 可选，无全局默认值**。未传递时表示不表达数据源偏好：
  该维度不参与能力过滤，由每个 Provider 在 manifest 中**必填声明**的
  `default_variant`（Provider × 资产类型级）具体化，`meta.variant` 回显实际值；
  `default_variant` 不覆盖其余维度时回退到声明顺序第一条匹配能力的第一个变体；
- 显式传递 `variant` 时严格匹配，不会静默降级；Provider 不支持的变体在能力目录即不声明；
- variant 与 capability、内部 operation 一样参与路由维度匹配；
- 每个 Provider 的 `default_variant` 声明见 `/v1/capabilities` 响应的
  `default_variant` 字段（如 akshare：stock/index → `eastmoney`，
  contract/continuous → `sina`；其余单源 Provider → 其自身变体）。

### adjust 复权方式（默认值约定）

`adjust` 取值 `none`（不复权）、`qfq`（前复权）、`hfq`（后复权）。**未传递时的声明默认值按资产类型区分**：

| 资产类型 | 默认 adjust |
| --- | --- |
| `stock` | **`qfq`（前复权）** |
| 其余（index、contract 等） | `none` |

通用参数转换的原则是：**所有相关参数都显式传递给上游，不依赖上游默认值**。例如 akshare `stock_zh_a_hist` 的 `adjust` 默认 `""`（不复权），与 tapehub 的声明默认值（个股 qfq）不一致，因此适配器始终显式传参，保证「接口默认行为」与「上游实际行为」一致。

### interval：ISO 8601 Duration

周期直接使用 ISO 8601 Duration：

| 周期 | 取值 | | 周期 | 取值 |
| --- | --- | --- | --- | --- |
| 1 秒 | `PT1S` | | 1 日 | `P1D` |
| 1 分钟 | `PT1M` | | 1 周 | `P1W` |
| 5 分钟 | `PT5M` | | 1 月 | `P1M` |
| 1 小时 | `PT1H` | | 1 年 | `P1Y` |

- **`PT1M`（1 分钟）与 `P1M`（1 个月）是不同周期**；
- 不把 `P1M` 换算为固定 30 天，也不把周期统一换算为秒数；`P1W` 与 `P7D`、`PT1H` 与 `PT60M` 视为**不同**周期代码；
- `tick` 不是 ISO 8601 周期；manifest 中出现非法周期会在加载时被拒绝。

### 时间边界与时区

- 时间范围是**闭区间 `[start, end]`**，`start` 必须早于 `end`；
- 时间边界**必须携带时区偏移**（国内标的推荐 `+08:00`，如
  `2026-01-05T00:00:00+08:00`）；纯日期（`2026-01-01`）与无时区的裸时间
  一律拒绝，不做隐式时区解释；
- **项目内部统一北京时间**：服务级时间戳（健康检查 `timestamp`、能力目录
  `generated_at`）一律北京时间（`+08:00` 后缀）；行情数据时间戳按标的所属
  市场输出——国内（CN）统一北京时间（`+08:00`）；GLOBAL（twelvedata 等）
  暂维持 UTC（`Z` 结尾），待数据探针确认上游口径后统一为北京时间；
- 上游只支持按日期过滤时，适配器在标准化后按 `[start, end]` 闭区间精确复核。

### 分页语义

| 参数 | 默认 | 约束 |
| --- | --- | --- |
| `offset` | `0` | ≥ 0 |
| `limit` | `500` | ≥ 1，最大值由 `TAPEHUB_MAX_LIMIT` 配置（默认 5000） |

- 结果先按 `timestamp` **升序**排序，再应用 `offset`/`limit`；
- 两者描述**统一接口最终结果**的分页语义；akshare 接口均不支持原生分页，由适配器用共享组件在内存切片（不落盘）；
- 第一版不返回 `total`；响应 `meta` 返回 `offset`、`limit`、`count`。

## 错误处理

```json
{
  "error": {
    "code": "UNSUPPORTED_CAPABILITY",
    "message": "Requested capability is not supported by the provider.",
    "request_id": "req_...",
    "details": {}
  }
}
```

| 错误码 | HTTP | 含义 |
| --- | --- | --- |
| `INVALID_REQUEST` | 422 | 参数校验失败 |
| `PROVIDER_NOT_FOUND` | 404 | 指定的 Provider 不存在 |
| `PROVIDER_DISABLED` | 409 | 指定的 Provider 已禁用 |
| `PROVIDER_NOT_IMPLEMENTED` | 501 | Provider 已启用但未实现 / SDK 未安装 |
| `UNSUPPORTED_CAPABILITY` | 422 | 能力目录或内部操作不支持该请求 |
| `NO_PROVIDER_AVAILABLE` | 409 | `auto` 路由无可用候选 |
| `CONCURRENCY_LIMIT` | 429 | 网关侧并发闸门：shared 模式在飞取数已达上限（`TAPEHUB_TQSDK_MAX_INFLIGHT_FETCH`），可稍后重试 |
| `UPSTREAM_TIMEOUT` | 504 | 上游超时 |
| `UPSTREAM_RATE_LIMITED` | 429 | 上游限频 |
| `UPSTREAM_ERROR` | 502 | 上游其它错误 |

SDK 异常不会穿透 API；日志不记录 Provider 密钥与完整行情响应。

## 架构与扩展

```text
API 层 → Application 层 → Domain 层
                         ↑
        Provider 适配器实现 Domain Port
```

- **能力目录是唯一事实来源**：`/v1/capabilities` 与公共路由复用同一份目录；
- **两级路由**：公共 `ProviderRouter` 按 provider × data_type × market × asset_type × interval × adjust × **variant** 选 Provider；适配器按 manifest 的 `operations` 做内部二级路由，上游函数名不暴露到公共 API；
- **auto 路由优先级（显式声明，非隐式顺序）**：显式 `provider=X` > manifest 的
  `default_provider` 声明 > 注册顺序（兜底）。每个 Provider 必填声明
  `default_provider`（资产类型列表，`[]` 表示对任何类型都不是缺省），注册表构建期
  强制校验**每个被覆盖的资产类型恰好一个缺省**（0 个或多个都拒绝启动）。
  当前声明：akshare `[stock, index]`、tqsdk `[contract, continuous, weighted-index, etf]`、
  twelvedata `[forex, crypto]`、tushare/alphafeed/tickflow `[]`；
- **manifest 属于配置**：缺失、格式错误或校验失败直接启动失败（fail-fast）；
  适配器导入失败（可选 SDK 未安装等）仍单 Provider 隔离，不影响启动；
- **公共能力声明 vs 内部 operation 映射**：manifest 的 `capabilities` 面向目录与公共路由（含 `variants` 声明）；`operations` 的 match 键包含全部六个维度且必须显式声明，面向适配器内部。

### akshare 已接入范围

接口签名均已按官方文档核实（https://akshare.akfamily.xyz/）：

| 资产类型 | variant | akshare 接口 | 周期 | adjust | 标准字段 |
| --- | --- | --- | --- | --- | --- |
| stock | eastmoney | `stock_zh_a_hist` | P1D/P1W/P1M | none/qfq/hfq | 全部 |
| stock | sina | `stock_zh_a_daily` | P1D | none/qfq/hfq | 全部 |
| stock | tencent | `stock_zh_a_hist_tx` | P1D | none/qfq/hfq | 全部 |
| index | eastmoney | `stock_zh_index_daily_em` | P1D | none | 全部 |
| index | sina | `stock_zh_index_daily` | P1D | none | 无 amount |
| index | tencent | `stock_zh_index_daily_tx` | P1D | none | 无 amount（上游 amount 列语义为成交量） |
| contract / continuous | sina | `futures_zh_daily_sina` | P1D | none | 全部（extensions: hold/settle） |

TickFlow（REST API `GET https://api.tickflow.org/v1/klines`，官方 SDK `tickflow`）：

| 资产类型 | 变体 | 上游接口 | 周期 | adjust | 标准字段 |
| --- | --- | --- | --- | --- | --- |
| stock | tickflow（默认） | `tf.klines.get` | PT1M~PT30M/PT1H、P1D/P1W/P1M/P1Y | none/qfq/hfq | 全部（extensions: prev_close） |

- API Key 通过环境变量 `TAPEHUB_TICKFLOW_API_KEY` 提供并传入 SDK 客户端，禁止写入代码/manifest/日志；未配置时能力目录正常，数据请求返回 `PROVIDER_NOT_IMPLEMENTED`；
- 标的代码映射：`CN-SSE-600000 → 600000.SH`、`CN-SZSE-… → .SZ`、`CN-BSE-… → .BJ`（后缀 SH/SZ/BJ/US/HK 已按文档核实，第一版仅声明 A 股）；
- 周期映射：`PT1M→1m`、`PT5M→5m`、`PT10M→10m`、`PT15M→15m`、`PT30M→30m`、`PT1H→60m`、`P1D→1d`、`P1W→1w`、`P1M→1M`、`P1Y→1Y`；季线 `1Q` 无法用 ISO 8601 Duration 表达，不声明；
- 复权映射：`qfq→forward`（比例前复权）、`hfq→backward`、`none→none`（差值加法变体不对外暴露）；SDK 自身默认 forward，适配器始终显式传参；
- 上游单次上限 10000 根：达到上限时响应 `meta.partial=true` 且 `warnings` 提示可能截断；
- 上游 SDK 的具体异常类型未在文档中核实，目前统一包装为 `UPSTREAM_ERROR`（限频 429 的细分错误码待核实后补充）。

AlphaFeed（REST API `GET https://api.alphafeed.org/v1/klines`，官方 SDK `alphafeed`）：

| 资产类型 | 变体 | 上游接口 | 周期 | adjust | 标准字段 |
| --- | --- | --- | --- | --- | --- |
| stock | alphafeed（默认） | `af.klines.get` | PT1M/PT5M/PT15M/PT30M/PT1H、P1D/P1W/P1M/P1Y | none/qfq/hfq | 全部（extensions: prev_close） |

- API Key 通过环境变量 `TAPEHUB_ALPHAFEED_API_KEY` 提供并传入 SDK 客户端，禁止写入代码/manifest/日志；未配置时能力目录正常，数据请求返回 `PROVIDER_NOT_IMPLEMENTED`；
- 标的代码映射：`CN-SSE-600519 → 600519.SH`、`CN-SZSE-… → .SZ`、`CN-BSE-… → .BJ`（示例 600519.SH / 000001.SZ / 920047.BJ 已按文档核实）；
- 周期映射：`PT1M→1m`、`PT5M→5m`、`PT15M→15m`、`PT30M→30m`、`PT1H→60m`、`P1D→1d`、`P1W→1w`、`P1M→1M`、`P1Y→1Y`；**上游无 10m 周期**，`PT10M` 不声明；季线 `1Q` 无法用 ISO 8601 Duration 表达，不声明；
- 复权映射：`qfq→forward`、`hfq→backward`、`none→none`（差值加法变体不对外暴露）；上游自身默认 forward，适配器始终显式传参；
- 上游单次上限 10000 根：达到上限时响应 `meta.partial=true` 且 `warnings` 提示可能截断；
- 分钟级周期需上游套餐开通分钟 K 线权限（403 时统一包装为 `UPSTREAM_ERROR`）。

Tushare（官方 SDK `tushare`：`ts.pro_bar` / `pro.fut_daily`）：

| 资产类型 | 变体 | 上游接口 | 周期 | adjust | 标准字段 |
| --- | --- | --- | --- | --- | --- |
| stock | tushare（默认） | `ts.pro_bar`（asset='E'） | P1D/P1W/P1M | none/qfq/hfq | 全部（extensions: pre_close/change/pct_chg） |
| contract | tushare（默认） | `pro.fut_daily` | P1D | none | 全部（extensions: pre_close/settle/oi） |

- Token 通过环境变量 `TAPEHUB_TUSHARE_TOKEN` 提供并传入 `ts.pro_api`，禁止写入代码/manifest/日志；未配置时能力目录正常，数据请求返回 `PROVIDER_NOT_IMPLEMENTED`；
- 股票代码映射：`CN-SSE-600000 → 600000.SH`、`CN-SZSE-… → .SZ`（后缀已按文档示例核实；北交所 .BJ 未在所引文档示例中出现，暂不声明）；
- 期货合约映射：`CN-SHFE-CU2401 → CU2401.SHF`（后缀仅核实上期所，文档示例 CU1811.SHF；DCE/CZCE/INE/GFEX/CFFEX 待逐所核实后补充）；主力连续另有专门接口（doc_id=189），本次未接入；
- 单位换算（按文档）：股票 `vol` 手→股（×100）、`amount` 千元→元（×1000）；期货 `vol` 保持手、`amount` 万元→元（×10000）；
- `fut_daily` 单次上限 2000 条：达到上限时响应 `meta.partial=true` 且 `warnings` 提示可能截断；该接口要求上游账户 2000 积分；
- 上游积分限频/错误码未在文档中核实，统一包装为 `UPSTREAM_ERROR`。

TqSdk（官方 SDK `tqsdk`：`TqApi(auth=TqAuth(...)).get_kline_serial(...)`）：

| 资产类型 | 变体 | 上游接口 | 周期 | adjust | 标准字段 |
| --- | --- | --- | --- | --- | --- |
| contract / continuous / weighted-index | tqsdk（默认） | `api.get_kline_serial` | PT1M/PT5M/PT15M/PT30M/PT1H、P1D | none | 无 amount（上游无成交额列；extensions: open_oi/close_oi） |
| stock | tqsdk | `api.get_kline_serial`（adj_type） | PT1M/PT5M/PT15M/PT30M/PT1H、P1D | none/qfq/hfq | 无 amount；volume 单位未核实，原样透出 |
| index | tqsdk | `api.get_kline_serial` | PT1M/PT5M/PT15M/PT30M/PT1H、P1D | none（adj_type 对指数无效） | 无 amount；无持仓量列 → extensions null |
| etf | tqsdk（默认） | `api.get_kline_serial`（adj_type） | PT1M/PT5M/PT15M/PT30M/PT1H、P1D | none/qfq/hfq | 无 amount；volume 单位未核实，原样透出 |

- 变体与 default_variant 均为 `tqsdk`（与 Provider 同名，官方 SDK 包名；背后为天勤行情平台，与东方财富无关）：缺省请求即可路由，也可显式传 `variant=tqsdk`；
- `default_provider` 声明：`[contract, continuous, weighted-index, etf]`（ETF 为唯一声明者）；
  stock/index 仅声明能力，缺省仍由 akshare 承担，tqsdk 股票/指数需显式 `provider=tqsdk`；
- **权限说明（usage/mddatas 原文，诚实标注）**：数据可见性由**天勤平台账户
  档位**决定（免费版/专业版），与 SDK 及其版本无关。免费版账户仅期货/期权
  与四大指数（上证50/沪深300/中证500/中证1000）实时行情，A股股票/ETF 实时
  与历史需专业版账户。**当前部署所用快期账户为专业版**（2026-11-04 确认，
  探针盘中实测：股票/ETF 实时五档可用、ETF 历史日线可用）；若换用免费版
  账户，无权限请求失败包装为 `UPSTREAM_ERROR`，不视为能力缺口；
- 复权映射（API 参考 `adj_type` 仅对股票/基金有效）：`qfq→F`、`hfq→B`、`none→None`
  （期货/指数恒为 None，显式传参不依赖上游默认值）；上游「暂不支持复权获取多合约 K 线」
  与本适配器单合约调用不冲突；
- 快期账户凭据（`TqAuth(用户名, 密码)`，必填）通过环境变量 `TAPEHUB_TQSDK_AUTH_USER` / `TAPEHUB_TQSDK_AUTH_PASSWORD` 提供并传入 SDK 客户端，禁止写入代码/manifest/日志；任一未配置时能力目录正常，数据请求返回 `PROVIDER_NOT_IMPLEMENTED`；
- **连接模式 `TAPEHUB_TQSDK_CONN_MODE`**（默认 `per_request`）：
  - `per_request`：每请求在线程池中独立创建并关闭 TqApi 实例（实测建连+首数据 ~0.9–1.5s/请求）；
  - `shared`：进程内唯一长连接 Runtime（`TqSdkRuntime`，专属线程 tick 驱动 + `create_task` 并发协程 +
    `wait_update` 维持推送，2026-11-04 实验验证），首次请求懒启动，此后每请求节省全部建连开销；
    配套：并发闸门 `TAPEHUB_TQSDK_MAX_INFLIGHT_FETCH`（默认 16，超出 429 `CONCURRENCY_LIMIT`）、
    连续 wait_update 异常自动重建新一代、北京时间 04:00 跨交易日回收（采纳官方跨交易日重启建议）、
    停机/回收时在飞请求显式失败（可快速重试）。两种模式标准化语义完全一致，`shared` 问题可一键切回；
- 合约代码映射（交易所全大写，品种代码大小写遵从交易所规定）：`CN-SHFE-cu2607 → SHFE.cu2607`、`CN-CZCE-SR609 → CZCE.SR609`；主力连续 `CN-SHFE-cu → KQ.m@SHFE.cu`；指数连续/加权 `CN-SHFE-bu → KQ.i@SHFE.bu`（标准 symbol 允许小写品种代码，如 tqsdk 的小写品种约定）；
- 周期映射（周期为不超过 1 日的任意秒数）：`PT1M→60`、`PT5M→300`、`PT15M→900`、`PT30M→1800`、`PT1H→3600`、`P1D→86400`；
- 上游按“最新 N 根”返回（`data_length`）：**单次上限 2026-11-03 探针实测 5000 根**
  （PT1M/PT5M 同时恰好 5000；文档 8000/10000 口径未复现，疑受当前套餐限制，
  升级套餐后需重新实证）；当返回数量达到上限且最早一根仍晚于请求 `start` 时，
  更早数据取不到，响应 `meta.partial=true` 且 `warnings` 提示；
- **分钟线为滚动保留**（2026-11-03 探针实测边界，精度 ±5 天，随时间前移）：
  `history` 以 `retention_days` 声明——覆盖「now − N 天 → now」，客户端按查询
  时点自行换算。取值向下取整留安全余量：不足 90 天取 7 的倍数（周）、
  不小于 90 天取 30 的倍数（月）。边界按数据类划分——现货三类逐一相同，
  期货两类各自一致：**1m** 现货 35 天、期货 14 天；**5m** 现货 150 天、期货 84 天；
  **15m** 现货 450 天、期货 240 天；**30m** 现货 930 天、期货 450 天、加权 390 天；
  **1H** 现货 1500 天、期货 840 天、加权 690 天。六周期折算根数均落在
  **5000 根量级**，与单次上限（data_length 5000）一致——保留深度即序列存储上限；
- `datetime` 为纳秒时间戳（绝对时间），标准化为北京时间（`+08:00`）；期货/指数无复权概念，`adjust` 仅声明 none；
- **复权实证**（2026-11-03 探针）：hfq 与 none 差异显著（生效）；分钟级 qfq 与 none 相同（天勤特性，如实标注）；日线 qfq/hfq 均有差异；
- **history 回填**（2026-11-03 探针二分估计：首轮粒度 90 天、次轮 5 天）：
  日线以固定起点 `start` 声明——continuous/weighted-index 自 2016-01-05、
  stock/index/etf 自 2018-01-02；contract 随挂牌期限，保持 null；
  分钟级以滚动保留 `retention_days` 声明（见上）；
  `/v1/capabilities` 的 interval history 字段按声明形式原样透出；
- 代码映射（股票/指数/ETF 同期货格式）：`CN-SSE-600000 → SSE.600000`、`CN-SZSE-000001 → SZSE.000001`、`CN-SSE-000016 → SSE.000016`、`CN-SSE-510050 → SSE.510050`（mddatas 文档示例口径；指数仅核实 SSE/SZSE 发布的代码）；
- 每次请求在线程池中独立创建并关闭 TqApi 实例（官方约定一个线程一个实例）；
- 上游 SDK 异常类型未在文档中核实，统一包装为 `UPSTREAM_ERROR`。

单位约定（按官方文档）：

- 个股 `volume` 统一为**股**：东财接口返回手，适配器 ×100 换算；新浪/腾讯为股，原样返回；TqSdk 股票/ETF 的 volume 单位文档未写明，**2026-11-03 探针量级对照判定为股**（600000 日 volume ≈5×10^7），原样透出；
- `amount` 统一为**元**；指数/期货的上游未标注单位时原样返回（见各接口文档）；
- 交易所映射（已核实部分）：个股/指数 `SSE→sh`、`SZSE→sz`；指数东财另支持 `CSI→csi`、`BSE→bj`；期货合约符号原样传递（如 `RB2410`），主力连续为品种代码+`0`（如 `RB`→`RB0`）。

未接入说明（诚实边界，不编造能力）：

- `futures_hist_em`（东财期货）需要**中文合约名**（如“热卷主连”），无法从标准代码映射，第一版不接入；
- 加权（`weighted-index`）在上述接口文档中没有符号说明，暂不声明能力；
- 历史覆盖范围（`history`）已由数据探针实测回填（2026-11-04，窗口粒度 90 天，approx）：个股各源 `≈2001-01-01`（complete=False，预算耗尽近似，待精化）、指数各源 `≈1991-01-01`；期货未实测，保持 `null`。

Twelve Data（官方库 `twelvedata`（twelvedata-python）：`TDClient(apikey=...).time_series(...).as_json()`）：

| 资产类型 | 变体 | 上游接口 | 周期 | adjust | 标准字段 |
| --- | --- | --- | --- | --- | --- |
| forex | twelvedata（默认） | `td.time_series` | PT1M~PT1H、P1D/P1W/P1M | none | 无 volume/amount（外汇无成交量） |
| crypto | twelvedata（默认） | `td.time_series` | PT1M~PT1H、P1D/P1W/P1M | none | 无 amount（volume 上游可能缺失 → null） |

- API Key 通过环境变量 `TAPEHUB_TWELVEDATA_API_KEY` 提供并传入 `TDClient(apikey=...)`，禁止写入代码/manifest/日志；未配置时能力目录正常，数据请求返回 `PROVIDER_NOT_IMPLEMENTED`；
- 标的代码约定（symbol 段允许斜杠，对应上游货币对写法）：外汇 `GLOBAL-OTC-EUR/USD → EUR/USD`（ISO 4217，OTC 场外聚合报价）；加密货币 `GLOBAL-CRYPTO-BTC/USD → BTC/USD`（聚合对；交易所专属对与 `exchange` 筛选参数暂不支持）；
- interval 枚举（上游完整支持 1min/5min/15min/30min/45min/1h/2h/4h/8h/1day/1week/1month）：本适配器声明 `PT1M/PT5M/PT15M/PT30M/PT1H/P1D/P1W/P1M`（45min/2h/4h/8h 暂不声明）；
- 外汇/加密货币的 bar 时间按 **UTC**（官方文档明确）；`start_date`/`end_date` 显式传 `YYYY-MM-DDTHH:MM:SS`；`outputsize` 显式传上限 5000（上游默认仅 30）；
- 上游 `values` 默认倒序（最新在前），适配器统一按升序排序输出；`values` 字段为字符串，标准化为数值；
- 上游 SDK/HTTP 异常类型与限频规则未在所引文档中核实，统一包装为 `UPSTREAM_ERROR`。

六个内置 Provider（akshare、tickflow、alphafeed、tushare、tqsdk、twelvedata）均已接入，接口签名与数据口径按各自官方文档核实；未配置凭据或未安装 SDK 时数据请求返回 `PROVIDER_NOT_IMPLEMENTED`，`provider=auto` 在无可用候选时返回 `NO_PROVIDER_AVAILABLE`，也**绝不返回写死的模拟行情**。

### 新增 / 接入 Provider

1. 新建 `src/tapehub/providers/<name>/`，放置 `manifest.yaml`（能力声明 + operations 映射，match 六维度显式声明）；
2. 实现 `adapter.py`（继承 `BaseProviderAdapter`，`implemented = True`，`fetch_bars` 内延迟导入 SDK 并用 `call_in_thread` 隔离同步调用）；
3. 新增 `request_mapper.py`（所有参数显式传递，不依赖上游默认值）与 `response_mapper.py`（缺失值→null，扩展字段白名单，单位按文档换算）；
4. `pyproject.toml` 以 `[project.optional-dependencies]` 添加该 SDK；
5. 凭据只能通过环境变量提供，禁止写入 manifest、日志或异常响应。

接入一个新 Provider 需要修改的最小文件集合：

```text
src/tapehub/providers/<name>/manifest.yaml
src/tapehub/providers/<name>/adapter.py
src/tapehub/providers/<name>/request_mapper.py   # 新增
src/tapehub/providers/<name>/response_mapper.py  # 新增
pyproject.toml
tests/                                           # 该适配器的 Mock 测试
```

Application/API 层与其它 Provider 不需要任何改动。

### 配置

环境变量前缀 `TAPEHUB_`（见 [.env.example](.env.example)）：`TAPEHUB_VERSION`、`TAPEHUB_API_VERSION`、`TAPEHUB_CATALOG_VERSION`、`TAPEHUB_MAX_LIMIT`、`TAPEHUB_PROVIDER_ENABLED_OVERRIDES`（JSON，如 `{"akshare": false}` 可整体停用 akshare）、`TAPEHUB_TICKFLOW_API_KEY`、`TAPEHUB_ALPHAFEED_API_KEY`、`TAPEHUB_TUSHARE_TOKEN`、`TAPEHUB_TQSDK_AUTH_USER`、`TAPEHUB_TQSDK_AUTH_PASSWORD`、`TAPEHUB_TQSDK_CONN_MODE`（`per_request`/`shared`）、`TAPEHUB_TQSDK_MAX_INFLIGHT_FETCH`、`TAPEHUB_TWELVEDATA_API_KEY`（Provider 凭据，只在内存中传给 SDK 客户端）。`.env` 不提交版本库。

### 未来扩展边界（现在不实现）

- **定时全量转储**：适配器不依赖 FastAPI 请求上下文，未来 Worker 可复用统一查询与分页模型逐批拉取；
- **实时行情订阅**：未来新增独立 `StreamingProvider` 接口，不侵入历史查询链路；
- **新资产类型**：扩展标准枚举、能力清单与对应操作映射即可；
- 第一版不存储任何行情数据。

## 测试

```bash
uv run pytest
```

所有外部 SDK 调用均通过 `sys.modules` 注入的桩 Mock，测试不发起真实网络请求、不落盘、不依赖真实 Token；akshare 的调用参数与响应列均按官方文档构造。
