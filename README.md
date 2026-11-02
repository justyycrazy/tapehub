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
- 基于 Fake Provider 与 Mock SDK 的完整链路验证与单元/契约测试。

明确不实现（第一版）：

- 数据库、Redis、行情缓存、CSV/Parquet 等行情落盘；
- 定时任务、全量转储 Worker、消息队列、WebSocket/SSE 实时订阅；
- 实时行情、快照、逐笔、盘口、标的元数据等其它查询接口；
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

只读取本地能力目录，不实时访问上游；支持可选过滤参数 `provider`、`data_type`、`market`、`asset_type`、`interval`、`variant`。`enabled`（是否启用）与 `runtime_status`（运行状态，第一版恒为 `unknown`）是两个不同概念。

### 历史 K 线

```http
GET /v1/market/bars?provider=akshare&asset_type=stock&instrument=CN-SSE-600000&interval=P1D&start=2026-01-01&end=2026-01-31&offset=0&limit=100
```

参数：`provider`（默认 `auto`）、`asset_type`（必填）、`instrument`（必填）、`interval`（必填）、`start`/`end`（必填）、`adjust`（可选）、`variant`（可选，默认 `eastmoney`）、`offset`（默认 0）、`limit`（默认 500）。

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

同一 Provider 下同一资产类型往往有多个上游接口（如 akshare 个股有东方财富/新浪/腾讯三个源）。统一请求通过可选参数 `variant` 指定，取值为**中立站点名**而非上游函数名：

| variant | 含义 | 默认 |
| --- | --- | --- |
| `eastmoney` | 东方财富 | ✅ 默认值 |
| `sina` | 新浪 | |
| `tencent` | 腾讯 | |

- 未传递 `variant` 时默认 `eastmoney`；Provider 不支持的变体在能力目录即不声明；
- variant 与 capability、内部 operation 一样参与路由维度匹配，不会静默降级。

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

### 时间边界

- 时间范围是**闭区间 `[start, end]`**，`start` 必须早于 `end`；
- 纯日期（`2026-01-01`）按 UTC 0 点解释；带时间的值**必须包含时区**；
- 响应中的时间统一为 UTC（`Z` 结尾）；日线 K 线的时间戳为该交易日 00:00 UTC；
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
| stock | eastmoney（默认，单接口 Provider） | `tf.klines.get` | PT1M~PT30M/PT1H、P1D/P1W/P1M/P1Y | none/qfq/hfq | 全部（extensions: prev_close） |

- API Key 通过环境变量 `TAPEHUB_TICKFLOW_API_KEY` 提供并传入 SDK 客户端，禁止写入代码/manifest/日志；未配置时能力目录正常，数据请求返回 `PROVIDER_NOT_IMPLEMENTED`；
- 标的代码映射：`CN-SSE-600000 → 600000.SH`、`CN-SZSE-… → .SZ`、`CN-BSE-… → .BJ`（后缀 SH/SZ/BJ/US/HK 已按文档核实，第一版仅声明 A 股）；
- 周期映射：`PT1M→1m`、`PT5M→5m`、`PT10M→10m`、`PT15M→15m`、`PT30M→30m`、`PT1H→60m`、`P1D→1d`、`P1W→1w`、`P1M→1M`、`P1Y→1Y`；季线 `1Q` 无法用 ISO 8601 Duration 表达，不声明；
- 复权映射：`qfq→forward`（比例前复权）、`hfq→backward`、`none→none`（差值加法变体不对外暴露）；SDK 自身默认 forward，适配器始终显式传参；
- 上游单次上限 10000 根：达到上限时响应 `meta.partial=true` 且 `warnings` 提示可能截断；
- 上游 SDK 的具体异常类型未在文档中核实，目前统一包装为 `UPSTREAM_ERROR`（限频 429 的细分错误码待核实后补充）。

AlphaFeed（REST API `GET https://api.alphafeed.org/v1/klines`，官方 SDK `alphafeed`）：

| 资产类型 | 变体 | 上游接口 | 周期 | adjust | 标准字段 |
| --- | --- | --- | --- | --- | --- |
| stock | eastmoney（默认，单接口 Provider） | `af.klines.get` | PT1M/PT5M/PT15M/PT30M/PT1H、P1D/P1W/P1M/P1Y | none/qfq/hfq | 全部（extensions: prev_close） |

- API Key 通过环境变量 `TAPEHUB_ALPHAFEED_API_KEY` 提供并传入 SDK 客户端，禁止写入代码/manifest/日志；未配置时能力目录正常，数据请求返回 `PROVIDER_NOT_IMPLEMENTED`；
- 标的代码映射：`CN-SSE-600519 → 600519.SH`、`CN-SZSE-… → .SZ`、`CN-BSE-… → .BJ`（示例 600519.SH / 000001.SZ / 920047.BJ 已按文档核实）；
- 周期映射：`PT1M→1m`、`PT5M→5m`、`PT15M→15m`、`PT30M→30m`、`PT1H→60m`、`P1D→1d`、`P1W→1w`、`P1M→1M`、`P1Y→1Y`；**上游无 10m 周期**，`PT10M` 不声明；季线 `1Q` 无法用 ISO 8601 Duration 表达，不声明；
- 复权映射：`qfq→forward`、`hfq→backward`、`none→none`（差值加法变体不对外暴露）；上游自身默认 forward，适配器始终显式传参；
- 上游单次上限 10000 根：达到上限时响应 `meta.partial=true` 且 `warnings` 提示可能截断；
- 分钟级周期需上游套餐开通分钟 K 线权限（403 时统一包装为 `UPSTREAM_ERROR`）。

Tushare（官方 SDK `tushare`：`ts.pro_bar` / `pro.fut_daily`）：

| 资产类型 | 变体 | 上游接口 | 周期 | adjust | 标准字段 |
| --- | --- | --- | --- | --- | --- |
| stock | eastmoney（默认） | `ts.pro_bar`（asset='E'） | P1D/P1W/P1M | none/qfq/hfq | 全部（extensions: pre_close/change/pct_chg） |
| contract | eastmoney（默认） | `pro.fut_daily` | P1D | none | 全部（extensions: pre_close/settle/oi） |

- Token 通过环境变量 `TAPEHUB_TUSHARE_TOKEN` 提供并传入 `ts.pro_api`，禁止写入代码/manifest/日志；未配置时能力目录正常，数据请求返回 `PROVIDER_NOT_IMPLEMENTED`；
- 股票代码映射：`CN-SSE-600000 → 600000.SH`、`CN-SZSE-… → .SZ`（后缀已按文档示例核实；北交所 .BJ 未在所引文档示例中出现，暂不声明）；
- 期货合约映射：`CN-SHFE-CU2401 → CU2401.SHF`（后缀仅核实上期所，文档示例 CU1811.SHF；DCE/CZCE/INE/GFEX/CFFEX 待逐所核实后补充）；主力连续另有专门接口（doc_id=189），本次未接入；
- 单位换算（按文档）：股票 `vol` 手→股（×100）、`amount` 千元→元（×1000）；期货 `vol` 保持手、`amount` 万元→元（×10000）；
- `fut_daily` 单次上限 2000 条：达到上限时响应 `meta.partial=true` 且 `warnings` 提示可能截断；该接口要求上游账户 2000 积分；
- 上游积分限频/错误码未在文档中核实，统一包装为 `UPSTREAM_ERROR`。

TqSdk（官方 SDK `tqsdk`：`TqApi(auth=TqAuth(...)).get_kline_serial(...)`）：

| 资产类型 | 变体 | 上游接口 | 周期 | adjust | 标准字段 |
| --- | --- | --- | --- | --- | --- |
| contract / continuous / weighted-index | eastmoney（默认） | `api.get_kline_serial` | PT1M/PT5M/PT15M/PT30M/PT1H、P1D | none | 无 amount（上游无成交额列；extensions: open_oi/close_oi） |

- 快期账户凭据（`TqAuth(用户名, 密码)`，必填）通过环境变量 `TAPEHUB_TQSDK_AUTH_USER` / `TAPEHUB_TQSDK_AUTH_PASSWORD` 提供并传入 SDK 客户端，禁止写入代码/manifest/日志；任一未配置时能力目录正常，数据请求返回 `PROVIDER_NOT_IMPLEMENTED`；
- 合约代码映射（交易所全大写，品种代码大小写遵从交易所规定）：`CN-SHFE-cu2607 → SHFE.cu2607`、`CN-CZCE-SR609 → CZCE.SR609`；主力连续 `CN-SHFE-cu → KQ.m@SHFE.cu`；指数连续/加权 `CN-SHFE-bu → KQ.i@SHFE.bu`（标准 symbol 允许小写品种代码，如 tqsdk 的小写品种约定）；
- 周期映射（周期为不超过 1 日的任意秒数）：`PT1M→60`、`PT5M→300`、`PT15M→900`、`PT30M→1800`、`PT1H→3600`、`P1D→86400`；
- 上游按“最新 N 根”返回（`data_length`，每序列上限 10000，使用文档另有 8000 根的说明、以 API 参考为准）：当返回数量达到上限且最早一根仍晚于请求 `start` 时，更早数据取不到，响应 `meta.partial=true` 且 `warnings` 提示；
- `datetime` 为纳秒时间戳（绝对时间），标准化为 UTC；期货无复权概念，`adjust` 仅声明 none；
- 每次请求在线程池中独立创建并关闭 TqApi 实例（官方约定一个线程一个实例）；
- 上游 SDK 异常类型未在文档中核实，统一包装为 `UPSTREAM_ERROR`。

单位约定（按官方文档）：

- 个股 `volume` 统一为**股**：东财接口返回手，适配器 ×100 换算；新浪/腾讯为股，原样返回；
- `amount` 统一为**元**；指数/期货的上游未标注单位时原样返回（见各接口文档）；
- 交易所映射（已核实部分）：个股/指数 `SSE→sh`、`SZSE→sz`；指数东财另支持 `CSI→csi`、`BSE→bj`；期货合约符号原样传递（如 `RB2410`），主力连续为品种代码+`0`（如 `RB`→`RB0`）。

未接入说明（诚实边界，不编造能力）：

- `futures_hist_em`（东财期货）需要**中文合约名**（如“热卷主连”），无法从标准代码映射，第一版不接入；
- 加权（`weighted-index`）在上述接口文档中没有符号说明，暂不声明能力；
- 历史覆盖范围（`history`）未核实，能力目录中保持 `null`。

五个内置 Provider（akshare、tickflow、alphafeed、tushare、tqsdk）均已接入，接口签名与数据口径按各自官方文档核实；未配置凭据或未安装 SDK 时数据请求返回 `PROVIDER_NOT_IMPLEMENTED`，`provider=auto` 在无可用候选时返回 `NO_PROVIDER_AVAILABLE`，也**绝不返回写死的模拟行情**。

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

环境变量前缀 `TAPEHUB_`（见 [.env.example](.env.example)）：`TAPEHUB_VERSION`、`TAPEHUB_API_VERSION`、`TAPEHUB_CATALOG_VERSION`、`TAPEHUB_MAX_LIMIT`、`TAPEHUB_PROVIDER_ENABLED_OVERRIDES`（JSON，如 `{"akshare": false}` 可整体停用 akshare）、`TAPEHUB_TICKFLOW_API_KEY`、`TAPEHUB_ALPHAFEED_API_KEY`、`TAPEHUB_TUSHARE_TOKEN`、`TAPEHUB_TQSDK_AUTH_USER`、`TAPEHUB_TQSDK_AUTH_PASSWORD`（Provider 凭据，只在内存中传给 SDK 客户端）。`.env` 不提交版本库。

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
