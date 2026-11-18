"""缺省路由配置与凭据驱动注册的单元测试（全部离线）。

路由顺序是**可调配置**：只要满足加载期校验（能力覆盖完整、引用合法、
链非空），任何顺序都是合法配置，不得导致测试失败。因此本文件：

- 不断言内置 yaml 的具体链序 / 条目数（那是配置快照，不是行为契约）；
  覆盖完整性与引用合法性由注册表构建期校验强制，此处只验「能加载 +
  链非空 + 引用是内置 Provider」的不变量；
- auto 路由的**机制**（按链序裁决、停用者跳过）用「内置配置整体逆序」
  的合成配置验证，期望序列从合成配置与注册表启用状态推导得出——
  内置 yaml 无论怎么调整顺序，本文件全部通过。
"""

from pathlib import Path

import pytest
import yaml

from tapehub.domain.errors import RoutingConfigError
from tapehub.domain.models import AssetType, DataType
from tapehub.providers.registry import BUILTIN_PROVIDERS, build_default_registry
from tapehub.providers.routing import builtin_routing_path, load_routing_config
from tests.conftest import make_settings


class TestLoadRoutingConfig:
    def test_builtin_config_loads_with_valid_chains(self) -> None:
        """包内内置路由配置可加载，且全部回退链满足机制不变量。"""
        table = load_routing_config()
        assert table
        for key, order in table.items():
            assert order, f"empty chain for {key}"
            for name in order:
                assert name in BUILTIN_PROVIDERS, f"unknown provider {name!r} for {key}"

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
        """凭据缺失的 Provider 不进入 auto 候选：候选 = 链内启用者（按链序）。"""
        settings = make_settings(tickflow_api_key="test-key")  # 仅 tickflow 凭据非空
        registry = build_default_registry(settings)

        chain = load_routing_config()[(DataType.QUOTE, "CN", AssetType.STOCK)]
        expected = [name for name in chain if registry.get(name).enabled]
        assert expected  # 该组合在仅 tickflow 凭据下仍有可用者，否则测试失去意义

        candidates = registry.resolve_candidates(
            data_type=DataType.QUOTE,
            market="CN",
            asset_type=AssetType.STOCK,
        )
        assert [c.record.name for c in candidates] == expected


def _write_permuted_routing(tmp_path: Path, *, reverse: bool) -> tuple[Path, list[dict]]:
    """把内置路由配置的全部回退链整体逆序（或保持原序）写成临时配置文件。

    组合集与成员不变、只改顺序——仍是合法配置，加载期校验可通过。
    返回（文件路径, defaults 原始条目列表），供期望值推导。
    """
    raw = yaml.safe_load(builtin_routing_path().read_text(encoding="utf-8"))
    if reverse:
        for entry in raw["defaults"]:
            entry["order"] = list(reversed(entry["order"]))
    path = tmp_path / ("routing_reversed.yaml" if reverse else "routing_orig.yaml")
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return path, raw["defaults"]


class TestAutoRoutingFollowsChainOrder:
    """auto 路由按回退链序裁决的机制测试（合成置换配置，不绑定内置顺序）。

    同一批 Provider、不同链序 → 候选序列随之改变；期望序列从合成配置与
    注册表启用状态推导，因此内置 yaml 无论怎么调整顺序本类都通过。
    """

    @pytest.mark.parametrize("reverse", [False, True], ids=["orig", "reversed"])
    def test_candidates_follow_config_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, reverse: bool
    ) -> None:
        routing_file, defaults = _write_permuted_routing(tmp_path, reverse=reverse)
        monkeypatch.setattr(
            "tapehub.providers.routing.builtin_routing_path", lambda: routing_file
        )
        # 启用/停用混合（tickflow/tqsdk/twelvedata 有凭据，baostock/akshare
        # 免凭据恒启用，其余缺凭据停用），使命名链的裁剪有区分度
        settings = make_settings(
            tickflow_api_key="test-key",
            tqsdk_auth_user="user@example.com",
            tqsdk_auth_password="secret",
            twelvedata_api_key="test-twelvedata-key",
        )
        registry = build_default_registry(settings)

        for entry in defaults:
            match = entry["match"]
            expected = [
                name
                for name in entry["order"]
                if registry.get(name) is not None and registry.get(name).enabled
            ]
            candidates = registry.resolve_candidates(
                data_type=DataType(match["data_type"]),
                market=match["market"],
                asset_type=AssetType(match["asset_type"]),
            )
            assert [c.record.name for c in candidates] == expected, (
                f"chain order not respected for {match} (reverse={reverse})"
            )

    def test_permutation_changes_winner_for_same_providers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """同一组合、同一批 Provider，仅换链序 → auto 首选随之改变（裁决只认链序）。"""

        def _first_enabled(records) -> str | None:
            return next((r.record.name for r in records), None)

        winners: dict[bool, str | None] = {}
        for reverse in (False, True):
            routing_file, _ = _write_permuted_routing(tmp_path, reverse=reverse)
            monkeypatch.setattr(
                "tapehub.providers.routing.builtin_routing_path", lambda: routing_file
            )
            registry = build_default_registry(make_settings(tickflow_api_key="test-key"))
            winners[reverse] = _first_enabled(
                registry.resolve_candidates(
                    data_type=DataType.BARS, market="CN", asset_type=AssetType.STOCK
                )
            )
        assert winners[False] != winners[True]


class TestAdapterLoadFailureIsolation:
    """适配器加载失败分层（审查项 L6）：仅导入级错误隔离，其余 fail-fast。

    SDK 均为延迟导入——适配器模块的模块级 ``ImportError`` 现实中即「适配器
    自身导入缺陷或其顶层依赖缺失」，隔离为 ``load_error`` 合理；构造失败/
    类型错误等代码缺陷向上传播（启动即失败，绝不静默下线）。

    隔离在**裁剪部署形态**下验证（单 Provider + 空路由表）：内置全功能
    yaml 下，链上 Provider 被隔离会因路由交叉校验「引用无能力声明」而
    fail-fast（隔离 + 校验组合的既有自洽行为——部署者经
    ``TAPEHUB_ROUTING_CONFIG_PATH`` 裁剪 yaml 移除坏 Provider 后隔离生效）。
    """

    @staticmethod
    def _patch_single_provider_env(
        tmp_path, monkeypatch: pytest.MonkeyPatch, exc: Exception  # type: ignore[no-untyped-def]
    ) -> None:
        """注入单 Provider（fake）+ 空路由表 + 导入异常的裁剪环境。

        manifest 加载一并 mock（内置清单按包资源加载，fake 无真实包）——
        本类只针对「manifest 合法、适配器导入失败」的分层。
        """
        from tapehub.providers import registry as registry_module
        from tapehub.providers import routing as routing_module
        from tapehub.providers.manifest import ProviderManifest

        routing_file = tmp_path / "routing.yaml"
        routing_file.write_text("version: 1\ndefaults: []\n", encoding="utf-8")
        monkeypatch.setattr(routing_module, "builtin_routing_path", lambda: routing_file)
        monkeypatch.setattr(registry_module, "BUILTIN_PROVIDERS", ("fake",))
        monkeypatch.setattr(
            registry_module,
            "load_builtin_manifest",
            lambda name: ProviderManifest(provider=name),
        )

        def fake_import(name: str, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            raise exc

        monkeypatch.setattr(registry_module, "import_module", fake_import)

    def test_import_error_isolated_as_load_error(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch  # type: ignore[no-untyped-def]
    ) -> None:
        self._patch_single_provider_env(
            tmp_path, monkeypatch, ModuleNotFoundError("No module named 'broken_dep'")
        )
        registry = build_default_registry(make_settings())
        record = registry.get("fake")
        assert record is not None
        assert record.load_error is not None
        assert record.load_error.startswith("ModuleNotFoundError")
        assert record.enabled is False
        assert record.capabilities == []
        assert len(registry.list_records()) == 1

    def test_non_import_error_propagates_fail_fast(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch  # type: ignore[no-untyped-def]
    ) -> None:
        self._patch_single_provider_env(
            tmp_path, monkeypatch, ValueError("boom: adapter code bug")
        )
        with pytest.raises(ValueError, match="boom: adapter code bug"):
            build_default_registry(make_settings())
