"""Append-only local audit log of every mutation Claude makes to email."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("GMAIL_MCP_DATA_DIR", str(Path.home() / ".gmail-mcp")))
AUDIT_LOG = DATA_DIR / "audit.log"
DRAFT_BODIES_LOG = DATA_DIR / "draft_bodies.jsonl"


def log(action: str, account: str, **details):
    """Append a JSON line to the audit log. Never throws."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "account": account,
            **details,
        }
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        # Audit log is best-effort. Do not break the caller if it fails.
        pass


def log_draft_body(**fields):
    """Append a draft's full body + metadata to a separate JSONL log.

    Kept out of audit.log so view_audit_log stays readable. Used by the
    draft-automation metric (compare draft body vs what was actually sent).
    Never throws.
    """
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        entry = {"ts": datetime.now(timezone.utc).isoformat(), **fields}
        with DRAFT_BODIES_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _entries() -> list[dict]:
    """Every audit entry on disk, oldest first. The log is append-only and small
    (a few thousand lines after months), so reading it whole costs nothing."""
    if not AUDIT_LOG.exists():
        return []
    out = []
    for line in AUDIT_LOG.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def tail(n: int = 50, since: str = "", until: str = "",
         account: str = "", action: str = "") -> list[dict]:
    """The last n entries, optionally narrowed first.

    `since` / `until` are ISO dates or timestamps compared as text against `ts`
    ("2026-09-23" catches the whole day); `account` and `action` are substrings.
    Filtering happens BEFORE the tail, so a day far back is reachable without
    raising n to the size of the log.

    Why this exists: on 25.09 the question was who wrote six drafts into app@ on
    23.09. The tool took `n`, a call passed `limit=`, the argument was ignored and
    25 recent lines came back - from which the log looked like it only kept 25
    actions at all. It keeps everything; only the view was short. The answer, once
    the day was filtered, was that the log has no entries for 23.09 whatsoever.
    """
    rows = _entries()
    if since:
        rows = [r for r in rows if str(r.get("ts", "")) >= since]
    if until:
        rows = [r for r in rows if str(r.get("ts", "")) <= until]
    if account:
        rows = [r for r in rows if account.lower() in str(r.get("account", "")).lower()]
    if action:
        rows = [r for r in rows if action.lower() in str(r.get("action", "")).lower()]
    return rows[-n:]


def span() -> tuple[str, str, int]:
    """Oldest timestamp, newest timestamp and total number of entries."""
    rows = _entries()
    if not rows:
        return "", "", 0
    return str(rows[0].get("ts", "")), str(rows[-1].get("ts", "")), len(rows)
