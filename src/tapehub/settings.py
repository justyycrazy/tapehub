"""服务配置（pydantic-settings，环境变量前缀 TAPEHUB_）。"""

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """服务配置。

    - ``max_limit``：统一分页最大 limit；
    - ``provider_enabled_overrides``：按 provider 名覆盖 manifest 的启用状态。
    """

    model_config = SettingsConfigDict(env_prefix="TAPEHUB_", env_file=".env", extra="ignore")

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

    @model_validator(mode="after")
    def _validate_limits(self) -> "Settings":
        if self.max_limit < 1:
            raise ValueError("max_limit must be >= 1")
        return self
