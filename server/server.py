"""Gmail MCP Server. Multi-account Gmail for Claude with hardened defaults."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from mcp.server.fastmcp import FastMCP

import audit
import auth
import gmail_client
import storage

mcp = FastMCP("gmail-mcp")

# Attachments are always saved here — Claude cannot redirect saves elsewhere
DEFAULT_DOWNLOAD_DIR = Path.home() / "Downloads" / "gmail-mcp"

# Only files inside this directory may be attached to outgoing emails
SAFE_UPLOAD_DIR = Path.home() / "Downloads" / "gmail-mcp-uploads"


def _validate_upload_path(raw: str) -> Path:
    """Resolve path and ensure it is inside SAFE_UPLOAD_DIR. Raises ValueError otherwise."""
    SAFE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    resolved = Path(raw).expanduser().resolve()
    if not resolved.is_relative_to(SAFE_UPLOAD_DIR):
        raise ValueError(
            f"Attachment '{raw}' is outside the allowed upload directory "
            f"({SAFE_UPLOAD_DIR}). Move the file there first."
        )
    return resolved


def _require_creds(account: str):
    """Return creds or raise with a useful message for Claude to relay."""
    creds = auth.get_credentials(account)
    if not creds:
        raise ValueError(
            f"Account '{account}' is not authenticated. Call add_account('{account}') first."
        )
    return creds


# ---------- Account management ----------

@mcp.tool()
def list_accounts() -> str:
    """List every Gmail account currently authenticated with this server."""
    accounts = storage.list_accounts()
    if not accounts:
        return "No accounts authenticated yet. Use add_account(email) to connect one."
    return "Authenticated accounts:\n" + "\n".join(f"- {a}" for a in accounts)


@mcp.tool()
def add_account(email: str) -> str:
    """
    Authenticate a new Gmail account. Opens a browser for Google sign-in.
    The signed-in email must match `email` or tokens will not be saved.

    Args:
        email: The Gmail address you intend to sign in as (e.g., you@gmail.com).
    """
    try:
        saved = auth.authenticate_account(email)
        audit.log("add_account", saved)
        return f"Authenticated {saved}. You can now use it in other tools."
    except auth.IdentityMismatch as e:
        return f"Identity mismatch: {e}"
    except auth.OAuthConfigMissing as e:
        return f"OAuth setup incomplete: {e}"
    except Exception as e:
        return f"Authentication failed: {type(e).__name__}: {e}"


@mcp.tool()
def remove_account(email: str) -> str:
    """Remove an account from this server (revokes local tokens, not Google-side)."""
    if storage.remove_account(email):
        audit.log("remove_account", email)
        return f"Removed {email} from local storage."
    return f"Account {email} was not found."


# ---------- Read operations ----------

@mcp.tool()
def search_emails(account: str, query: str, max_results: int = 10) -> str:
    """
    Search emails using Gmail search syntax.

    Args:
        account: Gmail address to search in.
        query: Gmail search query. Examples:
               "is:unread"
               "from:boss@company.com newer_than:7d"
               "subject:invoice has:attachment"
               "label:important -label:newsletters"
        max_results: Max results to return (default 10, cap 50).
    """
    creds = _require_creds(account)
    max_results = min(max_results, 50)
    try:
        messages = gmail_client.search_messages(creds, query, max_results)
    except Exception as e:
        return f"Search failed: {type(e).__name__}: {e}"

    if not messages:
        return f"No emails found matching: {query}"

    lines = [f"Found {len(messages)} emails in {account}:\n"]
    for m in messages:
        lines.append(
            f"ID: {m['id']}  Thread: {m['thread_id']}\n"
            f"From: {m['from']}\n"
            f"Subject: {m['subject']}\n"
            f"Date: {m['date']}\n"
            f"Preview: {m['snippet']}\n"
            + "-" * 50
        )
    return "\n".join(lines)


@mcp.tool()
def read_email(account: str, message_id: str, full: bool = False) -> str:
    """
    Read the full content of an email by ID.

    Args:
        account: Gmail address.
        message_id: Message ID from search_emails.
        full: If True, return the full body even if very long. Default False (truncated at 20k chars).
    """
    creds = _require_creds(account)
    try:
        msg = gmail_client.get_message(creds, message_id, full=full)
    except Exception as e:
        return f"Read failed: {type(e).__name__}: {e}"

    out = [
        f"Subject: {msg['subject']}",
        f"From: {msg['from']}",
        f"To: {msg['to']}",
    ]
    if msg["cc"]:
        out.append(f"CC: {msg['cc']}")
    out.append(f"Date: {msg['date']}")
    out.append(f"Thread: {msg['thread_id']}")
    out.append(f"Labels: {', '.join(msg['labels'])}")

    if msg["attachments"]:
        out.append("Attachments:")
        for a in msg["attachments"]:
            out.append(f"  - {a['filename']} ({a['mime_type']}, {a['size']} bytes)  id={a['attachment_id']}")

    out.append("")
    out.append("--- Body ---")
    out.append("")
    out.append(msg["body"])

    if msg["truncated"]:
        out.append("\n[Body truncated. Call read_email(..., full=True) for the full text.]")
    return "\n".join(out)


@mcp.tool()
def get_thread(account: str, thread_id: str, full: bool = False) -> str:
    """
    Read an entire email thread in chronological order.
    Use this before drafting a reply so Claude has full conversation context.
    """
    creds = _require_creds(account)
    try:
        thread = gmail_client.get_thread(creds, thread_id, full=full)
    except Exception as e:
        return f"Thread fetch failed: {type(e).__name__}: {e}"

    lines = [f"Thread {thread_id} ({thread['message_count']} messages):\n"]
    for i, m in enumerate(thread["messages"], 1):
        lines.append(f"=== Message {i} of {thread['message_count']} ===")
        lines.append(f"From: {m['from']}")
        lines.append(f"Date: {m['date']}")
        lines.append(f"Subject: {m['subject']}")
        if m["attachments"]:
            lines.append(f"Attachments: {', '.join(a['filename'] for a in m['attachments'])}")
        lines.append("")
        lines.append(m["body"])
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
def save_attachment(account: str, message_id: str, attachment_id: str,
                    filename: str) -> str:
    """
    Download an attachment from an email and save it to ~/Downloads/gmail-mcp/.

    Args:
        account: Gmail address.
        message_id: Source message ID.
        attachment_id: Attachment ID (from read_email output).
        filename: Name to save as.
    """
    creds = _require_creds(account)
    try:
        path = gmail_client.get_attachment(creds, message_id, attachment_id, DEFAULT_DOWNLOAD_DIR, filename)
    except Exception as e:
        return f"Attachment download failed: {type(e).__name__}: {e}"

    audit.log("save_attachment", account, message_id=message_id, path=str(path))
    return f"Saved to {path}"


# ---------- Draft + send (confirmation flow) ----------

@mcp.tool()
def draft_email(account: str, to: str, subject: str, body: str,
                cc: str = "", bcc: str = "",
                attachments: str = "") -> str:
    """
    Create a Gmail draft. Does NOT send. Returns the draft_id for confirmation.
    Call send_draft(account, draft_id) to actually send it.

    Args:
        account: Gmail address to send from.
        to: Recipient(s), comma-separated.
        subject: Email subject.
        body: Plain-text body.
        cc: Optional CC recipients.
        bcc: Optional BCC recipients.
        attachments: Optional comma-separated list of absolute file paths to attach.
    """
    creds = _require_creds(account)
    try:
        attach_paths = [_validate_upload_path(p.strip()) for p in attachments.split(",") if p.strip()]
    except ValueError as e:
        return f"Attachment rejected: {e}"
    try:
        result = gmail_client.create_draft(
            creds, from_email=account, to=to, subject=subject, body=body,
            cc=cc, bcc=bcc, attachments=attach_paths
        )
    except Exception as e:
        return f"Draft creation failed: {type(e).__name__}: {e}"

    audit.log("draft_email", account, draft_id=result["draft_id"], to=to, subject=subject)
    audit.log_draft_body(action="draft_email", account=account, draft_id=result["draft_id"],
                         to=to, subject=subject, body=body)
    return (
        f"Draft created (NOT sent).\n"
        f"draft_id: {result['draft_id']}\n"
        f"To: {to}\n"
        f"Subject: {subject}\n\n"
        f"To send: send_draft('{account}', '{result['draft_id']}')\n"
        f"To discard: discard_draft('{account}', '{result['draft_id']}')"
    )


@mcp.tool()
def draft_reply(account: str, message_id: str, body: str,
                include_original: bool = False,
                attachments: str = "",
                cc: str = "", bcc: str = "") -> str:
    """
    Create a reply draft to an existing message. Preserves threading headers.
    Does NOT send. Use send_draft to confirm.

    Args:
        account: Gmail address to send from.
        message_id: The message being replied to.
        body: Your reply text.
        include_original: If True, quote the original message below your reply.
        attachments: Comma-separated absolute file paths.
        cc: Optional CC recipients (comma-separated).
        bcc: Optional BCC recipients (comma-separated).
    """
    creds = _require_creds(account)
    try:
        original = gmail_client.get_message(creds, message_id, full=True)
    except Exception as e:
        return f"Could not fetch original message: {type(e).__name__}: {e}"

    subject = original["subject"]
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject

    # Reply goes to the original sender
    reply_to = original["from"]
    in_reply_to = original["message_id_header"]

    reply_body = body
    if include_original:
        # Quote only the newest message's own content: the plain-text body
        # already carries the full nested history (">"-prefixed) and old
        # signatures with raw tracking URLs — re-quoting it builds a ">>>"
        # ladder in the outgoing mail.
        fresh_lines = []
        for line in original["body"].splitlines():
            if line.lstrip().startswith(">"):
                break
            fresh_lines.append(line)
        while fresh_lines and (
            not fresh_lines[-1].strip()
            or fresh_lines[-1].startswith("On ")
            or fresh_lines[-1].strip() == "wrote:"
        ):
            fresh_lines.pop()
        quoted = "\n".join("> " + line for line in fresh_lines)
        reply_body = f"{body}\n\nOn {original['date']}, {original['from']} wrote:\n{quoted}"

    try:
        attach_paths = [_validate_upload_path(p.strip()) for p in attachments.split(",") if p.strip()]
    except ValueError as e:
        return f"Attachment rejected: {e}"

    try:
        result = gmail_client.create_draft(
            creds, from_email=account,
            to=reply_to, subject=subject, body=reply_body,
            cc=cc, bcc=bcc,
            in_reply_to=in_reply_to, references=in_reply_to,
            thread_id=original["thread_id"],
            attachments=attach_paths,
        )
    except Exception as e:
        return f"Reply draft failed: {type(e).__name__}: {e}"

    audit.log("draft_reply", account, draft_id=result["draft_id"],
              reply_to_message=message_id, subject=subject,
              cc=cc, bcc=bcc)
    audit.log_draft_body(action="draft_reply", account=account, draft_id=result["draft_id"],
                         reply_to_message=message_id, thread_id=original["thread_id"],
                         subject=subject, body=body)
    cc_line = f"Cc: {cc}\n" if cc else ""
    bcc_line = f"Bcc: {bcc}\n" if bcc else ""
    return (
        f"Reply draft created (NOT sent).\n"
        f"draft_id: {result['draft_id']}\n"
        f"To: {reply_to}\n"
        f"{cc_line}{bcc_line}"
        f"Subject: {subject}\n\n"
        f"Preview of reply body:\n{reply_body[:500]}{'...' if len(reply_body) > 500 else ''}\n\n"
        f"To send: send_draft('{account}', '{result['draft_id']}')"
    )


@mcp.tool()
def send_draft(account: str, draft_id: str) -> str:
    """
    Send a previously-created draft. This is the confirmation step for draft_email and draft_reply.

    Args:
        account: Gmail address.
        draft_id: Draft ID from draft_email or draft_reply.
    """
    creds = _require_creds(account)
    try:
        result = gmail_client.send_draft(creds, draft_id)
    except Exception as e:
        return f"Send failed: {type(e).__name__}: {e}"

    audit.log("send_draft", account, draft_id=draft_id, message_id=result["message_id"])
    return f"Sent. Message ID: {result['message_id']}"


@mcp.tool()
def discard_draft(account: str, draft_id: str) -> str:
    """Delete an unsent draft."""
    creds = _require_creds(account)
    try:
        gmail_client.discard_draft(creds, draft_id)
    except Exception as e:
        return f"Discard failed: {type(e).__name__}: {e}"
    audit.log("discard_draft", account, draft_id=draft_id)
    return f"Discarded draft {draft_id}."


@mcp.tool()
def list_drafts(account: str, max_results: int = 10) -> str:
    """List pending (unsent) drafts in an account."""
    creds = _require_creds(account)
    try:
        drafts = gmail_client.list_drafts(creds, max_results)
    except Exception as e:
        return f"List failed: {type(e).__name__}: {e}"

    if not drafts:
        return "No drafts."
    lines = [f"{len(drafts)} drafts:"]
    for d in drafts:
        lines.append(f"- {d['draft_id']}: to {d['to']} | {d['subject']}\n    {d['snippet']}")
    return "\n".join(lines)


# ---------- Labels / organization ----------

@mcp.tool()
def get_labels(account: str) -> str:
    """List all Gmail labels/folders in an account."""
    creds = _require_creds(account)
    try:
        labels = gmail_client.get_labels(creds)
    except Exception as e:
        return f"Failed: {type(e).__name__}: {e}"
    return "Labels:\n" + "\n".join(f"- {l['name']} (id={l['id']})" for l in labels)


@mcp.tool()
def archive_email(account: str, message_id: str) -> str:
    """Archive an email (removes it from the inbox but keeps it searchable)."""
    creds = _require_creds(account)
    try:
        gmail_client.modify_labels(creds, message_id, remove_labels=["INBOX"])
    except Exception as e:
        return f"Archive failed: {type(e).__name__}: {e}"
    audit.log("archive", account, message_id=message_id)
    return f"Archived {message_id}."


@mcp.tool()
def trash_email(account: str, message_id: str) -> str:
    """Move an email to trash (reversible for 30 days)."""
    creds = _require_creds(account)
    try:
        gmail_client.trash_message(creds, message_id)
    except Exception as e:
        return f"Trash failed: {type(e).__name__}: {e}"
    audit.log("trash", account, message_id=message_id)
    return f"Moved {message_id} to trash."


@mcp.tool()
def mark_as_read(account: str, message_id: str) -> str:
    """Mark an email as read."""
    creds = _require_creds(account)
    try:
        gmail_client.modify_labels(creds, message_id, remove_labels=["UNREAD"])
    except Exception as e:
        return f"Failed: {type(e).__name__}: {e}"
    audit.log("mark_read", account, message_id=message_id)
    return f"Marked {message_id} as read."


@mcp.tool()
def mark_as_unread(account: str, message_id: str) -> str:
    """Mark an email as unread."""
    creds = _require_creds(account)
    try:
        gmail_client.modify_labels(creds, message_id, add_labels=["UNREAD"])
    except Exception as e:
        return f"Failed: {type(e).__name__}: {e}"
    audit.log("mark_unread", account, message_id=message_id)
    return f"Marked {message_id} as unread."


@mcp.tool()
def apply_label(account: str, message_id: str, label_id: str) -> str:
    """Apply a label to a message by label ID. Use get_labels to find IDs."""
    creds = _require_creds(account)
    try:
        gmail_client.modify_labels(creds, message_id, add_labels=[label_id])
    except Exception as e:
        return f"Failed: {type(e).__name__}: {e}"
    audit.log("apply_label", account, message_id=message_id, label_id=label_id)
    return f"Applied label {label_id} to {message_id}."


@mcp.tool()
def remove_label(account: str, message_id: str, label_id: str) -> str:
    """Remove a label from a message by label ID."""
    creds = _require_creds(account)
    try:
        gmail_client.modify_labels(creds, message_id, remove_labels=[label_id])
    except Exception as e:
        return f"Failed: {type(e).__name__}: {e}"
    audit.log("remove_label", account, message_id=message_id, label_id=label_id)
    return f"Removed label {label_id} from {message_id}."


# ---------- Audit ----------

@mcp.tool()
def view_audit_log(n: int = 100, since: str = "", until: str = "",
                   account: str = "", action: str = "") -> str:
    """Actions this server took on your Gmail accounts, newest last.

    n: how many entries to show (default 100).
    since / until: ISO date or timestamp, e.g. since="2026-09-23" until="2026-09-24"
        for a single day. Filters run BEFORE the cut, so an old day is reachable
        without asking for thousands of lines.
    account / action: case-insensitive substrings, e.g. account="app@laoshi.io",
        action="draft".

    The log covers ONLY this server. Anything writing to the same mailbox with its
    own credentials - the support automation in GitHub Actions, a person in the
    Gmail interface - leaves no trace here, so an empty result means "not us",
    not "nobody".
    """
    entries = audit.tail(n, since=since, until=until, account=account, action=action)
    if not entries:
        first, last, total = audit.span()
        if not total:
            return "No audit entries yet."
        return (f"Nothing matched. The log holds {total} entries, {first} .. {last} — "
                f"so this is an answer about the log, not an empty log.")
    first, last, total = audit.span()
    head = f"{len(entries)} of {total} entries (log spans {first[:10]} .. {last[:10]})"
    lines = [head + ":"]
    for e in entries:
        base = f"[{e.get('ts')}] {e.get('action')} on {e.get('account')}"
        extras = {k: v for k, v in e.items() if k not in ("ts", "action", "account")}
        if extras:
            base += " | " + ", ".join(f"{k}={v}" for k, v in extras.items())
        lines.append(base)
    return "\n".join(lines)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
