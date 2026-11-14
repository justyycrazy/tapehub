"""富途 OpenAPI「方式二：传统 API Key」签名辅助。

签名原文/请求头构造与认证约定见 docs/providers/futunn.md（唯一出处，
本文不复述）；官方 getting-started URL 见 manifest.yaml 头注释。
探针运行记录见 probes/results/PROBE_LOG.md（本地文件，不入库）。
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
