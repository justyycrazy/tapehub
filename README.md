# tapehub

通用行情数据网关：对外提供统一的 HTTP RESTful API，内部通过适配器接入多个行情数据源。

**当前版本实现「历史 K 线查询」与「实时行情（快照 + SSE 流式，限 `TAPEHUB_DEPLOYMENT_MODE=single` 单进程单用户模式）」两条业务链路，不建设行情数据存储平台。**

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

## 文档索引

| 文档 | 内容 |
| --- | --- |
| [docs/api.md](docs/api.md) | API 与数据约定：接口示例、请求规范（instrument/asset_type/variant/adjust/interval/时区/分页）、单位换算总则、错误码表、SSE 事件帧 |
| [docs/operations.md](docs/operations.md) | 运维与配置：依赖管理、凭据驱动注册、`TAPEHUB_*` 环境变量全表、安全纪律 |
| [docs/providers/](docs/providers/) | 各 Provider 独立文档（10 份）：上游映射表、标的代码映射、复权/单位换算、上游行为定案、路由参与、探针依据 |
| [AGENTS.md](AGENTS.md) | 开发指南：架构边界、常用命令、新增 Provider 流程、探针规范索引 |
| [probes/README.md](probes/README.md) | 数据探针框架规范：用例格式、运行方式、日志与输出 |
| [.env.example](.env.example) | 服务配置模板（复制为 `.env` 使用，不提交版本库） |

内置 Provider（10 个）：akshare、alphafeed、baostock、tickflow、ths、tiingo、tushare、tqsdk、twelvedata、futunn——各自文档见 [docs/providers/](docs/providers/)。

## 第一版边界

已实现：

- `GET /health`：健康检查（供其他项目配置 `base_url` 后测试连接）；
- `GET /v1/capabilities`：全部 Provider 能力目录；
- `GET /v1/providers/{provider}/capabilities`：单个 Provider 能力；
- `GET /v1/market/bars`：统一历史 K 线查询；
- `GET /v1/market/quotes`：实时快照（限 `single` 模式）：tqsdk（订阅-推送）、twelvedata/tiingo（WS 订阅缓存，GLOBAL 外汇/加密货币）、tickflow/ths/futunn（无状态 REST 快照）；
- `GET /v1/market/quotes/stream`：实时快照 SSE 流（单标的订阅推送、心跳与空闲退订、容量闸门，限 `single` 模式）：tqsdk（仅 `shared` 连接模式）、twelvedata、tiingo；
- `GET /v1/instruments/resolve`：标的模糊解析（宽松输入 → 标准代码候选，纯本地规则、零上游调用）；
- Provider 注册、能力匹配与公共路由；Provider 内部二级操作路由的扩展机制；
- 统一错误处理；
- 基于 Fake Provider 与 Mock SDK 的完整链路验证与单元/契约测试。

明确不实现（当前版本）：

- 数据库、Redis、行情缓存、CSV/Parquet 等行情落盘（实时链路的**进程内最新快照**除外——那是订阅状态，不是持久化）；
- 定时任务、全量转储 Worker、消息队列、面向客户端的双向 WebSocket（行情流式采用 SSE 单向推送）；
- 逐笔、标的元数据等其它查询接口；
- 多进程/分布式部署（`TAPEHUB_DEPLOYMENT_MODE` 为守门字段，当前仅支持 `single`，其他值启动即失败）；
- Docker 与前端页面。

## 快速开始

使用 [uv](https://docs.astral.sh/uv/) 管理项目，Python 3.12。

```bash
uv sync --dev --all-extras       # 同步依赖 + 全部 Provider SDK（务必 --all-extras，见 operations.md）
cp .env.example .env             # 填写 Provider 凭据（不填写则对应 Provider 不启用，服务仍可启动）

uv run pytest                    # 运行测试（零网络、零真实凭据）
uv run uvicorn tapehub.api.app:app --reload   # 启动服务（默认 127.0.0.1:8000）
```

依赖管理细节、凭据驱动注册语义与全部环境变量见 [docs/operations.md](docs/operations.md)；连接测试与接口示例见 [docs/api.md](docs/api.md)。

## 总体设计

```text
API 层 → Application 层 → Domain 层
                         ↑
        Provider 适配器实现 Domain Port
```

- **能力目录是唯一事实来源**：`/v1/capabilities` 与公共路由复用同一份目录；
- **两级路由**：公共 `ProviderRouter` 按 provider × data_type × market × asset_type × interval × adjust × **variant** 选 Provider；适配器按 manifest 的 `operations` 做内部二级路由，上游函数名不暴露到公共 API；
- **auto 路由优先级（显式声明，非隐式顺序）**：显式 `provider=X` > 缺省路由配置 `src/tapehub/config/provider_routing.yaml`（可用 `TAPEHUB_ROUTING_CONFIG` 指向外部文件整体替换内置版本）的完整回退链。该文件按 `(data_type, market, asset_type)` 三维全显式声明每组合的 Provider 回退顺序（首个「已注册、已启用且能力可解析」者为缺省；未启用条目自动跳过；回退只发生在路由选择阶段，请求执行失败不切换 Provider）；注册表构建期强制校验**凡能力覆盖的组合必须恰有条目**、order 引用的 Provider 必须已注册且实际声明该组合能力（缺失/重复/未知引用都拒绝启动）；代码内注册顺序不承载路由语义；
- **凭据驱动的注册**：需要凭据的 Provider，`.env` 相应变量全部非空（strip 后）才启用；凭据缺失的 Provider **能力目录仍可见但 `enabled=false`**，auto 路由跳过，显式请求返回 `PROVIDER_DISABLED`（details 携带缺失的变量名）。akshare/baostock 无凭据概念恒启用。启用裁决与逃生门细节见 [docs/operations.md](docs/operations.md)；
- **manifest 属于配置**：缺失、格式错误或校验失败直接启动失败（fail-fast）；路由配置同上；适配器导入失败（可选 SDK 未安装等）仍单 Provider 隔离，不影响启动；
- **公共能力声明 vs 内部 operation 映射**：manifest 的 `capabilities` 面向目录与公共路由（含 `variants` 声明）；`operations` 的 match 键包含全部六个维度且必须显式声明，面向适配器内部；对请求无意义的维度显式声明 `null`（如 quotes 快照的 interval/adjust，两维度在能力目录同样声明为空、路由时跳过），`null` 只与不携带该维度的请求匹配。

数据探针（`probes/`）用真实上游调用实证核实能力目录声明，能力结论回填于各 Provider 文档；探针规范见 [probes/README.md](probes/README.md)。

## 未来扩展边界（现在不实现）

- **定时全量转储**：适配器不依赖 FastAPI 请求上下文，未来 Worker 可复用统一查询与分页模型逐批拉取；
- **新资产类型**：扩展标准枚举、能力清单与对应操作映射即可；
- 第一版不存储任何行情数据。

## 开发

新增 / 接入 Provider 的最小改动集、架构边界（静态强制的依赖方向）与测试约定见 [AGENTS.md](AGENTS.md)。
