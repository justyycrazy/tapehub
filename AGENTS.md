# AGENTS.md — tapehub 开发指南

## 项目定位

通用行情数据网关：对外统一 REST API（FastAPI），内部经适配器接入多个行情数据源。
**当前版本做「历史 K 线查询」与「实时行情（tqsdk 订阅推送、twelvedata/tiingo WS 订阅：
快照 + SSE 流式，限 `TAPEHUB_DEPLOYMENT_MODE=single` 单进程单用户模式）」**：
明确不做存储/缓存、定时任务、面向客户端的双向 WebSocket、多进程/分布式部署、
Docker、前端（上游内部长连接不受此限，如 twelvedata WS）。

文档体系与总体设计见 `README.md`；API 与数据约定（请求规范、错误码表、单位换算）
见 `docs/api.md`；运维与配置见 `docs/operations.md`；各 Provider 上游映射与行为
定案见 `docs/providers/<name>.md`；数据探针框架规范与设计决策记录见
`docs/probes.md`——改动敏感区域前先读对应文档。

## 常用命令

```bash
uv sync --dev --all-extras          # 同步依赖 + 全部 Provider SDK（Python 3.12，uv 管理）
                                    # 注意：uv 默认「精确同步」，只 --extra 部分 SDK
                                    # 会移除其余 Provider SDK；SDK 间解析无冲突，
                                    # Provider SDK 为可选依赖，务必用 --all-extras 共存
uv run pytest                       # 全部测试（Mock 桩注入，不联网、不落盘、无需真实凭据）
uv run ruff check .                 # lint（line-length 100，规则 E/F/I 含 import 排序）
uv run uvicorn tapehub.api.app:app --reload   # 本地启动 127.0.0.1:8000
```

### 全量测试结果获取预案（Windows / Git Bash 环境）

全量 `uv run pytest` 超过默认 120s 超时；且**经管道会丢失 pytest 末尾汇总
行，`| tail` 等管道还会掩盖 pytest 真实退出码**（管道退出码是末段命令的）。
判定依据 = **退出码 + 完整日志**，汇总行与 FAILED/ERROR 清单三路互证：

1. **直接以后台方式启动**，不要先试前台等超时被转后台；完整日志重定向
   落盘，不经管道（无管道 → 任务完成通知里的退出码就是 pytest 自身的）。
   **命令行不要再加 `-q`**——pyproject 的 `addopts = "-q"` 已带一个，叠加
   成 `-qq` 会把末尾汇总行整个压掉（此前「汇总行消失」的根因）：
   ```bash
   uv run pytest > pytest_out.log 2>&1
   ```
2. **综合判定**（退出码为主，汇总行与报错清单互证）：
   ```bash
   echo $?                                    # 0 = 全部通过；1 = 有 failed；2 = 参数/收集问题
   tail -1 pytest_out.log                     # 汇总行：N passed, M skipped in Xs
   grep -nE "^FAILED|^ERROR" pytest_out.log   # 失败/错误清单（无输出 = 无失败）
   ```
   三路信号应一致：退出码 0 ⇔ 汇总行无 failed/error 字样 ⇔ grep 无输出；
   退出码非 0 时按日志内 traceback（完整留存）定位。
3. 判定完删除 `pytest_out.log`（不入库）。

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

## 路由与配置机制（要点）

路由模型与缺省路由裁决细节见 `README.md`「总体设计」；回退链数据见
`src/tapehub/config/provider_routing.yaml`（三维 match 全显式 + 完整回退链，
凡能力覆盖的组合必须恰有条目，缺失/重复/未知引用启动失败）。

- **凭据驱动的注册**：需凭据的 Provider（`.env` 变量全部非空才启用）凭据缺失时
  不启用——能力目录仍可见，auto 路由跳过，显式请求返回 `PROVIDER_DISABLED`
  （details 携带缺失变量名）；显式 `TAPEHUB_PROVIDER_ENABLED_OVERRIDES` 优先于
  凭据推断（细则见 `docs/operations.md`）。

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
6. 文档：新建 `docs/providers/<name>.md`（统一模板参照既有 Provider 文档），
   并新增 `probes/cases/<name>.yaml`。
   Application/API 层与其它 Provider 不需要任何改动。

接入所需的最小文件集合：

```text
src/tapehub/providers/<name>/manifest.yaml
src/tapehub/providers/<name>/adapter.py
src/tapehub/providers/<name>/request_mapper.py   # 新增
src/tapehub/providers/<name>/response_mapper.py  # 新增
pyproject.toml
docs/providers/<name>.md
probes/cases/<name>.yaml
tests/                                           # 该适配器的 Mock 测试
```

## 数据与 API 约定

请求规范（instrument 标准代码、asset_type 必填体系、variant/adjust/interval 语义、
时间边界与时区、分页）、错误码表、SSE 事件帧语义的唯一出处是 `docs/api.md`；
各 Provider 的上游映射与行为差异的唯一出处是 `docs/providers/<name>.md`。
开发时直接查阅这两处，不要在代码注释里复述其内容（注释只写文档指针）。

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
- **聚焦目录方案**（只验证某一主题——如某资产类型/某几所扩容——避免全量跑；
  照此执行，无需再行验证）：
  1. 目录 `probes/results/_cases-focus-<主题>/`（gitignore 内，不入库）。
     **必须含全部 BUILTIN_PROVIDERS 同名 yaml**（loader 对缺失/多余一律报错）——
     非聚焦 Provider 直接 `cp probes/cases/<name>.yaml` 占位即可，不被
     `--provider` 选中就不会执行；
  2. 聚焦 Provider 文件自规范文件裁剪：`targets` 只留聚焦主题的目标键——
     chain `include_declared: true` 只为「有目标键」的声明组合展开 case，
     无目标键的能力组合不生成 case；分析探针（adjust_diff/history_depth 等）
     按需保留或整段删除；raw 段只留聚焦主题 case；
  3. 预算按聚焦轮实际构成重算 `max_calls`（client 构造与每条 call 各计 1 调，
     close 不计），注释写明构成；
  4. 零网络校验两步：`list --cases-dir <目录> --provider <聚焦>`（schema/
     展开/预算核对）→ `run ... --dry-run`；然后 `run --cases-dir <目录>
     --provider <聚焦> --yes` 真实运行（多 Provider 各自独立调用与
     `--log-file`；调用链某步异常即中止后续调用——目录探测与直测须分列 case）；
  5. 迭代复测：聚焦文件是即用即弃工件，可按首轮证据就地改候选/收窄 case
     复跑；跑完照常追加 PROBE_LOG 轮次条目（注明聚焦目录）；定案后的规范
     文件修订与 manifest 回填照常走人工确认、单独提交；
- 运行日志统一 logging：默认输出 case 生命周期与每次上游调用的子步骤行，
  `-v` 开调试细节、`--log-file` 全量落盘；级别语义与格式规范见
  `docs/probes.md`「日志与输出」；
- 探针文件规范、类型语义与设计决策记录见 `docs/probes.md`；框架代码保持
  Provider 无关，Provider 专属事实只允许出现在 cases 文件里；
- **每轮探针跑完后，把该轮条目追加到 `probes/results/PROBE_LOG.md`**
  （对应 Provider 小节最上方，格式见该文件「记录格式」；不入库）；
- 探针只产出报告；manifest 能力回填必须人工确认后单独提交，回填写入
  `docs/providers/<name>.md` 与对应 manifest 数据面；
- 凭据只经环境变量；结果文件不记录凭据值（只记布尔），样本数据每 case 最多 3 根 bar。

## 语言与提交

- 代码注释、docstring、文档、commit message 均使用中文；
- commit message 描述业务接入内容（如「接入 Twelve Data Provider：外汇与加密货币」）。
