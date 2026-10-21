#!/usr/bin/env python3
"""PostToolUse hook (Edit|Write): run the test file(s) matching an edited module.

rfqbot/<mod>.py          -> tests/test_<mod>.py
rfqbot/<pkg>/<mod>.py    -> tests/test_<pkg>_<mod>.py, else tests/test_<mod>.py, else tests/test_<modprefix>_*.py,
                            else tests/test_<pkg>_*.py, else tests/test_<pkg>.py
tests/test_*.py          -> itself
Runs .venv/bin/python -m pytest -q on them (120 s cap) and, on failure, injects the tail of the
output back into the model's context. Never blocks the edit.
"""
import glob, json, os, subprocess, sys

def targets(rel):
    if rel.startswith("tests/test_") and rel.endswith(".py"):
        return [rel]
    if not (rel.startswith("rfqbot/") and rel.endswith(".py")):
        return []
    parts = rel[len("rfqbot/"):-3].split("/")
    if parts[-1] in ("__init__", "__main__"):
        parts = parts[:-1] or []
    if not parts:
        return []
    if len(parts) == 1:
        return [p for p in [f"tests/test_{parts[0]}.py"] if os.path.exists(p)]
    pkg, mod = parts[0], parts[-1]
    for cand in ([f"tests/test_{pkg}_{mod}.py"], [f"tests/test_{mod}.py"], sorted(glob.glob(f"tests/test_{mod.split('_')[0]}_*.py")) if "_" in mod else [],
                 sorted(glob.glob(f"tests/test_{pkg}_*.py")), [f"tests/test_{pkg}.py"]):
        found = [c for c in cand if os.path.exists(c)]
        if found:
            return found
    return []

def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    path = (payload.get("tool_input") or {}).get("file_path")
    root = payload.get("cwd") or os.getcwd()
    if not path:
        return
    rel = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    os.chdir(root)
    files = targets(rel)
    if not files:
        return
    py = ".venv/bin/python" if os.path.exists(".venv/bin/python") else sys.executable
    try:
        r = subprocess.run([py, "-m", "pytest", "-q", "-x", "--no-header", *files],
                           capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        print(json.dumps({"systemMessage": f"tests for {rel} timed out (120 s): {' '.join(files)}"}))
        return
    tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-25:])
    if r.returncode == 0:
        summary = tail.splitlines()[-1] if tail else "ok"
        print(json.dumps({"systemMessage": f"tests ok for {rel}: {summary}", "suppressOutput": True}))
    else:
        print(json.dumps({"systemMessage": f"TESTS FAILED after editing {rel} ({' '.join(files)})",
                          "hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                 "additionalContext": f"pytest failed for {' '.join(files)} after you edited {rel}:\n{tail}"}}))

if __name__ == "__main__":
    main()
