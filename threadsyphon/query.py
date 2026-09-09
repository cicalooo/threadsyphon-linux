from __future__ import annotations

from dataclasses import dataclass
import html
import re
from typing import Any


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
    "title", "sub", "body", "com", "id", "no", "board",
    "min_images", "min_replies", "max_images", "max_replies", "sticky", "closed",
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
        # lone NOT or -word
        j = i + 1
        while j < n and s[j].isspace():
            j += 1
        if j < n and not s[j].isspace():
            # could be -field: or -word
            pass
        return ("NOT", "-", i + 1)
    if s[i] in "\"'":
        quote = s[i]
        j = i + 1
        while j < n and s[j] != quote:
            j += 1
        if j >= n:
            raise ValueError("Unclosed quote in query")
        return ("TEXT", s[i + 1 : j], j + 1)
    # word or field:value
    j = i
    while j < n and not s[j].isspace():
        j += 1
    word = s[i:j]
    if ":" in word:
        field, _, rest = word.partition(":")
        fl = field.lower()
        if fl in _FIELD_NAMES:
            if rest == "" and j < n and s[j : j + 1] in "\"'":
                # field: "value"
                tok, val, j2 = _next_token(s, j)
                if tok != "TEXT":
                    raise ValueError(f"Expected value after {field}:")
                return ("FIELD", f"{fl}:{val}", j2)
            if rest == "":
                raise ValueError(f"Missing value after {field}:")
            # field:"quoted" attached or field:value
            if rest[0] in "\"'":
                quote = rest[0]
                if rest.endswith(quote) and len(rest) >= 2:
                    return ("FIELD", f"{fl}:{rest[1:-1]}", j)
                # continue reading until quote
                k = j
                buf = rest[1:]
                while k < n and quote not in buf:
                    while k < n and s[k].isspace():
                        k += 1
                    start = k
                    while k < n and not s[k].isspace() and s[k] != quote:
                        k += 1
                    buf += (" " if buf else "") + s[start:k]
                    if k < n and s[k] == quote:
                        return ("FIELD", f"{fl}:{buf}", k + 1)
                raise ValueError(f"Unclosed quote after {field}:")
            return ("FIELD", f"{fl}:{_unquote(rest)}", j)
    return ("TEXT", word, j)


def parse_query(text: str) -> QueryAST:
    """Parse filter syntax into an AST (OR of AND-groups)."""
    raw = (text or "").strip()
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
            groups[-1].append(Clause("text", value.lower(), negated))
            continue
        if kind == "FIELD":
            field, _, val = value.partition(":")
            field = field.lower()
            if field == "sub":
                field = "title"
            if field == "com":
                field = "body"
            if field == "no":
                field = "id"
            if field in {"title", "body", "board"}:
                groups[-1].append(Clause(field, val.lower(), negated))
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


def _clause_match(clause: Clause, thread: dict[str, Any]) -> bool:
    title = str(thread.get("title") or "").lower()
    body = str(thread.get("body") or "").lower()
    hay = f"{title} {body}".strip()
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
        ok = str(v) in title
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
