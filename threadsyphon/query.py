from __future__ import annotations

from dataclasses import dataclass
import html
import re
from typing import Any, Sequence


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
            if pending_not or not groups[-1]:
                raise ValueError("Query operator needs a following term.")
            break
        if kind == "OR":
            if pending_not or not groups[-1]:
                raise ValueError("OR needs a term on both sides.")
            groups.append([])
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


# Slash-tags embedded in a subject, e.g. "/wdg/ - Web Development General"
_EMBEDDED_TAG_RE = re.compile(r"(?:^|[\s\-_|(\[])(/[a-z0-9]{1,20}/)", re.I)
# Generation / date suffixes that change when a general is re-posted
_GENERATION_SUFFIX_RE = re.compile(
    r"(?:\s*[#№]\s*\d+|\s*\(\s*\d{1,5}\s*\)|\s+\d{4}-\d{2}-\d{2})\s*$"
)
_TITLE_IDENTIFIER_KINDS = frozenset({"tag", "title", "title_exact", "title_prefix"})


@dataclass(frozen=True, slots=True)
class WatchdogDraft:
    """Suggested catalog rule derived from thread title identifiers."""

    name: str
    board: str
    query: str
    label_prefix: str = ""


def extract_title_tags(title: str, board: str = "") -> list[str]:
    """Unique /tag/ identifiers in a subject, skipping the board's own /g/ token."""
    board_l = (board or "").strip().lower().strip("/")
    tags: list[str] = []
    for match in _EMBEDDED_TAG_RE.finditer(title or ""):
        tag = match.group(1).strip("/").lower()
        if not tag or tag == board_l or tag in tags:
            continue
        tags.append(tag)
    return tags


def stable_title_stem(title: str, board: str = "") -> str:
    """Subject with generation numbers/dates stripped, for prefix matching."""
    del board  # board tags stay in the stem so title^: still matches "/g/ …"
    text = _norm_space(title or "")
    while True:
        stripped = _GENERATION_SUFFIX_RE.sub("", text).strip(" -_|:")
        stripped = _norm_space(stripped)
        if stripped == text:
            break
        text = stripped
    return text


def quote_query_value(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return value
    if re.search(r"\s", value) or any(ch in value for ch in ":\"'"):
        return '"' + value.replace('"', "") + '"'
    return value


def query_is_title_identifier(query: QueryAST) -> bool:
    if query.empty:
        return False
    return all(clause.kind in _TITLE_IDENTIFIER_KINDS for group in query.groups for clause in group)


def watchdog_query_from_title(title: str, board: str = "") -> str:
    tags = extract_title_tags(title, board)
    if tags:
        return " ".join(f"/{tag}/" for tag in tags)
    stem = stable_title_stem(title, board)
    if len(stem) < 4:
        raise ValueError("This thread has no stable title identifier (need a /tag/ or a longer subject).")
    return f"title^:{quote_query_value(stem)}"


def _draft_from_query(board: str, query: str, name: str = "", label_prefix: str = "") -> WatchdogDraft:
    parsed = parse_query(query)
    if parsed.empty:
        raise ValueError("Watchdog query cannot be empty.")
    if not query_is_title_identifier(parsed):
        raise ValueError("Watchdog query must match title identifiers, not OP body text.")
    tags = [clause.value for group in parsed.groups for clause in group if clause.kind == "tag" and not clause.negated]
    if not name:
        name = f"/{tags[0]}/" if len(tags) == 1 else query.strip()[:60]
    if not label_prefix and len(tags) == 1:
        label_prefix = f"/{tags[0]}/"
    return WatchdogDraft(name=name.strip()[:60], board=board, query=query.strip(), label_prefix=label_prefix)


def watchdog_draft_from_titles(board: str, titles: Sequence[str]) -> WatchdogDraft:
    """Build one watchdog from subjects that share the same title identifiers."""
    board = (board or "").strip().lower().strip("/")
    cleaned = [title.strip() for title in titles if (title or "").strip()]
    if not cleaned:
        raise ValueError("Selected threads have no subject; pick one with a title tag like /wdg/.")
    tag_sets = [extract_title_tags(title, board) for title in cleaned]
    tagged = [tags for tags in tag_sets if tags]
    if tagged and len(tagged) != len(tag_sets):
        raise ValueError("Selected threads do not share a title tag. Select one series (e.g. only /wdg/ threads).")
    if tagged:
        shared = [tag for tag in tagged[0] if all(tag in tags for tags in tagged[1:])]
        if shared:
            query = " ".join(f"/{tag}/" for tag in shared)
            return _draft_from_query(board, query)
        raise ValueError("Selected threads do not share a title tag. Select one series (e.g. only /wdg/ threads).")
    stems = [stable_title_stem(title, board) for title in cleaned]
    prefix = stems[0]
    for stem in stems[1:]:
        while prefix and not stem.startswith(prefix):
            prefix = prefix[:-1]
        prefix = prefix.rstrip(" -_|:")
    prefix = _norm_space(prefix)
    if len(prefix) < 4:
        raise ValueError("Selected threads do not share a title identifier. Select one series.")
    query = f"title^:{quote_query_value(prefix)}"
    return WatchdogDraft(name=prefix[:60], board=board, query=query, label_prefix="")


def watchdog_draft_from_find_query(board: str, query: str) -> WatchdogDraft:
    """Use the Find box as a watchdog only when it already names title identifiers."""
    board = (board or "").strip().lower().strip("/")
    text = (query or "").strip()
    parsed = parse_query(text)
    if parsed.empty:
        raise ValueError("Select a thread or enter a title identifier (e.g. /wdg/ or title:…).")
    if not query_is_title_identifier(parsed):
        raise ValueError("Select a result so the watchdog can match its title identifiers, not OP body text.")
    return _draft_from_query(board, text)
