"""Source data stays data. These filters supplement, not replace, that boundary."""

import re

_INJECTION = re.compile(
    r"ignore\s+(?:all\s+)?(?:previous|prior|system|developer)\s+(?:instructions|rules|prompts)"
    r"|(?:override|bypass|disable)\s+(?:the\s+)?(?:safety|permissions|sandbox|system\s+prompt)"
    r"|(?:reveal|exfiltrate|upload|send)\b.{0,70}\b(?:secrets?|api[ _-]?keys?|credentials?|passwords?)"
    r"|<\|(?:im_start|system|developer)\|>|\[/?(?:SYSTEM|INST)\]"
    r"|忽略.{0,12}(?:指令|规则|提示)|(?:泄露|上传|发送).{0,16}(?:密钥|密码|凭据)",
    re.IGNORECASE | re.DOTALL,
)

_SECRETS = (
    (re.compile(r"\b(?:sk|ghp|gho|github_pat)-?[A-Za-z0-9_\-]{20,}\b"), "[REDACTED_TOKEN]"),
    (re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._\-]{16,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:api[_-]?key|password|secret|access[_-]?token)\s*[=:]\s*[\"']?)[^\s\"',;]+"), r"\1[REDACTED]"),
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.DOTALL), "[REDACTED_PRIVATE_KEY]"),
)


def redact(text: str) -> str:
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    return text


def suspicious(text: str) -> bool:
    return bool(_INJECTION.search(text))


def clean_statement(text: str) -> str:
    # Do not silently turn truncated paragraphs into complete asserted lessons.
    value = redact(text).strip().strip("` ")
    if len(value) > 2000:
        raise ValueError("Experience statements must be at most 2000 characters")
    return value

