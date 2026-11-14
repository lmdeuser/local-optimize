"""Bounded, inspectable retrieval variants; verification always uses the original query."""

from __future__ import annotations

import re

_FILLER = frozenset("как что такое это для чего зачем почему какой какая какие где работает работают делает делают используется используются устроен устроена устроено устроены функция функции класс класса how what does do is are the a an works work function class".split())
_IDENTIFIER = re.compile(r"\b(?:[A-Za-z][\w]*_[\w]+|[A-Z][a-z]+[A-Z]\w*|[A-Z]{2,}|[A-Za-z]\w+\.(?:env|py|sql|md))\b")
# Vocabulary equivalences only. Unknown terms and all constraints remain in the
# original question passed to the evidence model; these variants only fetch candidates.
_GLOSSARY = {
    "кэш": "cache", "кеш": "cache", "сохран": "save", "факт": "knowledge",
    "просроч": "overdue", "устар": "stale", "знан": "knowledge",
    "приват": "private", "сесси": "session", "парол": "password",
    "баз": "database", "удален": "delete", "документ": "document",
    "готов": "readiness", "работающ": "liveness", "огранич": "limit",
    "время": "timeout", "времен": "timeout", "поиск": "search", "поисков": "search",
    "поколен": "generation", "инвалидац": "invalidation", "очеред": "queue",
    "заголов": "header", "агент": "agent", "параллел": "concurrent",
    "источник": "source", "подключен": "connection", "сервер": "server",
}


def compact_query(query: str) -> str:
    if any(char in query for char in ('"', "`")):
        return query
    words = re.findall(r"[^\s?!,;]+", query)
    return " ".join(word for word in words if word.casefold() not in _FILLER)


def retrieval_variants(query: str) -> list[str]:
    """Keep the original plus at most two deterministic lookup variants."""
    variants = [query, compact_query(query)]
    translated = []
    for word in re.findall(r"\w+", query.casefold().replace("ё", "е")):
        for prefix, equivalent in _GLOSSARY.items():
            if word.startswith(prefix):
                translated.append(equivalent)
                break
    if translated and not any(char in query for char in ('"', "`")):
        translated.extend(_IDENTIFIER.findall(query))
        variants.append(" ".join(dict.fromkeys(translated))[:300])
    return list(dict.fromkeys(value for value in variants if value.strip()))[:3]


def query_facet(query: str) -> str | None:
    """Check the requested property independently of an attractive symbol name."""
    if not _IDENTIFIER.search(query):
        return None
    remainder = _IDENTIFIER.sub(" ", query)
    compact = compact_query(remainder)
    neutral = _FILLER | frozenset("найти определение определения определению функции методов метод функцию explain describe definition find differ difference between from and or versus vs чем отличается отличаются разница между и или of у автора developer".split())
    meaningful = [w for w in re.findall(r"\w+", compact.casefold()) if w not in neutral]
    return compact if meaningful else None


def requests_secret_value(query: str) -> bool:
    """Source code can explain secret storage; it must not disclose actual credentials."""
    return bool(re.search(
        r"\b(?:какой|какая|какое|назови|покажи|выдай|скажи|раскрой|выведи)\s+"
        r"(?:(?:секретный|текущий|действующий|реальный|записанный)\s+)?"
        r"(?:пароль|токен|секрет(?:ный)?\s+ключ|ключ\s+доступа)\b"
        r"|\b(?:actual|current|stored|real|raw|secret)\s+"
        r"(?:password|passphrase|api[ _-]?key|access[ _-]?token|bearer[ _-]?token|db[ _-]?password|secret[ _-]?key|private[ _-]?key)\b"
        r"|\b(?:show|reveal|give|print|tell|dump|display|extract)\s+(?:me\s+)?(?:the\s+)?"
        r"(?:password|passphrase|api[ _-]?key|access[ _-]?token|bearer[ _-]?token|db[ _-]?password|secret[ _-]?key|private[ _-]?key)\b",
        query, re.IGNORECASE,
    ))
