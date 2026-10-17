import re
from typing import List, Optional, Tuple

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
TICKET_RE = re.compile(r"\b[A-Z]{2,}-\d+\b")
UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.DOTALL)
_HEX_RE = re.compile(r"[0-9a-fA-F]{32,}")
_HEX_KEYWORD_RE = re.compile(r"token|key|secret", re.IGNORECASE)
_HEX_WINDOW = 20
_PAN_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9 ]{11,30}\b")

# (kind, regex, replacement) for the plain substitution rows, in spec order.
_SIMPLE_BEFORE_HEX = [
    ("github-token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"),
     "<redacted:github-token>"),
    ("gitlab-token", re.compile(r"glpat-[A-Za-z0-9_-]{20,}"), "<redacted:gitlab-token>"),
    ("npm-token", re.compile(r"npm_[A-Za-z0-9]{30,}"), "<redacted:npm-token>"),
    ("api-key", re.compile(r"sk-[A-Za-z0-9_-]{20,}|(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"),
     "<redacted:api-key>"),
    ("google-key", re.compile(r"AIza[0-9A-Za-z_-]{35}|ya29\.[0-9A-Za-z_-]{20,}"),
     "<redacted:google-key>"),
    ("aws-key", re.compile(r"AKIA[0-9A-Z]{16}"), "<redacted:aws-key>"),
    ("slack-token", re.compile(r"xox[bp]-[A-Za-z0-9-]{10,}"), "<redacted:slack-token>"),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
     "<redacted:jwt>"),
    ("bearer", re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{16,}"), "Bearer <redacted>"),
    ("url-credentials", re.compile(r"([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"), r"\1<redacted>@"),
    ("assignment", re.compile(r"\b(password|passwd|pwd|secret|token|api[_-]?key)\s*[:=]\s*\S+",
                              re.IGNORECASE), r"\1=<redacted>"),
]


def luhn_ok(digits: str) -> bool:
    if not digits or not digits.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def iban_ok(s: str) -> bool:
    compact = s.replace(" ", "")
    if not (15 <= len(compact) <= 34) or not compact.isalnum() or not compact.isascii():
        return False
    rearranged = compact[4:] + compact[:4]
    number = "".join(str(ord(c) - 55) if c.isalpha() else c for c in rearranged.upper())
    return int(number) % 97 == 1


def _hex_repl(match) -> str:
    text = match.string
    before = text[max(0, match.start() - _HEX_WINDOW):match.start()]
    after = text[match.end():match.end() + _HEX_WINDOW]
    if _HEX_KEYWORD_RE.search(before) or _HEX_KEYWORD_RE.search(after):
        return "<redacted:hex>"
    return match.group(0)


def _pan_repl(match) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    if 13 <= len(digits) <= 19 and luhn_ok(digits):
        return "<redacted:pan>"
    return match.group(0)


def _iban_repl(match) -> str:
    return "<redacted:iban>" if iban_ok(match.group(0)) else match.group(0)


def _extra_patterns(cfg: Optional[dict]) -> list:
    out = []
    for raw in (cfg or {}).get("extra_redact_patterns") or []:
        if not isinstance(raw, str):
            continue
        try:
            out.append(re.compile(raw))
        except re.error:
            continue
    return out


def scrub(text: Optional[str], cfg: Optional[dict] = None) -> Tuple[Optional[str], List[str]]:
    if text is None:
        return None, []
    flags = []  # type: List[str]

    def note(kind: str, changed: bool) -> None:
        if changed and "redacted:" + kind not in flags:
            flags.append("redacted:" + kind)

    text, n = _PRIVATE_KEY_RE.subn("<redacted:private-key>", text)
    note("private-key", n > 0)
    for kind, regex, repl in _SIMPLE_BEFORE_HEX:
        text, n = regex.subn(repl, text)
        note(kind, n > 0)
    redacted_hex = []  # type: List[bool]

    def hex_repl(match) -> str:
        out = _hex_repl(match)
        redacted_hex.append(out != match.group(0))
        return out

    text = _HEX_RE.sub(hex_repl, text)
    note("hex", any(redacted_hex))
    before = text
    text = _PAN_RE.sub(_pan_repl, text)
    note("pan", text != before)
    before = text
    text = _IBAN_RE.sub(_iban_repl, text)
    note("iban", text != before)
    for regex in _extra_patterns(cfg):
        text, n = regex.subn("<redacted:custom>", text)
        note("custom", n > 0)

    own = set()
    for dom in (cfg or {}).get("own_email_domains") or []:
        if isinstance(dom, str):
            own.add(dom.lower())
    emails = list(EMAIL_RE.finditer(text))
    if emails:
        flags.append("email")
        if any(m.group(1).lower() not in own for m in emails):
            flags.append("foreign_email")
    return text, flags


def mask_outbound(text: str) -> str:
    return EMAIL_RE.sub("<email>", UUID_RE.sub("<uuid>", text))
