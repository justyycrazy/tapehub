# 运维与配置

本文是 tapehub 运行配置的唯一出处：依赖管理、服务启动、凭据驱动注册、环境变量全表与安全纪律。项目说明与总体设计见 [README](../README.md)；API 与数据约定见 [api.md](api.md)。

## 依赖管理

使用 [uv](https://docs.astral.sh/uv/) 管理项目，Python 3.12。

```bash
# 安装 uv（任选其一）
# pipx install uv
# powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

uv sync --dev --all-extras       # 同步依赖 + 全部 Provider SDK（推荐日常使用）
uv sync --dev --extra akshare    # 仅装单个 SDK 的写法（会移除其它 Provider SDK）

uv run pytest          # 运行测试（不依赖任何真实 SDK 与网络）
uv run ruff check .    # 静态检查
uv run uvicorn tapehub.api.app:app --reload   # 启动服务（默认 127.0.0.1:8000）
```

**uv「精确同步」陷阱**：`uv sync` 只选部分 extra 时，venv 中不在当前选中集合里的包会被移除——其余 Provider SDK 会被卸载。Provider SDK 之间解析无冲突，务必用 `--all-extras` 共存。

**Provider SDK 均为可选依赖**（`pyproject.toml` `[project.optional-dependencies]`）：不安装时服务正常启动，对应请求返回 `PROVIDER_NOT_IMPLEMENTED`；未配置凭据时同理（见下文「凭据驱动的注册」）。能力目录不受 SDK 安装与凭据状态影响。

## 凭据驱动的注册

内置 Provider 中需要凭据的（alphafeed、tickflow、ths、tiingo、tushare、tqsdk、twelvedata、futunn），`.env` 相应变量**全部非空（strip 后）**才启用；凭据缺失的 Provider 能力目录仍可见但 `enabled=false`，auto 路由跳过，显式请求返回 `PROVIDER_DISABLED`（details 携带缺失的变量名）。akshare 与 baostock 无凭据概念，恒启用。

启用裁决优先级：

```text
显式 TAPEHUB_PROVIDER_ENABLED_OVERRIDES > 凭据推断 > manifest enabled
```

- 显式启用可越过凭据推断作为运维逃生门（启动日志告警，请求仍可能因凭据/SDK 缺失失败）；把未实现的 Provider 置为 enabled 后，请求返回 `PROVIDER_NOT_IMPLEMENTED`；
- 只判非空不做格式校验：格式错误的凭据在请求时报 `UPSTREAM_ERROR`，启动不因凭据内容失败。

## 环境变量全表

前缀 `TAPEHUB_`，模板见 [.env.example](../.env.example)（复制为 `.env` 使用；`.env` 不提交版本库）。字段定义见 `src/tapehub/settings.py`。

### 服务与元数据

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `TAPEHUB_DEPLOYMENT_MODE` | `single` | 部署模式守门字段，当前仅支持 `single`（单进程、单用户最小运行模式），其他值启动即失败；实时快照与 SSE 流均限此模式 |
| `TAPEHUB_VERSION` | `0.1.0` | 服务版本（出现在 `/health` 响应中） |
| `TAPEHUB_API_VERSION` | `v1` | API 版本（出现在 `/health` 响应中） |
| `TAPEHUB_CATALOG_VERSION` | `1` | 能力目录版本（出现在 `/v1/capabilities` 的 meta 中） |
| `TAPEHUB_MAX_LIMIT` | `5000` | 统一分页最大 `limit` |
| `TAPEHUB_DOCS_ENABLED` | `true` | 交互式文档开关（`/docs`、`/redoc`、`/openapi.json`）：false 时三端点整体下线（404）；对外暴露前建议关闭（不走鉴权，API 结构会披露） |
| `TAPEHUB_API_KEYS` | `[]` | 入站 API Key 白名单（JSON 数组，如 `["k1","k2"]`）：非空即启用全部业务端点的 `X-Api-Key` 校验（缺失/不匹配返回 401，统一错误结构）；`/health` 保持无鉴权；留空 = 无鉴权（single 模式设计边界，对外暴露前必须配置，见「安全边界」） |
| `TAPEHUB_CLIENT_RATE_LIMIT_PER_MINUTE` | `0` | 入站客户端限流：每身份每 60 秒固定窗口请求数上限，0 = 关闭。身份为命中白名单的 API Key（未命中一律归 anonymous，桶数上界 = len(API_KEYS)+1）；超限 429 `CLIENT_RATE_LIMITED` + `Retry-After`；`/health` 豁免。保护上游付费配额（进程内计数，不跨进程） |
| `TAPEHUB_PROVIDER_ENABLED_OVERRIDES` | `{}` | JSON 对象，按 provider 名覆盖启用状态（如 `{"akshare": false}` 整体停用 akshare），语义见上文 |
| `TAPEHUB_ROUTING_CONFIG_PATH` | 空 | 缺省路由顺序配置文件路径；留空用包内内置版本（`src/tapehub/config/provider_routing.yaml`），指定路径时**外部文件整体替换内置版本，不做合并** |

### Provider 凭据（全部非空才启用对应 Provider）

| 变量 | Provider | 说明 |
| --- | --- | --- |
| `TAPEHUB_TICKFLOW_API_KEY` | tickflow | REST API + 官方 Python SDK |
| `TAPEHUB_ALPHAFEED_API_KEY` | alphafeed | REST API + 官方 Python SDK |
| `TAPEHUB_TUSHARE_TOKEN` | tushare | Token 即 Tushare 积分凭据 |
| `TAPEHUB_TQSDK_AUTH_USER` | tqsdk | 快期账户（TqAuth）用户名/邮箱 |
| `TAPEHUB_TQSDK_AUTH_PASSWORD` | tqsdk | 快期账户密码；与用户名必须同时配置 |
| `TAPEHUB_TIINGO_API_KEY` | tiingo | REST + WS 直连（httpx2 + websockets） |
| `TAPEHUB_TWELVEDATA_API_KEY` | twelvedata | 官方 Python 库 + websockets |
| `TAPEHUB_THS_API_KEY` | ths | 同花顺金融数据 API，X-api-key 请求头 |
| `TAPEHUB_FUTUNN_APP_KEY` | futunn | 富途「方式二：传统 API Key」AppKey ID |
| `TAPEHUB_FUTUNN_PRIVATE_KEY_PATH` | futunn | 与 AppKey 公钥配对的本地私钥 PEM 路径；与 AppKey 必须同时配置 |

各凭据的获取方式与上游细节见对应 [Provider 文档](providers/)。

### tqsdk 运行时调优

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `TAPEHUB_TQSDK_CONN_MODE` | `shared` | 连接模式：`shared`（进程内唯一长连接 Runtime，首次请求懒启动）/ `per_request`（每请求独立实例，回滚配置） |
| `TAPEHUB_TQSDK_MAX_INFLIGHT_FETCH` | `16` | shared 模式在飞取数上限（并发闸门，超出返回 `CONCURRENCY_LIMIT` 429） |
| `TAPEHUB_TQSDK_FIRST_SNAPSHOT_TIMEOUT` | `5`（秒） | 实时快照首包等待超时；超时返回 `QUOTE_NOT_READY`（HTTP 408） |
| `TAPEHUB_TQSDK_SUBSCRIPTION_LIMIT` | `64` | 休眠订阅回收阈值：tqsdk 不支持取消订阅，退订只停本地维护；休眠订阅累计到该值触发新一代重建清理（仍被引用的订阅自动重订） |

### twelvedata / tiingo 订阅调优

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `TAPEHUB_TWELVEDATA_SUBSCRIPTION_LIMIT` | `8` | WS 并发订阅 symbol 上限（免费版 WS credit 口径），超出返回 `UPSTREAM_SUBSCRIPTION_LIMIT` 429；升档后调大 |
| `TAPEHUB_TWELVEDATA_FIRST_SNAPSHOT_TIMEOUT` | `5`（秒） | 冷订阅首包等待超时，超时返回 `QUOTE_NOT_READY` 408；活跃订阅直接读缓存即时返回 |
| `TAPEHUB_TIINGO_SUBSCRIPTION_LIMIT` | `8` | WS 并发订阅 symbol 软上限（上游未公布并发上限，默认与 twelvedata 对齐防失控；上游硬约束为月度唯一品种数，网关不计数），超出返回 `UPSTREAM_SUBSCRIPTION_LIMIT` 429 |
| `TAPEHUB_TIINGO_FIRST_SNAPSHOT_TIMEOUT` | `5`（秒） | 冷订阅首包等待超时，语义同上 |

### 实时行情流（SSE）与上游超时

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `TAPEHUB_STREAM_MAX_CLIENTS` | `8` | 并发流客户端上限（超出 `STREAM_LIMIT_REACHED` 429） |
| `TAPEHUB_STREAM_QUEUE_SIZE` | `64` | 每客户端事件缓冲长度（仅约束 quote 帧：满时丢旧保新，status 生命周期帧绝不丢弃） |
| `TAPEHUB_STREAM_HEARTBEAT_SECONDS` | `15`（秒） | 空闲心跳间隔：无事件时按该周期发送保活帧 |
| `TAPEHUB_STREAM_IDLE_TIMEOUT_SECONDS` | `300`（秒） | 流空闲超时：该窗口内无行情事件（心跳不算）则本地退订并结束流 |
| `TAPEHUB_UPSTREAM_TIMEOUT_SECONDS` | `30`（秒） | bars/quotes 单次请求从发起到返回的上限，超时返回 `UPSTREAM_TIMEOUT`（504）；SSE 流为长驻设计不适用此值 |
| `TAPEHUB_THREAD_POOL_SIZE` | `40` | 全局线程池容量（anyio 默认 limiter）：`TAPEHUB_THREAD_POOL_SIZE_MAP` 为空时全部线程型上游调用共享；非空时该值仅覆盖不经适配器分组路径的线程调用 |
| `TAPEHUB_THREAD_POOL_SIZE_MAP` | `{}`（空 = 关闭） | 按 Provider 分组线程池容量（JSON 对象，如 `{"akshare": 8}`）：非空即开启分组隔离——每家 Provider 独享名额池（列出者用配置容量，未列出者统一默认 4；10 家合计恰为全局默认 40，开启分组不改变总在飞预算，只改变分配方式）。单家上游挂死/慢响应的名额压力隔离在本家池内，其余 Provider 照常服务；本家池满快速返回 `CONCURRENCY_LIMIT`（429，可重试），应用层 `TAPEHUB_UPSTREAM_TIMEOUT_SECONDS` 兜底 `UPSTREAM_TIMEOUT`（504）。未知 Provider 名仅启动告警不阻断。注：挂死调用被取消/超时后名额立即释放，孤儿线程不占名额但 OS 线程不被回收（上游 socket 无超时属 SDK 限制） |

## 测试与本地环境解耦

测试代码一律用 `tests.conftest.make_settings` 构造 Settings（不读 `.env`），autouse fixture 会清空进程环境中的 `TAPEHUB_*` 变量；本地 `.env` 里的真实凭据不影响 `uv run pytest`（零网络、零真实凭据、不落盘）。服务运行路径不受影响，仍读 `.env`。

## 安全纪律

- Provider 凭据只经环境变量注入，禁止写入代码、manifest.yaml、日志、异常响应或测试 fixture；
- 日志不记录 Provider 密钥与完整行情响应；
- SDK 异常不穿透 API（统一包装为错误码，见 [api.md](api.md) 错误处理）。

### 安全边界（部署警示）

当前版本为 `single` 单进程单用户最小运行模式，**默认无入站鉴权**：

- 服务不强制回环绑定。默认启动命令（`uv run uvicorn tapehub.api.app:app`）监听
  `127.0.0.1:8000`；若以 `--host 0.0.0.0` 等方式绑定非回环地址，任何网络可达
  主体都可无门槛调用全部端点——消耗已配置 Provider 的付费配额（tushare 积分、
  ths/futunn/tiingo/twelvedata Key、tqsdk 账户）并读取全部行情数据；
- 对外（非回环）暴露前，必须配置入站认证（可选 API Key，`TAPEHUB_API_KEYS`，
  见 [api.md](api.md)「认证」）或确保端口仅处于可信网络内；
- 对外暴露时建议关闭交互式文档（`TAPEHUB_DOCS_ENABLED=false`）——`/docs`、
  `/redoc`、`/openapi.json` 默认开启且不走鉴权，完整 API 结构会向任何可达
  主体披露；
- `/health` 保持无鉴权（连接测试用途，不访问上游）。

## 可观测性备忘（cluster 立项时规划，Info 级）

当前 `single` 模式的可观测性仅访问日志（request_id/方法/路径/状态码/耗时，
见 `api/middleware.py`）与 Provider 级 warning，无指标暴露——单进程单用户
最小运行模式下可接受，**不属当前范围**；以下备忘在 cluster 模式立项时作为
设计输入启动，不提前实施：

- **触发条件**：多实例部署 / 对外生产暴露（与 `deployment_mode` 扩展同步立项）；
- **暴露面**：`/metrics`（Prometheus 格式）或结构化日志采集，二选一先定采集栈；
- **指标清单**：请求量与错误率（按端点 × 错误码）、上游调用延迟（按 Provider）、
  上游 429/超时率、`CLIENT_RATE_LIMITED` 命中数、SSE 活跃客户端数、各 Provider
  线程池占用（`adapter.thread_limiter` 已有只读挂点：`total_tokens` /
  `borrowed_tokens`）、tqsdk Runtime 生命周期状态与代际（`runtime_status` 已可
  经能力目录读取）、WS Runtime 存活；
- **标签纪律**：不以证券代码、用户 ID、完整 URL 等做高基数标签；
- **告警候选**：上游错误率突增、入站限流持续打满、启动期 Provider 静默下线
  （`load_error` / 缺凭据 warning）；
- **cluster 联动决策点**：入站限流计数为进程内存（固定窗口不跨进程，
  见 `TAPEHUB_CLIENT_RATE_LIMIT_PER_MINUTE`）——多实例下每实例独立窗口，
  届时需一并决策集中式限流或按实例配额。
