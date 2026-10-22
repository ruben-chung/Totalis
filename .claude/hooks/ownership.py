#!/usr/bin/env python3
"""PreToolUse hook (Edit|Write|MultiEdit|NotebookEdit): file-ownership guard.

Reads the tool call from stdin, looks the file up in .claude/ownership.json (glob -> owner),
and the calling session's label in .claude/session-owners.json (session_id -> label, written
by scripts/claim-owner). Unowned paths and the 'shared' owner pass; a session claimed as 'all' passes everywhere (used when one session takes over the whole tree). A path owned by another
label, or any owned path when the session has not claimed a label, returns permissionDecision
"ask" so a human confirms; it never hard-denies.
"""
import fnmatch, json, os, sys

def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    path = (payload.get("tool_input") or {}).get("file_path") or (payload.get("tool_input") or {}).get("notebook_path")
    if not path:
        return
    root = payload.get("cwd") or os.getcwd()
    try:
        rel = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    except ValueError:
        return
    if rel.startswith(".."):
        return
    cfg_path = os.path.join(root, ".claude", "ownership.json")
    try:
        owners = json.load(open(cfg_path)).get("owners", {})
    except Exception:
        return
    owner = None
    for pattern, label in owners.items():
        if fnmatch.fnmatch(rel, pattern) or (pattern.endswith("/**") and rel.startswith(pattern[:-3] + "/")):
            owner = label
            break
    if owner is None or owner == "shared":
        return
    sid = payload.get("session_id") or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    try:
        sessions = json.load(open(os.path.join(root, ".claude", "session-owners.json")))
    except Exception:
        sessions = {}
    mine = sessions.get(sid)
    if mine == owner or mine == "all":
        return
    if mine is None:
        reason = (f"{rel} is owned by '{owner}' and this session has not claimed an owner label. "
                  f"If you are the {owner} session run: scripts/claim-owner {owner}  (then retry). Otherwise ask the owner.")
    else:
        reason = f"{rel} is owned by '{owner}'; this session is '{mine}'. Ask the owner session to make the change, or confirm to override."
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                             "permissionDecisionReason": reason}}))

if __name__ == "__main__":
    main()
