#!/usr/bin/env python3
"""nginx / OpenResty access-log analyzer (CLI).

First feature: print the log lines whose HTTP status code matches the
codes / class-patterns the user asks for.

  python3 log_analyze.py -c 4xx -c 302 /var/log/nginx/access.log
  python3 log_analyze.py --codes 5xx,404,302 /var/log/nginx/access.log
  tail -f access.log | python3 log_analyze.py -c 5xx -

Supports the same two log formats as nginx_monitor.py:
  - JSON  : a line whose first non-space char is '{'  (status = "status" key)
  - "perf": plain text where the 3-digit status follows the quoted request

Standard library only — runs on a bare server with just python3.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

# ───────────────────────── log parsing ─────────────────────────

# perf format: ... "GET /x HTTP/1.1" 200 1234 ...  — first match is the status
_REQ = re.compile(r'"[A-Z][^"]*"\s+(\d{3})\b')


def status_of(line: str) -> int | None:
    """Return the HTTP status of a log line, or None if it can't be parsed."""
    s = line.strip()
    if not s:
        return None
    # 1) JSON format (log_format ... escape=json '{...}')
    if s[0] == "{":
        try:
            d = json.loads(s)
        except json.JSONDecodeError:
            return None
        try:
            return int(d["status"])
        except (KeyError, TypeError, ValueError):
            return None
    # 2) plain-text perf format
    m = _REQ.search(s)
    if not m:
        return None
    return int(m.group(1))


# ───────────────────────── matchers ─────────────────────────

# exact 3-digit code (100-599) or a class pattern like 4xx / 5XX
_EXACT = re.compile(r"^\d{3}$")
_CLASS = re.compile(r"^([1-5])xx$", re.IGNORECASE)


def parse_matchers(tokens: list[str]) -> tuple[set[int], set[int]]:
    """Turn user tokens into (exact_codes, classes).

    classes holds the leading digit, e.g. 4 for the token "4xx".
    Raises ValueError("invalid code/pattern: '<tok>'") on a bad token.
    """
    exact: set[int] = set()
    classes: set[int] = set()
    for raw in tokens:
        tok = raw.strip()
        cm = _CLASS.match(tok)
        if cm:
            classes.add(int(cm.group(1)))
            continue
        if _EXACT.match(tok) and 100 <= int(tok) <= 599:
            exact.add(int(tok))
            continue
        raise ValueError(f"invalid code/pattern: '{raw}'")
    return exact, classes


def matches(status: int, exact: set[int], classes: set[int]) -> bool:
    """True if status is an exact match or falls in a selected class."""
    return status in exact or status // 100 in classes


# ───────────────────────── input ─────────────────────────


def collect_tokens(args: argparse.Namespace) -> list[str]:
    """Merge -c/--code and --codes into one token list.

    Empty / whitespace-only pieces are dropped so a benign trailing comma
    (e.g. --codes 5xx,) or an empty -c value doesn't abort the whole run;
    genuine garbage tokens are still validated and rejected downstream.
    """
    tokens: list[str] = [t for t in (args.code or []) if t.strip()]
    if args.codes:
        tokens += [t for t in args.codes.split(",") if t.strip()]
    return tokens


def iter_lines(path: str):
    """Yield raw lines from `path` (or stdin when path is '-').

    Streams line by line so it copes with huge files and live `tail -f`.
    """
    if path == "-":
        yield from sys.stdin
        return
    with open(path, "r", errors="replace") as fh:
        yield from fh


# ───────────────────────── filter (extension point) ─────────────────────────


def run_filter(lines, exact: set[int], classes: set[int], out) -> None:
    """Print every line whose status matches; skip the rest silently.

    Future sub-features / filters (by method, URI, IP, time range, latency,
    rate-limiting, counting, output formats, ...) would plug in here — each
    line already passes through this single chokepoint.
    """
    for line in lines:
        st = status_of(line)
        if st is None:
            continue
        if matches(st, exact, classes):
            out.write(line if line.endswith("\n") else line + "\n")
            out.flush()


# ───────────────────────── CLI ─────────────────────────


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="log_analyze.py",
        description="Filter nginx/OpenResty access-log lines by HTTP status "
                    "code (exact codes and 1xx-5xx class patterns).",
        epilog=(
            "examples:\n"
            "  python3 log_analyze.py -c 4xx -c 302 --access-log /var/log/nginx/access.log\n"
            "  python3 log_analyze.py --codes 5xx,404,302 /var/log/nginx/access.log\n"
            "  tail -f access.log | python3 log_analyze.py -c 5xx -\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("path", nargs="?", default=None,
                    help="log file ('-' = stdin); overrides --access-log")
    ap.add_argument("--access-log",
                    default="/usr/local/openresty/nginx/logs/access.log",
                    help="path to the access log (default: %(default)s)")
    ap.add_argument("-c", "--code", action="append", metavar="CODE",
                    help="status code or class to match (repeatable), e.g. 404 or 5xx")
    ap.add_argument("--codes", metavar="LIST",
                    help="comma-separated codes/classes, e.g. 5xx,404,302")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)

    tokens = collect_tokens(args)
    if not tokens:
        ap.error("no codes supplied — give at least one -c/--code or --codes")

    try:
        exact, classes = parse_matchers(tokens)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    path = args.path if args.path is not None else args.access_log

    try:
        run_filter(iter_lines(path), exact, classes, sys.stdout)
    except BrokenPipeError:
        # downstream (e.g. 'head') closed the pipe — exit quietly.
        # Redirect stdout to devnull so the interpreter's final flush on
        # exit doesn't re-raise BrokenPipeError. (Must precede OSError:
        # BrokenPipeError is a subclass of OSError.)
        _silence_broken_pipe()
        return 0
    except FileNotFoundError:
        print(f"error: log file not found: {path}", file=sys.stderr)
        return 1
    except (PermissionError, IsADirectoryError, OSError) as e:
        print(f"error: cannot read {path}: {e}", file=sys.stderr)
        return 1
    return 0


class _NullWriter:
    """No-op stdout replacement so the interpreter's final flush is silent."""

    def write(self, _data):
        return 0

    def flush(self):
        pass


def _silence_broken_pipe() -> None:
    """Swap stdout for a sink so the final flush can't re-raise."""
    sys.stdout = _NullWriter()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except BrokenPipeError:
        sys.exit(0)
