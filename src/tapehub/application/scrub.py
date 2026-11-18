"""日志脱敏助手：把敏感凭据值替换为占位符。

Provider SDK 异常文本可能回显请求 URL 或账户信息（凭据以查询参数/请求
头传递），catch-all 日志输出 ``str(exc)`` 原文存在凭据落日志风险。各 WS
运行时在 provider 层各自持有本地 ``_scrub``（provider 层不反向依赖
application 层）；本模块服务于 application 层查询/流服务的统一脱敏入口。
"""

CREDENTIAL_MASK = "***"


def scrub(text: str, secret_values: tuple[str, ...]) -> str:
    """把 ``text`` 中出现的每个敏感值替换为 ``***``（逐个全量替换）。

    空元组原样返回（无凭据可脱敏）；替换按元组顺序串行执行，后序值在前序
    替换结果（含掩码文本）中再查找——凭据值互不包含时语义无歧义。
    """
    for value in secret_values:
        if value:
            text = text.replace(value, CREDENTIAL_MASK)
    return text


__all__ = ["CREDENTIAL_MASK", "scrub"]
