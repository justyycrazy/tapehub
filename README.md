# tapehub

通用行情数据网关：对外提供统一的 HTTP RESTful API，内部通过适配器接入多个行情数据源（计划：akshare、tushare、tickflow、alphafeed、tqsdk）。

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
- 统一错误处理；基于测试 Fake Provider 的完整链路验证与单元/契约测试。

明确不实现（第一版）：

- 数据库、Redis、行情缓存、CSV/Parquet 等行情落盘；
- 定时任务、全量转储 Worker、消息队列、WebSocket/SSE 实时订阅；
- 实时行情、快照、逐笔、盘口、标的元数据等其它查询接口；
- Docker 与前端页面；
- 真实 Provider 的未经核实接入（见下文「真实 Provider 接入状态」）。

## 快速开始

使用 [uv](https://docs.astral.sh/uv/) 管理项目，Python 3.12。

```bash
# 安装 uv（任选其一）
# pipx install uv
# powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

uv sync --dev          # 同步依赖（自动准备 Python 3.12）

uv run pytest          # 运行测试
uv run ruff check .    # 静态检查
uv run uvicorn tapehub.api.app:app --reload   # 启动服务（默认 127.0.0.1:8000）
```

## 连接测试（base_url + /health）

其他项目把 `base_url` 配置为本服务地址后，调用：

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

调用方可同时校验 **HTTP 200** 与 **`service == "tapehub"`**，避免 `base_url` 配置到了其他服务。该接口不访问任何上游 Provider，也不需要鉴权。

## API 示例

### 能力目录

```http
GET /v1/capabilities?asset_type=stock&interval=P1D
GET /v1/providers/tushare/capabilities
```

只读取本地能力目录，不实时访问上游；支持可选过滤参数 `provider`、`data_type`、`market`、`asset_type`、`interval`。`enabled`（是否启用）与 `runtime_status`（运行状态，第一版恒为 `unknown`）是两个不同概念。

### 历史 K 线

```http
GET /v1/market/bars?provider=auto&asset_type=stock&instrument=CN-SSE-600000&interval=P1D&start=2026-01-01&end=2026-01-31&adjust=none&offset=0&limit=100
```

成功响应（节选）：

```json
{
  "data": {
    "instrument": "CN-SSE-600000",
    "asset_type": "stock",
    "interval": "P1D",
    "bars": [
      {
        "timestamp": "2026-01-01T00:00:00Z",
        "open": 10.0, "high": 10.1, "low": 9.9, "close": 10.05,
        "volume": 1000.0, "amount": 10050.0,
        "extensions": null
      }
    ]
  },
  "meta": {
    "request_id": "req_...",
    "provider": "fake-provider",
    "source_time": null,
    "received_at": "2026-01-01T00:00:00Z",
    "offset": 0, "limit": 100, "count": 21, "partial": false
  },
  "warnings": []
}
```

缺失数值以 `null` 返回，不用 `0` 冒充；Provider 特有字段只能通过 `extensions` 受控透出。

## 请求规范

### instrument 标准代码

格式为 `{market}-{exchange}-{symbol}`，全部大写，例如 `CN-SSE-600000`。

- `market`、`exchange` 由服务端解析，调用方无需重复传递；
- **不接受** `CN.SSE.600000`、`600000` 等非标准格式；Provider 专用代码由各自适配器转换；
- 第一版只校验标准格式，不通过标的元数据验证真实资产类型。

### asset_type 必须显式传递

`asset_type`（`stock` / `etf` / `index` / `futures`）是**必填**字段：

- 同一数字代码在不同资产类型下含义不同（指数、ETF、股票可能共用代码段）；
- 服务端不做默认推断，也不根据代码格式猜测；
- 缺失时返回统一参数错误 `INVALID_REQUEST`。

### interval：ISO 8601 Duration

周期直接使用 ISO 8601 Duration，不使用自定义缩写：

| 周期 | 取值 | | 周期 | 取值 |
| --- | --- | --- | --- | --- |
| 1 秒 | `PT1S` | | 1 日 | `P1D` |
| 1 分钟 | `PT1M` | | 1 周 | `P1W` |
| 5 分钟 | `PT5M` | | 1 月 | `P1M` |
| 1 小时 | `PT1H` | | 1 年 | `P1Y` |

注意事项：

- **`PT1M`（1 分钟）与 `P1M`（1 个月）是不同周期**，能力匹配互不相通；
- 不会把 `P1M` 换算为固定 30 天，也不会把周期统一换算为秒数；
- 周期相等当且仅当各分量一致，因此 `P1W` 与 `P7D`、`PT1H` 与 `PT60M` 视为**不同**周期代码；
- `tick` 不是 ISO 8601 周期，不在第一版范围内；manifest 中出现非法周期会在加载时被拒绝。

### 时间边界

- 时间范围是**闭区间 `[start, end]`**，`start` 必须早于 `end`；
- 纯日期（`2026-01-01`）按 UTC 0 点解释；
- 带时间的值**必须包含时区**（如 `2026-01-01T09:30:00+08:00` 或 `...Z`），否则返回参数错误；
- 响应中的时间统一转换为 UTC（`Z` 结尾）。

### 分页语义

| 参数 | 默认 | 约束 |
| --- | --- | --- |
| `offset` | `0` | ≥ 0 |
| `limit` | `500` | ≥ 1，最大值由 `TAPEHUB_MAX_LIMIT` 配置（默认 5000） |

- 结果先按 `timestamp` **升序**排序，再应用 `offset`/`limit`；
- 两者描述**统一接口最终结果**的分页语义，不是 Provider 原生页码；
- 适配器在返回前完成排序与切片（原生支持分页时可下推，但语义不变），Application 层不会二次切片；
- 第一版不返回 `total`，避免为统计总数拉取全量数据；
- 响应 `meta` 返回 `offset`、`limit`、`count`（本次实际返回数量）。

## 错误处理

所有错误返回统一结构：

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
| `PROVIDER_NOT_IMPLEMENTED` | 501 | Provider 已启用但未实现 |
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

- **能力目录是唯一事实来源**：`/v1/capabilities` 与公共路由复用同一份目录，不会出现「接口显示支持、路由却不支持」；
- **两级路由**：公共 `ProviderRouter` 只按能力维度（provider × data_type × market × asset_type × interval × adjust）选 Provider；选中后由适配器按 manifest 中的 `operations` 做内部二级路由（如 `stock_daily` / `etf_daily`），上游函数名不会暴露到公共 API；
- **公共能力声明 vs 内部 operation 映射**：manifest 的 `capabilities` 面向目录与公共路由；`operations` 面向适配器内部，二者分开维护。

### 新增 / 接入 Provider

1. 新建 `src/tapehub/providers/<name>/` 目录，放置 `manifest.yaml`；
2. 在 `manifest.yaml` 中填写**已核实**的能力声明（`capabilities`）与内部操作映射（`operations`）；
3. 实现 `adapter.py`：继承 `BaseProviderAdapter`，把 `implemented` 置为 `True`，实现 `fetch_bars`；
4. Provider SDK 一律作为**可选依赖 + 延迟导入**（只在 `fetch_bars` 内部导入，同步阻塞调用用 `call_in_thread` 隔离）；任一 SDK 缺失不影响服务启动；
5. Token 等凭据只能通过环境变量提供，禁止写入 manifest、日志或异常响应。

接入第一个真实 Provider 需要修改的最小文件集合：

```text
src/tapehub/providers/<name>/manifest.yaml      # 能力声明 + 操作映射
src/tapehub/providers/<name>/adapter.py         # 实现 fetch_bars，延迟导入 SDK
src/tapehub/providers/<name>/request_mapper.py  # 新增：标准请求 → Provider 参数
src/tapehub/providers/<name>/response_mapper.py # 新增：上游数据 → Bar/BarBatch
pyproject.toml                                  # 以 optional-dependencies 添加该 SDK
tests/                                          # 新增该适配器的 Mock 单元测试
```

Application/API 层与其它 Provider 不需要任何改动。

### 真实 Provider 接入状态

五个内置 Provider（akshare、tushare、tickflow、alphafeed、tqsdk）目前均为**未接入**状态：

- manifest 的 `capabilities` / `operations` 为空，`enabled: false`；
- 指定请求返回 `PROVIDER_DISABLED`（默认）或启用后 `PROVIDER_NOT_IMPLEMENTED`；
- `provider=auto` 不会选中它们，也**绝不返回写死的模拟行情**；
- 测试链路由 `tests/fixtures` 中的 Fake Provider 覆盖，仅存在于测试环境。

### 配置

环境变量前缀 `TAPEHUB_`（见 [.env.example](.env.example)）：`TAPEHUB_VERSION`、`TAPEHUB_API_VERSION`、`TAPEHUB_CATALOG_VERSION`、`TAPEHUB_MAX_LIMIT`、`TAPEHUB_PROVIDER_ENABLED_OVERRIDES`（JSON，如 `{"tushare": true}`）。`.env` 不提交版本库。

### 未来扩展边界（现在不实现）

- **定时全量转储**：适配器不依赖 FastAPI 请求上下文，未来 Worker 可复用统一查询与分页模型逐批拉取；现在不建数据库与调度器；
- **实时行情订阅**：未来新增独立 `StreamingProvider` 接口，不扩展 `HistoricalBarProvider`，不侵入历史查询链路；
- **新资产类型**：扩展标准枚举、能力清单与对应操作映射即可，无需修改健康检查与公共响应框架；
- 第一版不存储任何行情数据。

## 测试

```bash
uv run pytest
```

所有外部 SDK 调用均 Mock，测试不发起真实网络请求、不落盘、不依赖真实 Token；测试数据只来自 Fake Provider 与 fixture。
