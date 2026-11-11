"""A small robots.txt evaluator that follows RFC 9309.

Rules implemented, with the RFC section each comes from:
  - Consecutive user-agent lines form one group; the group's allow/disallow
    rules follow them (2.1, 2.2).
  - A crawler matches a group by its product token, case-insensitively. Only
    the leading token characters count, so "GPTBot/1.1" matches GPTBot (2.2.1).
  - Several groups naming the same crawler are merged (2.2.1).
  - If no group names the crawler, the "*" group applies; if there is no "*"
    group either, everything is allowed (2.2.1).
  - The most specific (longest) matching rule wins; on a tie, allow wins (2.2.2).
  - "*" in a path matches any sequence of characters, "$" at the end anchors
    the match (2.2.3).
  - An empty Disallow value matches nothing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

TOKEN = re.compile(r"[A-Za-z_\-]+")


@dataclass
class Group:
    agents: list[str] = field(default_factory=list)   # lower-cased product tokens, or "*"
    rules: list[tuple[bool, str]] = field(default_factory=list)  # (is_allow, path pattern)


def _agent_token(value: str) -> str:
    value = value.strip()
    if value.startswith("*"):
        return "*"
    m = TOKEN.match(value)
    return m.group(0).lower() if m else value.lower()


def parse(text: str) -> list[Group]:
    groups: list[Group] = []
    current: Group | None = None
    in_agent_run = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower().replace(" ", "").replace("_", "-")
        value = value.strip()
        if key in ("user-agent", "useragent"):
            if current is None or not in_agent_run:
                current = Group()
                groups.append(current)
            current.agents.append(_agent_token(value))
            in_agent_run = True
        elif key in ("allow", "disallow"):
            in_agent_run = False
            if current is not None:  # rules before any user-agent line belong to no group
                current.rules.append((key == "allow", value))
        elif key == "crawl-delay":
            in_agent_run = False  # a group member, so it ends the run of user-agent lines
        # sitemap and unknown keys are global or ignored; they do not affect grouping
    return groups


def _pattern_to_regex(pattern: str) -> re.Pattern:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
    return re.compile("^" + regex + ("$" if anchored else ""))


def _matches(pattern: str, path: str) -> bool:
    if pattern == "":
        return False
    return _pattern_to_regex(pattern).match(path) is not None


def groups_for(groups: list[Group], token: str) -> list[Group]:
    tok = token.lower()
    named = [g for g in groups if tok in g.agents]
    if named:
        return named
    return [g for g in groups if "*" in g.agents]


def is_named(groups: list[Group], token: str) -> bool:
    tok = token.lower()
    return any(tok in g.agents for g in groups)


def is_allowed(groups: list[Group], token: str, path: str) -> bool:
    matched = groups_for(groups, token)
    if not matched:
        return True
    best: tuple[int, bool] | None = None
    for g in matched:
        for allow, pattern in g.rules:
            if _matches(pattern, path):
                key = (len(pattern), allow)  # longer wins; on equal length True (allow) wins
                if best is None or key > best:
                    best = key
    return True if best is None else best[1]
