"""题库搜索语法解析与记录匹配，不接触文件系统。"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any


SUPPORTED_FIELDS = frozenset({
    "tag", "content", "solution", "note", "source", "type", "difficulty", "starred",
})
_TRUE_VALUES = frozenset({"1", "true", "on"})
_FALSE_VALUES = frozenset({"0", "false", "off"})


class SearchQueryError(ValueError):
    """搜索表达式无法安全解释。"""


@dataclass(frozen=True)
class SearchClause:
    field: str
    value: str | bool


@dataclass(frozen=True)
class SearchQuery:
    raw: str
    structured: bool
    clauses: tuple[SearchClause, ...]


def _split_tokens(raw: str) -> list[str]:
    """只处理搜索语法需要的双引号，保留 LaTeX 中的反斜杠。"""
    tokens: list[str] = []
    current: list[str] = []
    quoted = False
    for char in raw:
        if char == '"':
            quoted = not quoted
            continue
        if char.isspace() and not quoted:
            if current:
                tokens.append("".join(current))
                current = []
            continue
        current.append(char)
    if quoted:
        raise SearchQueryError("搜索条件中的双引号没有闭合")
    if current:
        tokens.append("".join(current))
    return tokens


def parse_search_query(
        raw: str, *, allowed_types: Iterable[str] | None = None,
        allowed_difficulties: Iterable[str] | None = None) -> SearchQuery:
    raw = (raw or "").strip()
    tokens = _split_tokens(raw)
    structured = any(
        token.partition(":")[0].casefold() in SUPPORTED_FIELDS
        and bool(token.partition(":")[1])
        for token in tokens
    )
    if not structured:
        clauses = (SearchClause("any", raw),) if raw else ()
        return SearchQuery(raw=raw, structured=False, clauses=clauses)

    allowed = {
        "type": ({str(item).casefold() for item in allowed_types}
                 if allowed_types is not None else None),
        "difficulty": ({str(item).casefold() for item in allowed_difficulties}
                       if allowed_difficulties is not None else None),
    }
    clauses: list[SearchClause] = []
    for token in tokens:
        field, separator, value = token.partition(":")
        normalized_field = field.casefold()
        if not separator or normalized_field not in SUPPORTED_FIELDS:
            clauses.append(SearchClause("any", token))
            continue
        if not value:
            raise SearchQueryError(f"{normalized_field}: 后必须填写搜索内容")
        if normalized_field == "starred":
            normalized_value = value.casefold()
            if normalized_value in _TRUE_VALUES:
                clauses.append(SearchClause(normalized_field, True))
            elif normalized_value in _FALSE_VALUES:
                clauses.append(SearchClause(normalized_field, False))
            else:
                raise SearchQueryError(
                    "starred: 仅支持 true/false 或 1/0")
        else:
            choices = allowed.get(normalized_field)
            if choices is not None and value.casefold() not in choices:
                raise SearchQueryError(
                    f"{normalized_field}: 不支持「{value}」")
            clauses.append(SearchClause(normalized_field, value))
    return SearchQuery(raw=raw, structured=True, clauses=tuple(clauses))


def _contains(value: Any, needle: str) -> bool:
    return needle.casefold() in str(value or "").casefold()


def _matches_any(record: Mapping[str, Any], needle: str) -> bool:
    return (
        _contains(record.get("body"), needle)
        or _contains(record.get("solution"), needle)
        or _contains(record.get("note"), needle)
        or _contains(record.get("source"), needle)
        or any(_contains(tag, needle) for tag in record.get("tags", []))
    )


def matches_search(
        record: Mapping[str, Any], query: SearchQuery, *,
        tag_resolver: Callable[[str], Iterable[str]] | None = None) -> bool:
    """判断一条记录是否满足查询；所有子句均按 AND 组合。"""
    for clause in query.clauses:
        field = clause.field
        value = clause.value
        if field == "any":
            matched = _matches_any(record, str(value))
        elif field == "tag":
            tags = record.get("tags", [])
            matched = any(_contains(tag, str(value)) for tag in tags)
            if not matched and tag_resolver is not None:
                descendants = {
                    str(tag).casefold() for tag in tag_resolver(str(value))
                }
                matched = any(str(tag).casefold() in descendants for tag in tags)
        elif field == "content":
            matched = _contains(record.get("body"), str(value))
        elif field == "solution":
            matched = _contains(record.get("solution"), str(value))
        elif field == "note":
            matched = _contains(record.get("note"), str(value))
        elif field == "source":
            matched = _contains(record.get("source"), str(value))
        elif field in {"type", "difficulty"}:
            matched = str(record.get(field, "")).casefold() == str(value).casefold()
        else:  # starred
            matched = bool(record.get("starred")) is value
        if not matched:
            return False
    return True
