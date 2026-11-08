"""baostock 适配器（已实现）。

约定：

- baostock 是可选依赖：SDK 只在 ``_fetch_sync`` 内部延迟导入，
  未安装时返回 PROVIDER_NOT_IMPLEMENTED，不影响服务启动；
- baostock 为免费无凭据数据平台（证券宝），恒可注册——registry 不登记
  凭据，凭据推断恒通过；
- 同步阻塞调用通过 ``call_in_thread`` 隔离到线程池，不阻塞事件循环；
- baostock 是 ``login()/logout()`` 全局单连接模型（模块级单例 socket，
  非线程安全）：模块级锁把「登录 → 查询 → 行收集」全程串行化；会话
  采用「懒登录 + 进程内复用 + 错误重登」——首次调用登录，之后复用；
  查询返回未登录/网络类错误码时重登一次并重试一次。不主动 logout
  （进程退出即断，baostock >=0.8.9 已修复退出时 socket 未关闭问题）；
- 上游错误经字符串 ``error_code/error_msg`` 返回而非异常（"0"=成功），
  非零错误码统一包装为 ``UPSTREAM_ERROR``（details 透出上游错误码字符串，
  本 Provider 无凭据可泄露）；SDK 异常由应用层统一包装，不穿透 API；
- 上游接口不支持 offset/limit，统一分页由共享 ``paginate_bars`` 完成；
  上游按日期过滤的边界由适配器在标准化后按闭区间 [start, end] 精确复核。
"""

import threading
from datetime import datetime
from typing import Any

from tapehub.domain.errors import ProviderNotImplementedError, UpstreamError
from tapehub.domain.models import CN_TIMEZONE, BarBatch, BarQuery
from tapehub.providers.baostock.request_mapper import build_request
from tapehub.providers.baostock.response_mapper import to_standard_bars
from tapehub.providers.base import BaseProviderAdapter, call_in_thread
from tapehub.providers.pagination import paginate_bars

# baostock 的 login/query_* 作用于模块级单例 socket 会话（进程全局），
# 非线程安全：call_in_thread 并发时必须串行化全部会话操作
_SESSION_LOCK = threading.Lock()

# 会话状态与 baostock 模块对象绑定：模块对象变化（重导/测试桩替换）即重新登录
_SESSION_LOGGED_IN = False
_SESSION_OWNER: Any = None

# 上游未登录错误码（会话失效，重登可恢复）；10002xxx 为网络类错误码前缀
_NO_LOGIN_ERROR_CODE = "10001001"
_NETWORK_ERROR_CODE_PREFIX = "10002"


def _import_sdk() -> Any:
    """延迟导入 baostock；未安装返回 PROVIDER_NOT_IMPLEMENTED。"""
    try:
        import baostock as bs
    except ImportError as exc:
        raise ProviderNotImplementedError(
            "Provider SDK 'baostock' is not installed.",
            details={"provider": "baostock", "sdk": "baostock"},
        ) from exc
    return bs


def _is_recoverable(error_code: str) -> bool:
    """未登录或网络类错误：重登一次可能恢复。"""
    return error_code == _NO_LOGIN_ERROR_CODE or error_code.startswith(_NETWORK_ERROR_CODE_PREFIX)


def _error_code(result: Any) -> str:
    return str(getattr(result, "error_code", "") or "")


def _error_msg(result: Any) -> str:
    return str(getattr(result, "error_msg", "") or "")


def _upstream_error(message: str, error_code: str, error_msg: str) -> UpstreamError:
    return UpstreamError(
        message,
        details={
            "provider": "baostock",
            "upstream_error_code": error_code,
            "upstream_error_msg": error_msg,
        },
    )


def _ensure_session(bs: Any) -> None:
    """懒登录：进程内复用会话；登录失败包装为 UpstreamError。

    官方错误码 "0" 表示成功；其余（含黑名单 10001011）如实透出。
    """
    global _SESSION_LOGGED_IN, _SESSION_OWNER
    if _SESSION_LOGGED_IN and _SESSION_OWNER is bs:
        return
    lg = bs.login()
    error_code = _error_code(lg)
    if error_code != "0":
        _SESSION_LOGGED_IN = False
        _SESSION_OWNER = None
        raise _upstream_error("baostock login failed.", error_code, _error_msg(lg))
    _SESSION_LOGGED_IN = True
    _SESSION_OWNER = bs


def _force_relogin(bs: Any) -> None:
    """强制重登：会话失效/网络中断后的恢复路径（官方示例允许重复 login）。"""
    global _SESSION_LOGGED_IN
    _SESSION_LOGGED_IN = False
    _ensure_session(bs)


class ProviderAdapter(BaseProviderAdapter):
    """baostock 适配器。"""

    implemented = True

    async def fetch_bars(self, query: BarQuery) -> BarBatch:
        operation = self.resolve_operation(query)
        return await call_in_thread(self._fetch_sync, operation, query)

    def _fetch_sync(self, operation: str, query: BarQuery) -> BarBatch:
        # 先完成参数映射（能力/标的映射错误优先于 SDK 检查，与 akshare 一致）
        kwargs = build_request(operation, query)
        bs = _import_sdk()
        # 单例 socket 会话：从登录到行收集全程持锁串行化
        with _SESSION_LOCK:
            _ensure_session(bs)
            result = self._query_upstream(bs, kwargs)
            rows = self._collect_rows(result)
        bars = to_standard_bars(query.asset_type, query.interval, rows)
        # 上游按日期过滤，这里按闭区间 [start, end] 精确复核统一时间语义
        window = [bar for bar in bars if query.start <= bar.timestamp <= query.end]
        page = paginate_bars(window, query.offset, query.limit)
        return BarBatch(
            instrument=query.instrument,
            interval=query.interval,
            bars=page,
            provider=self.name,
            received_at=datetime.now(CN_TIMEZONE),
            offset=query.offset,
            limit=query.limit,
        )

    def _query_upstream(self, bs: Any, kwargs: dict[str, str]) -> Any:
        """单次查询；未登录/网络类错误码重登一次并重试一次（仍失败如实上报）。"""
        result = bs.query_history_k_data_plus(**kwargs)
        error_code = _error_code(result)
        if error_code == "0":
            return result
        if _is_recoverable(error_code):
            _force_relogin(bs)
            result = bs.query_history_k_data_plus(**kwargs)
            error_code = _error_code(result)
            if error_code == "0":
                return result
        raise _upstream_error("baostock query failed.", error_code, _error_msg(result))

    def _collect_rows(self, result: Any) -> list[dict[str, Any]]:
        """ResultData → 字典行列表（按 rs.fields 成键）；空结果返回空列表。

        上游行值全为字符串；服务端分批对调用者透明（next() 循环取尽），
        行长度与 fields 不齐按契约违背处理（zip strict 抛错 → 上层包装）。
        """
        fields = list(getattr(result, "fields", None) or [])
        rows: list[dict[str, Any]] = []
        while result.next():
            row = result.get_row_data()
            rows.append(dict(zip(fields, row, strict=True)))
        return rows
