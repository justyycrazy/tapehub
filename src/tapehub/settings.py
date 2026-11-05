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
    - ``provider_enabled_overrides``：按 provider 名覆盖 manifest 的启用状态。
    """

    model_config = SettingsConfigDict(env_prefix="TAPEHUB_", env_file=".env", extra="ignore")

    deployment_mode: Literal["single"] = "single"
    # tqsdk 连接模式：shared（进程内唯一长连接 Runtime，懒启动；2026-11-05
    # 15.3h 观察期验证后为默认）／ per_request（每请求独立实例，回滚配置）
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
    version: str = "0.1.0"
    api_version: str = "v1"
    catalog_version: str = "1"
    max_limit: int = Field(default=5000, ge=1)
    provider_enabled_overrides: dict[str, bool] = Field(default_factory=dict)
    # Provider 凭据：只能通过环境变量提供，禁止写入代码/manifest/日志
    tickflow_api_key: str = ""
    alphafeed_api_key: str = ""
    tushare_token: str = ""
    # TqSdk 快期账户（TqAuth）：用户名/邮箱与密码
    tqsdk_auth_user: str = ""
    tqsdk_auth_password: str = ""
    twelvedata_api_key: str = ""

    @model_validator(mode="after")
    def _validate_limits(self) -> "Settings":
        if self.max_limit < 1:
            raise ValueError("max_limit must be >= 1")
        return self
