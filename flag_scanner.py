#!/usr/bin/env python3
"""feature-flag-staleness-scanner.

Finds dead and dying feature flags in your codebase.

Static scans source files for common feature-flag usage patterns
(LaunchDarkly, Unleash, OpenFeature, Statsig, generic flag helpers,
env-var reads, and flags declared in YAML/JSON config), compares them
against a rollout-states JSON file you provide, and reports:

  STALE   - flag at 100% rollout (or marked retired) but still referenced
            in code -> dead code paths waiting to be cleaned up.
  ZOMBIE  - flag not touched in 90+ days (via `git log -S` on the flag key).
            Skipped gracefully when run outside a git repo.
  ORPHAN  - flag referenced in code but absent from the states file.

Exit code is 1 when any STALE flag is found, so it can gate CI on flag hygiene.

Stdlib only.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Flag usage patterns: (provider label, compiled regex, group index of key)
# ---------------------------------------------------------------------------
SDK_PATTERNS = [
    ("LaunchDarkly", re.compile(r'\bvariation(?:Detail)?\(\s*["\']([^"\']+)["\']'), 1),
    ("Unleash", re.compile(r'\bisEnabled\(\s*["\']([^"\']+)["\']'), 1),
    ("OpenFeature", re.compile(r'\bget(?:Boolean|String|Number|Object)Value\(\s*["\']([^"\']+)["\']'), 1),
    ("OpenFeature", re.compile(r'\bget_(?:boolean|string|float|integer|object)_value\(\s*["\']([^"\']+)["\']'), 1),
    ("Statsig", re.compile(r'\bcheckGate\(\s*["\']([^"\']+)["\']'), 1),
    ("Statsig", re.compile(r'\bcheck_gate\(\s*["\']([^"\']+)["\']'), 1),
    ("Statsig", re.compile(r'\bgetConfig\(\s*["\']([^"\']+)["\']'), 1),
    ("Env var", re.compile(r'\bos\.(?:getenv|environ(?:\[|\.get))\s*\(\s*["\']([A-Z][A-Z0-9_]{2,})["\']'), 1),
    ("Env var", re.compile(r'\bprocess\.env\.([A-Z][A-Z0-9_]{2,})'), 1),
    ("Env var", re.compile(r'\benv\(\s*["\']([A-Z][A-Z0-9_]{2,})["\']'), 1),
    ("Generic flag helper", re.compile(
        r'\b(?:isFeatureEnabled|featureEnabled|isFlagOn|flagEnabled|getFlag|'
        r'is_feature_enabled|flag_enabled)\(\s*["\']([^"\']+)["\']'), 1),
]

SOURCE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".go", ".java", ".rb", ".php", ".cs", ".rs", ".swift",
    ".kt", ".kts", ".m", ".mm", ".scala", ".sh", ".bash",
    ".c", ".cpp", ".h", ".hpp", ".vue", ".svelte",
}
CONFIG_EXTENSIONS = {".yaml", ".yml", ".json"}

SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__",
    "dist", "build", ".tox", "site-packages", ".idea",
    ".vscode", ".mypy_cache", ".pytest_cache", "vendor",
}

YAML_KEY_LINE = re.compile(r'^\s{0,8}([A-Za-z0-9_][A-Za-z0-9_.\-/]{1,80})\s*:')


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------
def iter_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            _, ext = os.path.splitext(name.lower())
            if ext in SOURCE_EXTENSIONS or ext in CONFIG_EXTENSIONS:
                yield os.path.join(dirpath, name)


def read_text_safely(path):
    try:
        with open(path, "r", encoding="utf-8", errors="strict") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


def scan_source_file(path, findings):
    text = read_text_safely(path)
    if text is None:
        return
    for lineno, line in enumerate(text.splitlines(), 1):
        for provider, pattern, group in SDK_PATTERNS:
            for match in pattern.finditer(line):
                key = match.group(group).strip()
                if key:
                    findings.setdefault(key, []).append(
                        {"file": path, "line": lineno, "provider": provider})


def scan_config_file(path, findings):
    text = read_text_safely(path)
    if text is None:
        return
    if path.endswith(".json"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for k, v in node.items():
                    if isinstance(k, str) and k.strip():
                        findings.setdefault(k.strip(), []).append(
                            {"file": path, "line": 0, "provider": "config"})
                    stack.append(v)
            elif isinstance(node, list):
                stack.extend(node)
    else:  # YAML: best-effort key extraction, stdlib only
        for lineno, line in enumerate(text.splitlines(), 1):
            m = YAML_KEY_LINE.match(line)
            if m:
                findings.setdefault(m.group(1).strip(), []).append(
                    {"file": path, "line": lineno, "provider": "config"})


def scan_codebase(root, exclude=()):
    """Return {flag_key: [ {file, line, provider}, ... ]}."""
    excluded = {os.path.realpath(p) for p in exclude}
    findings = {}
    for path in iter_files(root):
        if os.path.realpath(path) in excluded:
            continue
        _, ext = os.path.splitext(path.lower())
        if ext in CONFIG_EXTENSIONS:
            scan_config_file(path, findings)
        else:
            scan_source_file(path, findings)
    return findings


# ---------------------------------------------------------------------------
# Rollout states file
# ---------------------------------------------------------------------------
def load_states(path):
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    states = {}
    for flag, value in raw.items():
        rollout = None
        status = "active"
        if isinstance(value, (int, float)):
            rollout = float(value)
        elif isinstance(value, str):
            v = value.strip().lower()
            if v in ("retired", "removed", "deleted", "dead"):
                status = "retired"
            else:
                try:
                    rollout = float(v.rstrip("%"))
                except ValueError:
                    status = v
        elif isinstance(value, dict):
            if "rollout" in value:
                try:
                    rollout = float(str(value["rollout"]).rstrip("%"))
                except ValueError:
                    rollout = None
            s = str(value.get("status", "active")).strip().lower()
            if s in ("retired", "removed", "deleted", "dead"):
                status = "retired"
            elif s != "active":
                status = s
        states[flag] = {"rollout": rollout, "status": status}
    return states


# ---------------------------------------------------------------------------
# Git recency (zombie detection)
# ---------------------------------------------------------------------------
def is_git_repo(root):
    try:
        out = subprocess.run(
            ["git", "-C", root, "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=30)
        return out.returncode == 0 and out.stdout.strip() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def last_touch_epoch(root, flag_key, exclude=()):
    """Most recent commit timestamp touching flag_key, via `git log -S`.

    Paths in `exclude` (repo-relative) are ignored so bookkeeping files
    like the states file itself don't count as "touches".
    Returns None when unavailable (not a repo, no git, or never found).
    """
    cmd = ["git", "-C", root, "log", "-1", "--format=%ct",
           "-S", flag_key, "--", "."]
    cmd.extend(":!" + p for p in exclude)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    stamp = out.stdout.strip()
    if not stamp.isdigit():
        return None
    return int(stamp)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
def classify(findings, states, root, zombie_days=90, now=None, git_excludes=()):
    now = now or datetime.now(timezone.utc)
    in_git = is_git_repo(root)
    stale, zombie, orphan = [], [], []

    for flag, refs in sorted(findings.items()):
        state = states.get(flag)
        if state is None:
            orphan.append({"flag": flag, "refs": refs})
            continue
        rollout = state["rollout"]
        if rollout is not None and rollout >= 100:
            stale.append({"flag": flag, "refs": refs,
                          "rollout": rollout, "status": state["status"]})
        elif state["status"] == "retired":
            stale.append({"flag": flag, "refs": refs,
                          "rollout": rollout, "status": state["status"]})
        if in_git:
            touched = last_touch_epoch(root, flag, exclude=git_excludes)
            if touched is not None:
                age_days = (now - datetime.fromtimestamp(touched, tz=timezone.utc)).days
                if age_days >= zombie_days:
                    zombie.append({"flag": flag, "refs": refs, "age_days": age_days})

    return {
        "stale": stale,
        "zombie": zombie,
        "orphan": orphan,
        "git_available": in_git,
        "flags_found": len(findings),
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def ref_list(refs, root):
    seen = []
    for r in refs:
        rel = os.path.relpath(r["file"], root)
        tag = f"{rel}:{r['line']}" if r["line"] else rel
        if tag not in seen:
            seen.append(tag)
        if len(seen) >= 6:
            break
    more = f" (+{len(refs) - len(seen)} more)" if len(refs) > len(seen) else ""
    return ", ".join(seen) + more


def report_text(result, root):
    lines = []
    lines.append("feature-flag-staleness-scanner report")
    lines.append(f"flags found in code: {result['flags_found']}")
    if not result["git_available"]:
        lines.append("note: not a git repo - zombie detection skipped")
    lines.append("")

    if result["stale"]:
        lines.append(f"STALE ({len(result['stale'])}) - 100% rollout or retired, still referenced:")
        for item in result["stale"]:
            detail = f"rollout {item['rollout']}%" if item["rollout"] is not None else item["status"]
            lines.append(f"  - {item['flag']} [{detail}] -> {ref_list(item['refs'], root)}")
        lines.append("")
    if result["zombie"]:
        lines.append(f"ZOMBIE ({len(result['zombie'])}) - untouched for 90+ days:")
        for item in result["zombie"]:
            lines.append(f"  - {item['flag']} [last touched {item['age_days']}d ago] -> "
                         f"{ref_list(item['refs'], root)}")
        lines.append("")
    if result["orphan"]:
        lines.append(f"ORPHAN ({len(result['orphan'])}) - in code, missing from states file:")
        for item in result["orphan"]:
            lines.append(f"  - {item['flag']} -> {ref_list(item['refs'], root)}")
        lines.append("")

    if not result["stale"] and not result["zombie"] and not result["orphan"]:
        lines.append("clean - no stale, zombie, or orphan flags found.")
    return "\n".join(lines)


def report_json(result, root):
    serializable = {
        "flags_found": result["flags_found"],
        "git_available": result["git_available"],
        "stale": [{"flag": i["flag"], "rollout": i["rollout"], "status": i["status"],
                   "refs": [os.path.relpath(r["file"], root) for r in i["refs"]]}
                  for i in result["stale"]],
        "zombie": [{"flag": i["flag"], "age_days": i["age_days"]} for i in result["zombie"]],
        "orphan": [{"flag": i["flag"]} for i in result["orphan"]],
    }
    return json.dumps(serializable, indent=2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Find dead and dying feature flags in your codebase.")
    parser.add_argument("path", help="directory to scan")
    parser.add_argument("--states", required=True,
                        help="JSON file mapping flag key -> rollout %% (0-100), "
                             '"retired", or {"rollout": N}')
    parser.add_argument("--zombie-days", type=int, default=90,
                        help="days without a git touch before a flag is a ZOMBIE (default: 90)")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)

    root = os.path.abspath(args.path)
    if not os.path.isdir(root):
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2
    if not os.path.isfile(args.states):
        print(f"error: states file not found: {args.states}", file=sys.stderr)
        return 2

    findings = scan_codebase(root, exclude=[args.states])
    states = load_states(args.states)

    # Zombie detection must ignore the states file itself: committing it
    # is bookkeeping, not a real touch of the flag in code.
    states_abs = os.path.abspath(args.states)
    git_excludes = []
    try:
        if os.path.commonpath([root, states_abs]) == root:
            git_excludes = [os.path.relpath(states_abs, root)]
    except ValueError:
        pass
    result = classify(findings, states, root, zombie_days=args.zombie_days,
                      git_excludes=git_excludes)

    if args.format == "json":
        print(report_json(result, root))
    else:
        print(report_text(result, root))

    return 1 if result["stale"] else 0


if __name__ == "__main__":
    sys.exit(main())
