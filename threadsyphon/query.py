from __future__ import annotations

from dataclasses import dataclass
import html
import re
from typing import Any


# /caig/ style board-general tags in titles
TAG_RE = re.compile(r"^/[a-z0-9]{1,20}/$", re.I)


@dataclass(frozen=True, slots=True)
class Clause:
    kind: str
    value: str | int | bool
    negated: bool = False


@dataclass(frozen=True, slots=True)
class QueryAST:
    """OR of AND-groups."""

    groups: tuple[tuple[Clause, ...], ...]

    @property
    def empty(self) -> bool:
        return not self.groups or all(len(g) == 0 for g in self.groups)


def strip_html(value: str) -> str:
    text = html.unescape(value or "")
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _norm_space(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _parse_bool(value: str) -> bool:
    v = value.strip().lower()
    if v in {"1", "true", "yes", "y", "on"}:
        return True
    if v in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"Expected true/false, got {value!r}")


_FIELD_NAMES = {
    "title", "sub", "body", "com", "id", "no", "board", "tag",
    "min_images", "min_replies", "max_images", "max_replies", "sticky", "closed",
    # exact / prefix title forms written as title=: val via special parse below
}


def _next_token(s: str, i: int) -> tuple[str, str, int]:
    n = len(s)
    while i < n and s[i].isspace():
        i += 1
    if i >= n:
        return ("EOF", "", i)
    if s.startswith("OR", i) and (i + 2 == n or not s[i + 2].isalnum()):
        return ("OR", "OR", i + 2)
    if s.startswith("NOT", i) and (i + 3 == n or not s[i + 3].isalnum()):
        return ("NOT", "NOT", i + 3)
    if s[i] == "-":
        return ("NOT", "-", i + 1)
    if s[i] in "\"'":
        quote = s[i]
        j = i + 1
        while j < n and s[j] != quote:
            j += 1
        if j >= n:
            raise ValueError("Unclosed quote in query")
        return ("TEXT", s[i + 1 : j], j + 1)

    # Support title=:value and title^:value (no space)
    for prefix, kind in (("title=:", "title_exact"), ("sub=:", "title_exact"),
                         ("title^:", "title_prefix"), ("sub^:", "title_prefix"),
                         ("title=:", "title_exact")):
        if s.startswith(prefix, i):
            j = i + len(prefix)
            if j < n and s[j] in "\"'":
                tok, val, j2 = _next_token(s, j)
                if tok != "TEXT":
                    raise ValueError(f"Expected value after {prefix}")
                return ("FIELD", f"{kind}:{val}", j2)
            k = j
            while k < n and not s[k].isspace():
                k += 1
            if k == j:
                raise ValueError(f"Missing value after {prefix}")
            return ("FIELD", f"{kind}:{_unquote(s[j:k])}", k)

    j = i
    while j < n and not s[j].isspace():
        j += 1
    word = s[i:j]
    if ":" in word:
        field, _, rest = word.partition(":")
        fl = field.lower()
        if fl in _FIELD_NAMES:
            if rest == "" and j < n and s[j : j + 1] in "\"'":
                tok, val, j2 = _next_token(s, j)
                if tok != "TEXT":
                    raise ValueError(f"Expected value after {field}:")
                return ("FIELD", f"{fl}:{val}", j2)
            if rest == "":
                raise ValueError(f"Missing value after {field}:")
            if rest[0] in "\"'":
                quote = rest[0]
                if rest.endswith(quote) and len(rest) >= 2:
                    return ("FIELD", f"{fl}:{rest[1:-1]}", j)
                k = j
                buf = rest[1:]
                while k < n:
                    while k < n and s[k].isspace():
                        k += 1
                    start = k
                    while k < n and s[k] != quote:
                        k += 1
                    buf += (" " if buf else "") + s[start:k]
                    if k < n and s[k] == quote:
                        return ("FIELD", f"{fl}:{buf}", k + 1)
                raise ValueError(f"Unclosed quote after {field}:")
            return ("FIELD", f"{fl}:{_unquote(rest)}", j)
    return ("TEXT", word, j)


def _title_clause(field: str, val: str, negated: bool) -> Clause:
    """Map title operators: title: substring, title=: exact, title^: prefix, tag: /x/."""
    if field in {"title", "sub"}:
        return Clause("title", val.lower(), negated)
    if field in {"title_exact", "sub_exact"}:
        return Clause("title_exact", _norm_space(val), negated)
    if field in {"title_prefix", "sub_prefix"}:
        return Clause("title_prefix", _norm_space(val), negated)
    if field == "tag":
        tag = val.strip().lower().strip("/")
        return Clause("tag", tag, negated)
    raise ValueError(field)


def parse_query(text: str) -> QueryAST:
    """Parse filter syntax into an AST (OR of AND-groups).

    Title helpers:
      title:/caig/          substring in subject only
      title:"/caig/ c ai"   phrase in subject only
      title=:"/caig/ c ai general"   full subject equality (normalized)
      title^:/caig/         subject starts with
      tag:caig              subject contains /caig/ as a tag token
      /caig/                same as tag:caig (title only, not body)
    """
    raw = (text or "").strip()
    # Allow accidental leading = from users typing "= /caig/"
    if raw.startswith("="):
        raw = raw[1:].strip()
    if not raw:
        return QueryAST(groups=((),))

    groups: list[list[Clause]] = [[]]
    pending_not = False
    i = 0
    while True:
        kind, value, i = _next_token(raw, i)
        if kind == "EOF":
            break
        if kind == "OR":
            groups.append([])
            pending_not = False
            continue
        if kind == "NOT":
            pending_not = True
            continue
        negated = pending_not
        pending_not = False
        if kind == "TEXT":
            # /caig/ alone → title tag match (not OP body)
            if TAG_RE.match(value):
                groups[-1].append(Clause("tag", value.strip("/").lower(), negated))
            else:
                groups[-1].append(Clause("text", value.lower(), negated))
            continue
        if kind == "FIELD":
            field, _, val = value.partition(":")
            field = field.lower()
            if field == "com":
                field = "body"
            if field == "no":
                field = "id"
            if field in {"title", "sub", "title_exact", "title_prefix", "tag"}:
                if field == "sub":
                    field = "title"
                if field == "title" and val.startswith("="):
                    # title:=foo or title=:/ already handled; title:=foo via title: =foo
                    groups[-1].append(Clause("title_exact", _norm_space(val.lstrip("=")), negated))
                elif field == "title" and val.startswith("^"):
                    groups[-1].append(Clause("title_prefix", _norm_space(val.lstrip("^")), negated))
                else:
                    groups[-1].append(_title_clause(field if field != "title" else "title", val, negated))
            elif field == "body":
                groups[-1].append(Clause("body", val.lower(), negated))
            elif field == "board":
                groups[-1].append(Clause("board", val.lower(), negated))
            elif field == "id":
                digits = re.sub(r"\D", "", val)
                if not digits:
                    raise ValueError("id: needs a thread number")
                groups[-1].append(Clause("id", int(digits), negated))
            elif field in {"min_images", "min_replies", "max_images", "max_replies"}:
                groups[-1].append(Clause(field, int(val), negated))
            elif field in {"sticky", "closed"}:
                groups[-1].append(Clause(field, _parse_bool(val), negated))
            else:
                raise ValueError(f"Unknown field: {field}")
            continue
        raise ValueError(f"Unexpected token {kind}")

    cleaned = tuple(tuple(g) for g in groups if g)
    if not cleaned:
        return QueryAST(groups=((),))
    return QueryAST(groups=cleaned)


def _title_has_tag(title: str, tag: str) -> bool:
    """True if title contains /tag/ as its own token (not a longer word)."""
    title_l = title.lower()
    token = f"/{tag.lower().strip('/')}/"
    if token not in title_l:
        return False
    # Ensure it's a path-like tag, not embedded mid-token oddly
    return re.search(rf"(^|[\s\-_|]){re.escape(token)}([\s\-_|]|$)", title_l) is not None or title_l.startswith(token)


def _clause_match(clause: Clause, thread: dict[str, Any]) -> bool:
    title = str(thread.get("title") or "")
    title_l = title.lower()
    title_n = _norm_space(title)
    body = str(thread.get("body") or "").lower()
    hay = f"{title_l} {body}".strip()
    board = str(thread.get("board") or "").lower()
    no = int(thread.get("no") or 0)
    images = int(thread.get("images") or 0)
    replies = int(thread.get("replies") or 0)
    sticky = bool(thread.get("sticky"))
    closed = bool(thread.get("closed"))

    k, v = clause.kind, clause.value
    if k == "text":
        ok = str(v) in hay
    elif k == "title":
        ok = str(v) in title_l
    elif k == "title_exact":
        ok = title_n == _norm_space(str(v))
    elif k == "title_prefix":
        ok = title_n.startswith(_norm_space(str(v)))
    elif k == "tag":
        ok = _title_has_tag(title, str(v))
    elif k == "body":
        ok = str(v) in body
    elif k == "board":
        ok = board == str(v)
    elif k == "id":
        ok = no == int(v)
    elif k == "min_images":
        ok = images >= int(v)
    elif k == "min_replies":
        ok = replies >= int(v)
    elif k == "max_images":
        ok = images <= int(v)
    elif k == "max_replies":
        ok = replies <= int(v)
    elif k == "sticky":
        ok = sticky is bool(v)
    elif k == "closed":
        ok = closed is bool(v)
    else:
        ok = False
    return (not ok) if clause.negated else ok


def match_thread(query: QueryAST, thread: dict[str, Any]) -> bool:
    if query.empty:
        return True
    for group in query.groups:
        if all(_clause_match(c, thread) for c in group):
            return True
    return False


def filter_threads(query: QueryAST, threads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [t for t in threads if match_thread(query, t)]
