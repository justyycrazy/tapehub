"""富途 OpenAPI「方式二：传统 API Key」签名辅助。

认证约定（官方 getting-started 页，2026-11-14 核实）：

- 控制台「AppKey 管理」创建 AppKey 并上传公钥（Ed25519 或
  RSA-SHA256 / PKCS#1 v1.5），私钥本地保存、只经环境变量路径注入
  （pydantic-settings ``futunn_private_key_path``），绝不写入代码/manifest/
  日志/异常响应；
- 签名原文为 5 个字段以 ``\\n`` 连接：``timestamp_ms``（毫秒 Unix）、大写
  ``http_method``、``request_path``（含路径参数，如
  ``/api/v1.0/quote/HK.00700/history-kline``）、``query_string``（与实际
  请求完全一致的查询串，无查询参数为空串）、``body_part``（请求体字节
  的 SHA256 小写十六进制，无请求体为空串）；
- 私钥签名后 Base64 编码，经 4 个请求头注入：``X-Api-Key``（AppKey ID）、
  ``Authorization``（Base64 签名，**不加 Bearer 前缀**）、``X-Timestamp``
  （毫秒时间戳，须与签名一致；与服务端偏差超过默认 5 秒报 -12006——
  第一版不做自动校时，依赖宿主机时钟，详见 adapter docstring）、
  ``X-Nonce``（1-64 位随机字符串，防重放）；
- 签名算法按私钥 PEM 类型自动检测：Ed25519 → 原文即消息；
  RSA → SHA256 + PKCS#1 v1.5。两种算法均由 ``cryptography`` 提供
  （可选依赖，函数内延迟导入，未安装返回 PROVIDER_NOT_IMPLEMENTED，
  与 httpx 同款处理，不影响服务启动）。
"""

import base64
import hashlib
import secrets
import time

from tapehub.domain.errors import ProviderNotImplementedError

# 随机 nonce 字符集与长度（官方约定 1-64 位字母/数字/下划线/连字符）
_NONCE_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
_NONCE_LENGTH = 32


def load_private_key_pem(path: str, *, provider: str) -> bytes:
    """读取私钥 PEM 文件内容（字节）；文件缺失/不可读属运行前提失败。"""
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError as exc:
        raise ProviderNotImplementedError(
            "futunn private key file is missing or unreadable.",
            details={"provider": "futunn", "setting": "TAPEHUB_FUTUNN_PRIVATE_KEY_PATH"},
        ) from exc


def build_signed_headers(
    app_key: str,
    private_key_pem: bytes,
    *,
    method: str,
    request_path: str,
    query_string: str,
    body: bytes | None,
) -> dict[str, str]:
    """构造签名请求头（X-Api-Key / Authorization / X-Timestamp / X-Nonce）。

    ``query_string`` 必须与实际发送的查询串逐字节一致（request_mapper 统一
    生成、adapter 用同一字符串拼 URL 与签名）；``body`` 为原始请求体字节
    （POST JSON），无请求体传 None。签名算法按私钥类型自动检测（Ed25519 /
    RSA-SHA256 PKCS#1 v1.5）；Content-Type 随头一并注入（POST JSON 体）。
    """
    timestamp_ms = str(int(time.time() * 1000))
    body_part = hashlib.sha256(body).hexdigest() if body else ""
    message = "\n".join(
        [timestamp_ms, method.upper(), request_path, query_string, body_part]
    ).encode("utf-8")
    signature = base64.b64encode(_sign(message, private_key_pem)).decode("ascii")
    return {
        "X-Api-Key": app_key,
        "Authorization": signature,
        "X-Timestamp": timestamp_ms,
        "X-Nonce": _nonce(),
        "Content-Type": "application/json",
    }


def _sign(message: bytes, private_key_pem: bytes) -> bytes:
    """按私钥类型签名（Ed25519 → 原文即消息；RSA → SHA256 + PKCS#1 v1.5）。

    ``cryptography`` 延迟导入（可选依赖）：未安装返回 PROVIDER_NOT_IMPLEMENTED；
    私钥内容非法（非 PEM/损坏/加密未带密码）或类型不受支持（官方仅
    Ed25519 与 RSA 两种公钥类型，EC 等属配置错误）按运行前提失败——错误
    信息只定位到设置项，不携带密钥材料片段。
    """
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
    except ImportError as exc:
        raise ProviderNotImplementedError(
            "Provider dependency 'cryptography' is not installed.",
            details={"provider": "futunn", "sdk": "cryptography"},
        ) from exc
    try:
        key = load_pem_private_key(private_key_pem, password=None)
    except (ValueError, TypeError) as exc:
        raise ProviderNotImplementedError(
            "futunn private key file is not a valid unencrypted PEM private key.",
            details={"provider": "futunn", "setting": "TAPEHUB_FUTUNN_PRIVATE_KEY_PATH"},
        ) from exc
    if isinstance(key, ed25519.Ed25519PrivateKey):
        return key.sign(message)
    if isinstance(key, rsa.RSAPrivateKey):
        # RSA 私钥（PKCS#1/PKCS#8 均可加载）：官方约定的 PKCS#1 v1.5 签名
        return key.sign(message, padding.PKCS1v15(), hashes.SHA256())
    raise ProviderNotImplementedError(
        "futunn private key type is not supported (expected Ed25519 or RSA).",
        details={"provider": "futunn", "setting": "TAPEHUB_FUTUNN_PRIVATE_KEY_PATH"},
    )


def _nonce() -> str:
    """防重放随机串（官方约定 1-64 位字母/数字/下划线/连字符）。"""
    return "".join(secrets.choice(_NONCE_ALPHABET) for _ in range(_NONCE_LENGTH))


__all__ = ["build_signed_headers", "load_private_key_pem"]
