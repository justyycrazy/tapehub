"""服务配置（pydantic-settings，环境变量前缀 TAPEHUB_）。"""

from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """服务配置。

    - ``deployment_mode``：部署模式守门字段。当前版本仅支持 ``single``
      （单进程、单用户最小运行模式）；其他值在启动时直接失败——该字段
      的职责是防误用与记录意图，不承诺任何未实现的能力。
    - ``max_limit``：统一分页最大 limit；
    - ``provider_enabled_overrides``：按 provider 名覆盖启用状态（两级裁决：显式覆盖 > 凭据推断）；
    - ``routing_config_path``：缺省路由顺序配置文件路径；None 用包内内置版本
      （外部文件整体替换内置版本，不做合并）。
    """

    model_config = SettingsConfigDict(env_prefix="TAPEHUB_", env_file=".env", extra="ignore")

    deployment_mode: Literal["single"] = "single"
    # tqsdk 连接模式：shared（进程内唯一长连接 Runtime，懒启动）为默认／
    # per_request（每请求独立实例，回滚配置）
    tqsdk_conn_mode: Literal["per_request", "shared"] = "shared"
    # shared 模式的在飞取数上限（并发闸门，超出返回 CONCURRENCY_LIMIT）
    tqsdk_max_inflight_fetch: int = Field(default=16, ge=1)
    # 实时快照首包等待超时（秒）：订阅建立后上游未在该窗口内推送首包
    # 即返回 QUOTE_NOT_READY（HTTP 408）
    tqsdk_first_snapshot_timeout: float = Field(default=5.0, gt=0)
    # 休眠订阅回收阈值：tqsdk 不支持取消订阅，退订只停止本地快照维护，
    # 上游推送仍在消耗带宽；休眠订阅累计到该值触发新一代重建清理
    # （仍被引用的订阅自动重订）
    tqsdk_subscription_limit: int = Field(default=64, ge=1)
    # Twelve Data WS 并发订阅 symbol 上限（超出 UPSTREAM_SUBSCRIPTION_LIMIT 429）。
    # 免费版 8 个 credit，退订/断连即时释放；升档后调大该值即可
    twelvedata_subscription_limit: int = Field(default=8, ge=1)
    # Twelve Data 实时快照冷订阅首包等待超时（秒）：超时返回 QUOTE_NOT_READY
    # （HTTP 408）；已活跃订阅直接读缓存即时返回
    twelvedata_first_snapshot_timeout: float = Field(default=5.0, gt=0)
    # Tiingo WS 并发订阅 symbol 软上限（超出 UPSTREAM_SUBSCRIPTION_LIMIT 429）。
    # 上游未公布并发订阅数上限（硬约束为月度 500 个唯一品种，网关不做计数，
    # 由 docs/providers/tiingo.md 限额说明与上游超限报错兜底）；默认 8 与
    # twelvedata 对齐防失控
    tiingo_subscription_limit: int = Field(default=8, ge=1)
    # Tiingo 实时快照冷订阅首包等待超时（秒）：超时返回 QUOTE_NOT_READY
    # （HTTP 408）；已活跃订阅直接读缓存即时返回
    tiingo_first_snapshot_timeout: float = Field(default=5.0, gt=0)
    # —— 实时快照流（SSE，M3）——
    # 并发流客户端上限（超出 STREAM_LIMIT_REACHED 429）
    stream_max_clients: int = Field(default=8, ge=1)
    # 每客户端事件队列长度：队列满丢旧保新（行情「最新」比「完整」有价值）
    stream_queue_size: int = Field(default=64, ge=1)
    # 空闲心跳间隔（秒）：无事件时按该周期发送保活帧
    stream_heartbeat_seconds: float = Field(default=15.0, gt=0)
    # 流空闲超时（秒）：该窗口内无行情事件（心跳不算）则本地退订并结束流
    stream_idle_timeout_seconds: float = Field(default=300.0, gt=0)
    # 上游调用整体超时（秒）：bars/quotes 单次请求从发起到返回的上限，
    # 超时返回 UPSTREAM_TIMEOUT（HTTP 504）。SSE 流为长驻设计不适用此值。
    # 注意：tqsdk shared 模式经事件循环等待（取消即时生效）；线程型调用
    # （其余 Provider 与 tqsdk per_request）经 call_in_thread 的可放弃语义
    # 取消即时返回，工作线程在后台跑完当前上游调用后自行退出
    upstream_timeout_seconds: float = Field(default=30.0, gt=0)
    # 线程池容量（anyio 默认 limiter 的 total_tokens）：全部线程型上游调用
    # 共享该容量；akshare/baostock 等 SDK 上游 socket 无超时，挂死调用会
    # 占用容量至自然结束——调大是缓解手段，根治需上游超时或按 Provider
    # 分组限流（当前依赖上游报错与监控告警兜底）
    thread_pool_size: int = Field(default=40, ge=1)
    version: str = "0.1.0"
    api_version: str = "v1"
    catalog_version: str = "1"
    max_limit: int = Field(default=5000, ge=1)
    provider_enabled_overrides: dict[str, bool] = Field(default_factory=dict)
    # 缺省路由顺序配置（provider_routing.yaml）：None 用包内内置版本；
    # 外部文件整体替换内置版本，不做合并
    routing_config_path: str | None = None
    # Provider 凭据：只能通过环境变量提供，禁止写入代码/manifest/日志
    tickflow_api_key: str = ""
    alphafeed_api_key: str = ""
    tushare_token: str = ""
    # TqSdk 快期账户（TqAuth）：用户名/邮箱与密码
    tqsdk_auth_user: str = ""
    tqsdk_auth_password: str = ""
    tiingo_api_key: str = ""
    twelvedata_api_key: str = ""
    # 同花顺金融数据 API Key（X-api-key 请求头；fuyao.aicubes.cn）
    ths_api_key: str = ""
    # 富途 OpenAPI（webapi.futunn.com）「方式二：传统 API Key」凭据：控制台
    # AppKey 管理创建的 AppKey ID，及与其公钥（Ed25519/RSA-SHA256）配对的
    # 本地私钥 PEM 文件路径；两者必须同时配置（任一缺失即不启用）
    futunn_app_key: str = ""
    futunn_private_key_path: str = ""

    @model_validator(mode="after")
    def _validate_limits(self) -> "Settings":
        if self.max_limit < 1:
            raise ValueError("max_limit must be >= 1")
        return self
