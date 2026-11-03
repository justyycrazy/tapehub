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
