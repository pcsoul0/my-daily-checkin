#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日志脱敏工具（本仓库公开后的日志安全基线）

背景：本仓库的 GitHub Actions 日志对所有访问者可见，而签到脚本运行时会打印
接口返回体、异常文本、页面片段与本机路径，其中可能夹带 requestId、UUID、
凭据字段、手机号、邮箱以及 Windows 用户名。所有「外部数据 → 日志」的输出
都应先经过本模块。

用法::

    from logsafe import redact, redact_obj, mask_secret, mask_uid, mask_path

设计原则：
1. ``redact()`` 只做「字符串 → 字符串」替换，不改变任何业务逻辑；
2. **先脱敏再截断**（顺序反了会把凭据切成两半，导致规则失配）；
3. 指纹（sha256 前 8 位）保留跨运行比对能力，但不可由指纹反推原文。
"""
import hashlib
import json
import re

__all__ = ["redact", "redact_obj", "mask_secret", "mask_uid", "mask_path", "mask_url"]

# 需要打码的凭据类 URL 参数名 —— redact() 的规则 7 与 mask_url() 共用同一份名单，
# 避免两处各写一份后逐渐不同步。
_CRED_PARAM = (r"sign|signature|code|token|access_?token|refresh_?token|key|api_?key|secret|"
               r"session|session_?id|sid|auth|acw_sc__v2|acw_tc|cdn_sec_tc|verify|challenge")

# 脱敏规则，按顺序应用：先整字段打码，再兜底裸值，避免漏网。
_RULES = (
    # 1) 凭据 / 身份类字段：整值打码（"token":"xxx"、"uid": "xxx"）
    (re.compile(r'(?i)("?(?:access_?token|refresh_?token|token|uid|user_?id|openid|session|'
                r'secret|password|passwd|phone|mobile|email|nickname)"?\s*[:=]\s*")([^"]*)(")'),
     r"\1***\3"),
    # 2) 追踪码字段：整值打码（"requestId":"xxx"）
    (re.compile(r'(?i)("?(?:request_?id|trace_?id|req_?id|log_?id|session_?id)"?\s*[:=]\s*")([^"]*)(")'),
     r"\1***\3"),
    # 3) 裸 JWT（三段式）
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}"), "***"),
    # 4) 裸 UUID（无字段名时的 requestId、会话号等）
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "***"),
    # 5) 中国大陆手机号
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "***"),
    # 6) 邮箱
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "***"),
    # 7) URL query 中的凭据类参数值（sign / code / token / WAF 挑战 cookie 等），
    #    这类值不带引号，前面的「字段打码」规则覆盖不到
    (re.compile(r"(?i)([?&](?:%s)=)[^&\s\"'<>]*" % _CRED_PARAM), r"\1***"),
)

# 主目录段匹配：Windows 的 \Users\<name>、macOS 的 /Users/<name>、Linux 的 /home/<name>
_HOME_SEG = re.compile(r"(?i)([\\/](?:Users|home)[\\/])[^\\/]+")


def redact(text):
    """对字符串做脱敏，非字符串入参先转成字符串（None → 空串）。"""
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    for pat, rep in _RULES:
        text = pat.sub(rep, text)
    return text


def redact_obj(obj, limit=500):
    """把 dict / list / 任意对象序列化后脱敏并限长，用于打印接口返回体。"""
    try:
        text = json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        text = str(obj)
    return redact(text)[:limit]


def _fingerprint(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def mask_secret(secret):
    """凭据脱敏：只输出 sha256 前 8 位指纹与长度，日志中不出现任何原文片段。"""
    if not isinstance(secret, str) or not secret:
        return "***"
    return f"sha256:{_fingerprint(secret)} (len={len(secret)})"


def mask_uid(uid):
    """uid 脱敏：与 mask_secret 同策略，保留指纹以便跨运行比对是否换了账号。

    注意：各平台的 uid 常为数字（如 B 站 mid、部分论坛的 uid），
    统一先转字符串再取指纹，否则会退化成 *** 而丢失可比对性。
    """
    if isinstance(uid, (int, float)) and not isinstance(uid, bool):
        uid = str(uid)
    return mask_secret(uid)


def mask_path(path):
    """路径脱敏：把用户主目录段替换为 ***，避免日志暴露本机用户名。"""
    if not isinstance(path, str) or not path:
        return "***"
    return _HOME_SEG.sub(r"\1***", path)


_QUERY_CRED = re.compile(r"(?i)([?&](?:%s)=)[^&\s#\"'<>]*" % _CRED_PARAM)


def mask_url(url):
    """URL 脱敏：只打码凭据类 query 参数的值，其余参数原样保留。

    用于诊断信息里的「最终 URL」——主机、路径和 `id=zqlj_sign` 这类非敏感
    参数是排障关键（能看出是否被重定向到登录/验证页），而 sign code、会话号
    等凭据必须抹掉。
    """
    if not isinstance(url, str) or not url:
        return "***"
    return _QUERY_CRED.sub(r"\1***", url)
