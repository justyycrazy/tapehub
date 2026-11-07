# AGENTS.md — tapehub 开发指南

## 项目定位

通用行情数据网关：对外统一 REST API（FastAPI），内部经适配器接入多个行情数据源
（akshare、tushare、tickflow、alphafeed、tqsdk、twelvedata）。
**当前版本做「历史 K 线查询」与「实时行情（tqsdk 订阅推送、twelvedata WS 订阅：
快照 + SSE 流式，限 `TAPEHUB_DEPLOYMENT_MODE=single` 单进程单用户模式）」**：
明确不做存储/缓存、定时任务、面向客户端的双向 WebSocket、多进程/分布式部署、
Docker、前端（上游内部长连接不受此限，如 twelvedata WS）。

`README.md` 是最完整的行为规范（API 参数约定、错误码表、各 Provider 上游映射表、
单位换算），改动敏感区域前先读对应章节。

## 常用命令

```bash
uv sync --dev                       # 同步依赖（Python 3.12，uv 管理）
uv sync --dev --extra akshare       # Provider SDK 为可选依赖：
                                    # akshare/tickflow/alphafeed/tushare/tqsdk/twelvedata
uv run pytest                       # 全部测试（Mock 桩注入，不联网、不落盘、无需真实凭据）
uv run ruff check .                 # lint（line-length 100，规则 E/F/I 含 import 排序）
uv run uvicorn tapehub.api.app:app --reload   # 本地启动 127.0.0.1:8000
```

测试与本地环境解耦：测试代码一律用 `tests.conftest.make_settings` 构造 Settings
（不读 `.env`），autouse fixture 会清空进程环境中的 `TAPEHUB_*` 变量；本地
`.env` 里的真实凭据不影响 `uv run pytest`。服务运行路径不受影响（仍读 `.env`）。

## 架构边界（tests/contract/test_architecture.py 用源码扫描静态强制）

依赖方向：API 层（`api/`）→ Application 层（`application/`）→ Domain 层（`domain/`）；
Provider 适配器实现 Domain Port。

- `domain/`：禁止 import `fastapi`、`yaml`、`uvicorn`、`tapehub.providers`、`tapehub.api`；
- `application/`：禁止依赖 FastAPI；**源码文本中不得出现任何 Provider 名称**（如 "akshare"），
  即不允许 Provider 特定分支；
- `api/`：禁止 import Provider SDK 或 `tapehub.providers.<name>`；
- 适配器（`providers/<name>/adapter.py`）：禁止依赖 FastAPI；SDK 必须**延迟导入**
  （模块顶层不得出现 `import <sdk>` / `from <sdk>`）；同步 SDK 调用统一用
  `call_in_thread` 隔离到线程池；不得依赖 FastAPI 请求上下文（未来后台 Worker 复用）。

## 两级路由

- 公共 `ProviderRouter` 按 provider × data_type × market × asset_type ×
  interval × adjust × **variant** 选 Provider；
- 选中后适配器按自身 manifest 的 `operations` 做内部二级路由，match 必须**显式写全六个维度**；
  上游函数名不暴露到公共 API；
- 能力目录（manifest `capabilities`）是唯一事实来源，`/v1/capabilities` 与路由复用同一份；
- **缺省路由**（auto、多 Provider 覆盖同一组合时）由
  `src/tapehub/config/provider_routing.yaml` 集中裁决：三维 match 全显式 +
  完整回退链，凡能力覆盖的组合必须恰有条目（缺失/重复/order 引用未知或
  未声明该能力的 Provider 都启动失败）；代码内注册顺序不承载路由语义；
- **凭据驱动的注册**：需凭据的 Provider（`.env` 变量全部非空才启用）凭据缺失时
  不启用——能力目录仍可见，auto 路由跳过，显式请求返回 `PROVIDER_DISABLED`
  （details 携带缺失变量名）；显式 `TAPEHUB_PROVIDER_ENABLED_OVERRIDES` 优先于
  凭据推断。

## 新增 / 接入 Provider 的最小改动集

1. 若与既有 Provider 能力重叠：在 `src/tapehub/config/provider_routing.yaml`
   相应组合的回退链中显式插入本 Provider 的位置（注册表构建期强制校验覆盖完整性）；
2. 新建 `src/tapehub/providers/<name>/`：
   - `manifest.yaml`：能力声明 + operations 映射（未核实的历史覆盖 `history` 保持 null）；
   - `adapter.py`：继承 `BaseProviderAdapter`，`implemented = True`，凭据从 `settings` 读；
   - `request_mapper.py`：所有参数**显式传给上游**，不依赖上游默认值；
   - `response_mapper.py`：缺失值 → `null`（不用 0 冒充）；Provider 特有字段只经
     `extensions` 白名单透出；单位按官方文档换算；
3. `src/tapehub/providers/registry.py` 的 `BUILTIN_PROVIDERS` 追加注册名称
   （顺序仅初始化序；需凭据的 Provider 同步登记 `_REQUIRED_CREDENTIALS`）；
4. `pyproject.toml` `[project.optional-dependencies]` 添加 SDK；
5. 测试：`tests/unit/test_<name>_provider.py`、`tests/contract/test_<name>_chain.py`，
   SDK Mock 桩放 `tests/fixtures/providers/stub_<name>.py`（经 `sys.modules` 注入）；
6. `README.md` 补充该 Provider 的映射表。
   Application/API 层与其它 Provider 不需要任何改动。

## 数据与 API 约定（详见 README「请求规范」）

- instrument 标准代码 `{market}-{exchange}-{symbol}` 全大写（如 `CN-SSE-600000`），
  Provider 专用代码（`sh600000`、`CU2401.SHF`）由适配器转换；
- interval 用 ISO 8601 Duration：`PT1M`（1 分钟）≠ `P1M`（1 月），周期代码之间不做换算；
- `asset_type` 必填不推断：stock/index/etf/contract/continuous/weighted-index/option/forex/crypto；
- `adjust` 声明默认值按资产类型：stock → `qfq`（前复权），其余 → `none`；
- 时间范围闭区间 `[start, end]`；输入时间必须带时区偏移（拒绝裸时间与仅日期）；
  项目内部统一北京时间：服务级时间戳一律北京时间（`+08:00`）；行情数据时间戳
  全市场统一北京时间（GLOBAL 上游墙钟口径由适配器解析转换，如 twelvedata
  外汇 Australia/Sydney、加密货币 UTC，2026-11-04 探针实测确认）；
- 结果先按 timestamp 升序排序，再应用 offset/limit；不返回 total；
- 实时快照（`GET /v1/market/quotes`，M2）：无 interval/时间范围/分页/adjust
  （快照是当前时点的订阅数据）；能力目录与 operations match 对无意义维度
  显式空/null，路由跳过对应维度；首包超时 `QUOTE_NOT_READY`（HTTP 408）；
  tqsdk 不支持取消订阅——Runtime 退订只停本地维护，休眠订阅累计到
  `TAPEHUB_TQSDK_SUBSCRIPTION_LIMIT` 触发新一代重建清理（活跃订阅自动重订）；
  twelvedata 走 WS 共享订阅（M2/M3 同源，探针实证 credit=并发活跃 symbol 数），
  冷订阅等首包、活跃读缓存，引用归零延迟 60s 真退订释放 credit，
  并发达 `TAPEHUB_TWELVEDATA_SUBSCRIPTION_LIMIT` 返回
  `UPSTREAM_SUBSCRIPTION_LIMIT`（429）。
- 实时快照流（`GET /v1/market/quotes/stream`，M3，SSE `text/event-stream`）：
  事件三类 `quote`/`status`（subscribed / upstream_disconnected / recycling /
  idle_unsubscribed）/`heartbeat`；断线与代际回收必须发明确 status 事件，
  **绝不静默断流**；coalesce 丢旧保新**只约束 quote 帧**（status 生命周期
  帧不丢）；心跳/空闲退订/客户端容量闸门（`STREAM_LIMIT_REACHED` 429）在
  Application 层流服务（`QuoteStreamService`，Provider 无关）；tqsdk 仅
  `shared` 连接模式支持；twelvedata WS 支持逐 symbol 真退订（无代际回收、
  不产生 `recycling` 事件），流关闭即本地退订（上游订阅按 Provider 语义：
  tqsdk 保留至回收 / twelvedata 经 linger 真退订）。

## 安全与诚实边界

- Provider 凭据只经环境变量（前缀 `TAPEHUB_`，见 `.env.example`）注入；
  禁止写入代码、manifest.yaml、日志、异常响应或测试 fixture；`.env` 不提交版本库；
- 不编造能力：上游接口签名/错误码未核实的，如实标注（`history` 保持 null，
  异常统一包装为 `UPSTREAM_ERROR`）；绝不返回写死的模拟行情；
- 日志不记录 Provider 密钥与完整行情响应；SDK 异常不穿透 API。

## 数据探针（probes/）

- `probes/` 是独立的离线诊断工具：用真实上游调用实证核实能力目录声明，
  产物写入 `probes/results/`（gitignore，不入库）；
- 不属于服务运行时，不进入 CI：`uv run pytest` 保持零网络、零真实凭据
  （探针框架自身测试在 `tests/unit/probes/`，全部离线）；
- 运行：`uv run python -m probes.cli list`（零网络）／
  `run --provider <name> --dry-run` ／ `run --provider <name> --yes`（真实调用，消耗配额）；
- 探针文件规范与类型语义见 `probes/README.md`；新增 Provider 需同步新增
  `probes/cases/<name>.yaml`；框架代码保持 Provider 无关，Provider 专属事实
  只允许出现在 cases 文件里；
- 探针只产出报告；manifest 能力回填必须人工确认后单独提交；
- 凭据只经环境变量；结果文件不记录凭据值（只记布尔），样本数据每 case 最多 3 根 bar。

## 语言与提交

- 代码注释、docstring、README、commit message 均使用中文；
- commit message 描述业务接入内容（如「接入 Twelve Data Provider：外汇与加密货币」）。
