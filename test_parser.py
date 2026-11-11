#!/usr/bin/env python3
"""Checks that the parser reads robots.txt the way RFC 9309 says. Run: python test_parser.py"""
from robots_parser import is_allowed, is_named, parse

P = "/news/2024/example-article"
cases = []


def case(name, text, token, expected_allowed, path=P):
    cases.append((name, text, token, expected_allowed, path))


case("specific group blocks GPTBot", "User-agent: GPTBot\nDisallow: /\n", "GPTBot", False)
case("...but not Googlebot", "User-agent: GPTBot\nDisallow: /\n", "Googlebot", True)
case("stacked user-agents share rules", "User-agent: GPTBot\nUser-agent: CCBot\nDisallow: /\n", "CCBot", False)
case("token match is case-insensitive", "User-agent: gptbot\nDisallow: /\n", "GPTBot", False)
case("version suffix still matches", "User-agent: GPTBot/1.1\nDisallow: /\n", "GPTBot", False)
case("prefix is not a match", "User-agent: GPT\nDisallow: /\n", "GPTBot", True)
case("star deny applies to unlisted bot",
     "User-agent: *\nDisallow: /\n\nUser-agent: Googlebot\nAllow: /\n", "GPTBot", False)
case("allow-listed bot gets in",
     "User-agent: *\nDisallow: /\n\nUser-agent: Googlebot\nAllow: /\n", "Googlebot", True)
case("specific group replaces star, not added to it",
     "User-agent: *\nDisallow: /\nUser-agent: GPTBot\nDisallow: /private/\n", "GPTBot", True)
case("longest match wins", "User-agent: *\nDisallow: /news\nAllow: /news/2024\n", "X", True)
case("tie goes to allow", "User-agent: *\nDisallow: /news/\nAllow: /news/\n", "X", True)
case("wildcard in path", "User-agent: *\nDisallow: /*article\n", "X", False)
case("$ anchors the end", "User-agent: *\nDisallow: /news$\n", "X", True)
case("empty disallow allows all", "User-agent: *\nDisallow:\n", "X", True)
case("comments are ignored", "# block AI\nUser-agent: GPTBot # openai\nDisallow: / # everything\n", "GPTBot", False)
case("same bot in two groups is merged",
     "User-agent: GPTBot\nDisallow: /a/\n\nUser-agent: GPTBot\nDisallow: /news/\n", "GPTBot", False)
case("sitemap line doesn't split a group",
     "User-agent: GPTBot\nSitemap: https://x/s.xml\nUser-agent: CCBot\nDisallow: /\n", "GPTBot", False)
case("no groups at all means allowed", "Sitemap: https://x/s.xml\n", "GPTBot", True)
case("CRLF line endings", "User-agent: GPTBot\r\nDisallow: /\r\n", "GPTBot", False)
case("homepage-only allow still blocks content",
     "User-agent: GPTBot\nAllow: /$\nDisallow: /\n", "GPTBot", False)

failed = 0
for name, text, token, expected, path in cases:
    got = is_allowed(parse(text), token, path)
    if got != expected:
        failed += 1
        print(f"FAIL  {name}: expected {'allowed' if expected else 'blocked'}, got {'allowed' if got else 'blocked'}")

assert is_named(parse("User-agent: GPTBot\nDisallow: /\n"), "gptbot")
assert not is_named(parse("User-agent: *\nDisallow: /\n"), "GPTBot")

print(f"{len(cases) - failed}/{len(cases)} parser checks passed")
raise SystemExit(1 if failed else 0)
