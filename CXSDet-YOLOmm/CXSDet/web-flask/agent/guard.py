"""OpenCode 入口的敏感请求保护；不把普通问答、代码或管理员操作当攻击。"""
import re

_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_HIDDEN = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")


def redact(text):
    normalized = _HIDDEN.sub("", text)
    if any(pattern.search(normalized) for pattern in (_KEY, _BEARER, _JWT)):
        text = normalized
    for pattern in (_KEY, _BEARER, _JWT):
        text = pattern.sub("[敏感凭据已隐藏]", text)
    return text


def inspect(text):
    check = _HIDDEN.sub("", text)
    if _KEY.search(check) or _JWT.search(check) or _BEARER.search(check):
        return "请勿在对话中粘贴密钥或登录令牌。请通过「设置 → 个人连接」配置；本次内容不会发送给模型。"
    extraction = (
        r"(?:输出|泄露|显示|打印|复述|告诉我).{0,30}(?:系统提示词|系统指令原文|完整系统指令)",
        r"(?:系统提示词|系统指令原文).{0,20}(?:输出|泄露|显示|打印|告诉我)",
        r"(?:你的|后台的|服务端的).{0,20}(?:api\s*key|密钥|登录令牌).{0,20}(?:告诉我|给我|输出|显示)",
        r"(?:输出|告诉我|显示|给我).{0,20}(?:你的|后台的|服务端的).{0,20}(?:api\s*key|密钥|登录令牌)",
        r"(?:reveal|print|show|repeat|output).{0,40}(?:your\s+system\s+prompt|system\s+instructions|your\s+api\s*key)",
        r"(?:忽略|ignore).{0,20}(?:指令|instructions).{0,80}(?:提示词|system\s+prompt|api\s*key)",
    )
    if any(re.search(pattern, check, re.I | re.S) for pattern in extraction):
        return "海巡助手不会提供服务端密钥、登录令牌或私有系统指令。你可以继续询问项目功能、处理文件或查看自己的模型连接状态。"
    return None
