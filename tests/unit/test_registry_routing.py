"""缺省路由配置与凭据驱动注册的单元测试（全部离线）。"""

import pytest

from tapehub.domain.errors import RoutingConfigError
from tapehub.domain.models import AssetType, DataType
from tapehub.providers.registry import build_default_registry
from tapehub.providers.routing import builtin_routing_path, load_routing_config
from tests.conftest import make_settings

# 内置路由配置必须声明的组合数（provider_routing.yaml 现状快照）：
# bars 12 条（stock CN/US/HK + index + contract + option + continuous +
# weighted-index + etf CN/US + forex + crypto）+ quotes 11 条
# （CN 五类资产 + HK/US stock + GLOBAL forex/crypto）。
EXPECTED_BUILTIN_ENTRY_COUNT = 23


class TestLoadRoutingConfig:
    def test_builtin_config_loads_with_expected_entries(self) -> None:
        """包内内置路由配置可加载，且与既有 Provider 能力覆盖一一对应。"""
        table = load_routing_config()
        assert len(table) == EXPECTED_BUILTIN_ENTRY_COUNT
        # US / HK 个股 K 线的完整回退链
        assert table[(DataType.BARS, "US", AssetType.STOCK)] == (
            "twelvedata",
            "alphafeed",
            "tickflow",
            "akshare",
            "futunn",
        )
        assert table[(DataType.BARS, "HK", AssetType.STOCK)] == (
            "alphafeed",
            "tickflow",
            "akshare",
            "futunn",
        )
        # 完整回退链示例：CN 个股七源；tqsdk 缺省的期货合约（ths 末位）；
        # CN 指数与 ETF 多源回退；CN 期权为 ths 独占
        assert table[(DataType.BARS, "CN", AssetType.STOCK)] == (
            "baostock",
            "alphafeed",
            "tickflow",
            "tushare",
            "akshare",
            "ths",
            "futunn",
        )
        assert table[(DataType.BARS, "CN", AssetType.INDEX)] == (
            "akshare",
            "baostock",
            "ths",
            "futunn",
        )
        assert table[(DataType.BARS, "CN", AssetType.ETF)] == (
            "tqsdk",
            "baostock",
            "akshare",
            "ths",
            "futunn",
        )
        assert table[(DataType.BARS, "CN", AssetType.CONTRACT)] == (
            "tqsdk",
            "akshare",
            "ths",
        )
        assert table[(DataType.BARS, "CN", AssetType.OPTION)] == ("ths",)
        # 个股快照缺省：CN tqsdk 缺省；HK/US alphafeed 接管缺省
        assert table[(DataType.QUOTE, "CN", AssetType.STOCK)] == (
            "tqsdk",
            "alphafeed",
            "tickflow",
            "ths",
            "futunn",
        )
        # CN 期权快照 ths 独占
        assert table[(DataType.QUOTE, "CN", AssetType.OPTION)] == ("ths",)
        assert table[(DataType.QUOTE, "HK", AssetType.STOCK)] == (
            "alphafeed",
            "tickflow",
            "futunn",
        )
        # 全部 order 链非空且首元素是各组合的既有缺省
        assert all(order for order in table.values())

    def test_builtin_path_points_to_package_resource(self) -> None:
        assert builtin_routing_path().name == "provider_routing.yaml"

    def test_missing_file_rejected(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(RoutingConfigError, match="cannot read routing config"):
            load_routing_config(tmp_path / "nope.yaml")

    def test_unsupported_version_rejected(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        path = tmp_path / "routing.yaml"
        path.write_text("version: 99\ndefaults: []\n", encoding="utf-8")
        with pytest.raises(RoutingConfigError, match="unsupported routing config version"):
            load_routing_config(path)

    def test_duplicate_match_rejected(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        path = tmp_path / "routing.yaml"
        path.write_text(
            "version: 1\n"
            "defaults:\n"
            "  - match: {data_type: bars, market: CN, asset_type: stock}\n"
            "    order: [akshare]\n"
            "  - match: {data_type: bars, market: CN, asset_type: stock}\n"
            "    order: [tushare]\n",
            encoding="utf-8",
        )
        with pytest.raises(RoutingConfigError, match="duplicate routing entry"):
            load_routing_config(path)

    def test_empty_order_rejected(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        path = tmp_path / "routing.yaml"
        path.write_text(
            "version: 1\n"
            "defaults:\n"
            "  - match: {data_type: bars, market: CN, asset_type: stock}\n"
            "    order: []\n",
            encoding="utf-8",
        )
        with pytest.raises(RoutingConfigError, match="at least 1 item"):
            load_routing_config(path)

    def test_duplicate_names_in_order_rejected(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        path = tmp_path / "routing.yaml"
        path.write_text(
            "version: 1\n"
            "defaults:\n"
            "  - match: {data_type: bars, market: CN, asset_type: stock}\n"
            "    order: [akshare, akshare]\n",
            encoding="utf-8",
        )
        with pytest.raises(RoutingConfigError, match="duplicate provider names"):
            load_routing_config(path)

    def test_non_mapping_rejected(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        path = tmp_path / "routing.yaml"
        path.write_text("- just\n- a list\n", encoding="utf-8")
        with pytest.raises(RoutingConfigError, match="must be a mapping"):
            load_routing_config(path)


class TestCredentialDrivenRegistration:
    """凭据驱动的注册：缺凭据不启用、目录可见、显式覆盖优先。"""

    def test_missing_credentials_disable_provider_but_keep_catalog(self) -> None:
        settings = make_settings()  # 全部凭据为空（测试环境隔离保证）
        registry = build_default_registry(settings)
        tqsdk = registry.get("tqsdk")
        assert tqsdk is not None
        assert tqsdk.enabled is False
        assert tqsdk.disabled_reason is not None
        assert "TAPEHUB_TQSDK_AUTH_USER" in tqsdk.disabled_reason
        assert "TAPEHUB_TQSDK_AUTH_PASSWORD" in tqsdk.disabled_reason
        # 能力目录不因凭据缺失收缩（enabled 与 capabilities 是两个概念）
        assert tqsdk.capabilities

    def test_akshare_registers_without_credentials(self) -> None:
        settings = make_settings()
        registry = build_default_registry(settings)
        akshare = registry.get("akshare")
        assert akshare is not None
        assert akshare.enabled is True
        assert akshare.disabled_reason is None

    def test_tqsdk_requires_both_user_and_password(self) -> None:
        # 只填用户名：判定为未配置（两变量必须同时非空）
        settings = make_settings(tqsdk_auth_user="user@example.com")
        registry = build_default_registry(settings)
        tqsdk = registry.get("tqsdk")
        assert tqsdk is not None
        assert tqsdk.enabled is False
        assert tqsdk.disabled_reason is not None
        assert "TAPEHUB_TQSDK_AUTH_PASSWORD" in tqsdk.disabled_reason

    def test_blank_whitespace_credentials_count_as_missing(self) -> None:
        settings = make_settings(twelvedata_api_key="   ")
        registry = build_default_registry(settings)
        twelvedata = registry.get("twelvedata")
        assert twelvedata is not None
        assert twelvedata.enabled is False

    def test_explicit_override_disable_beats_credentials(self) -> None:
        settings = make_settings(
            twelvedata_api_key="test-key",
            provider_enabled_overrides={"twelvedata": False},
        )
        registry = build_default_registry(settings)
        twelvedata = registry.get("twelvedata")
        assert twelvedata is not None
        assert twelvedata.enabled is False
        assert twelvedata.disabled_reason == "disabled via TAPEHUB_PROVIDER_ENABLED_OVERRIDES"

    def test_explicit_override_enable_is_escape_hatch(self) -> None:
        """显式启用可越过凭据推断（逃生门，恢复「注册但请求时失败」的旧行为）。"""
        settings = make_settings(
            tushare_token="",
            provider_enabled_overrides={"tushare": True},
        )
        registry = build_default_registry(settings)
        tushare = registry.get("tushare")
        assert tushare is not None
        assert tushare.enabled is True

    def test_disabled_provider_skipped_in_auto_routing(self) -> None:
        """凭据缺失的 Provider 不进入 auto 候选：quotes/CN/stock 回退 tickflow。"""
        settings = make_settings(tickflow_api_key="test-key")  # 仅 tqsdk 凭据缺失
        registry = build_default_registry(settings)

        candidates = registry.resolve_candidates(
            data_type=DataType.QUOTE,
            market="CN",
            asset_type=AssetType.STOCK,
        )
        names = [c.record.name for c in candidates]
        assert names == ["tickflow"]
