"""Tokenising for the graph index. Terms are graph nodes, so this decides what a Term is."""

from __future__ import annotations

import hashlib
import re

import snowballstemmer

_stem = snowballstemmer.stemmer("english").stemWord

STOP = frozenset(
    """a an and are as at be been but by can could do does did for from had has have how i if in into is it its
    me my of on or our please so some that the their them then there these this those to up us was we were what
    when where which who will with would you your also any all just need want like get let lets make sure using
    use via about over under out off new now here am im ive id can't cant dont don't should must may might per
    than too very each every one two three""".split()
)

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_WORD = re.compile(r"[a-z][a-z0-9]*")


def words(text: str) -> list[str]:
    """Lowercase words with identifiers (camelCase, snake_case, kebab-case) split apart."""
    return _WORD.findall(_CAMEL.sub(" ", text).lower())


def stems(text: str) -> list[str]:
    """Stemmed content words, in order, stopwords and one-letter words dropped."""
    return [_stem(w) for w in words(text) if len(w) > 1 and w not in STOP]


def terms(text: str, bigrams: bool = True) -> list[str]:
    """Unigram stems plus adjacent bigrams (`pull_request`), duplicates removed, order kept."""
    s = stems(text)
    out = list(s)
    if bigrams:
        out += [f"{a}_{b}" for a, b in zip(s, s[1:]) if a != b]
    return list(dict.fromkeys(out))


def nid(kind: str, key: str) -> int:
    """Stable 52-bit integer id for a node. HydraDB node identity is an integer upsert key."""
    digest = hashlib.blake2b(f"{kind}\x00{key}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") >> 12


_ENC = None


def count_tokens(text: str) -> int:
    """Token count: tiktoken cl100k when available, else the chars/4 estimate."""
    global _ENC
    if _ENC is None:
        try:
            import tiktoken

            _ENC = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _ENC = False
    if _ENC:
        return len(_ENC.encode(text))
    return max(1, round(len(text) / 4))
