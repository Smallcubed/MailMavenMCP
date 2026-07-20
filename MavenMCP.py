"""
MailMaven MCP Server
=============================================================
Uses standard AppleScript "with transaction / end transaction" blocks
for all object-creation operations (make new query, compose, reply,
forward) as required by MailMaven's AppleScript dictionary.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from typing import Any

from mcp.server.fastmcp import FastMCP

APP_NAME = "MailMaven"
DEFAULT_LIMIT = 50
MAX_LIMIT = 500


# ---------------------------------------------------------------------------
# AppleScript bridge
# ---------------------------------------------------------------------------

def _run_applescript(script: str, timeout: int = 60) -> str:
    """Execute an AppleScript string via osascript and return its text output."""
    proc = subprocess.run(
        ["osascript", "-l", "AppleScript", "-e", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        msg = proc.stderr.strip().replace("\n", " ")
        raise RuntimeError(f"AppleScript error: {msg}")
    return proc.stdout.strip()


def _from_account_clause(value: str) -> tuple[str, str]:
    """Build the setup statement and ` from ...` clause for a from_account value.

    MailMaven's `from` parameter accepts text, an account object, or an
    address object. Plain text is parsed by MailMaven as a raw email
    address / RFC2822 string, so passing an account *name* (e.g. "SC")
    as text fails to resolve (no "@", not a valid address) and MailMaven
    reports "No Account found for from address (null)".

    Account names are resolved via direct by-name element access
    (``account "SC"``), which needs no separate setup statement — it's a
    plain object specifier, not a whose-clause. Anything containing "@"
    is treated as an address string and passed straight through as text.

    Returns:
        (setup_line, from_clause) — setup_line is always "" here, kept
        for a consistent call signature at both call sites.
    """
    if "@" in value:
        return "", f' from {_to_applescript(value)}'
    return "", f' from account {_to_applescript(value)}'


def _tell(body: str) -> str:
    """Wrap body in a tell block targeting MailMaven.

    The transaction block (when needed) is placed *inside* the tell block
    in the body itself, using the standard AppleScript syntax:

        tell application "MailMaven"
            with transaction
                -- create objects here
            end transaction
        end tell
    """
    return f'tell application "{APP_NAME}"\n{body}\nend tell'


class ASConst:
    """Marks a value that must be emitted as a bareword AppleScript constant
    (e.g. ``equals to``) rather than a quoted string (e.g. ``"equals to"``).

    MailMaven's dictionary defines several properties - criterion
    ``qualifier``, ``flag``, ``importance`` level, offset ``unit`` - as
    enumerated *type class* values, not text. Wrapping the value in
    ASConst guarantees it is written as a bare identifier, which is what
    the dictionary actually expects (avoids the -1700 coercion error).
    """

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover
        return f"ASConst({self.name!r})"


# Verified against MailMaven.sdef.
QUALIFIER_INCLUDES = ASConst("includes")
QUALIFIER_EQUALS_TO = ASConst("equals to")
QUALIFIER_GREATER = ASConst("greater")
QUALIFIER_LESS = ASConst("less")

UNIT_DAY = ASConst("day")

FLAG_CONSTANTS = {
    "no flag": ASConst("no flag"),
    "red": ASConst("red"),
    "orange": ASConst("orange"),
    "yellow": ASConst("yellow"),
    "green": ASConst("green"),
    "blue": ASConst("blue"),
    "purple": ASConst("purple"),
    "grey": ASConst("grey"),
}

IMPORTANCE_CONSTANTS = {
    "very low": ASConst("very low"),
    "low": ASConst("low"),
    "normal": ASConst("normal"),
    "high": ASConst("high"),
    "very high": ASConst("very high"),
}


def _json_record(pairs: dict[str, Any]) -> str:
    """Build an AppleScript record literal from a Python dict."""
    parts: list[str] = []
    for key, val in pairs.items():
        parts.append(f"{key}:{_to_applescript(val)}")
    return "{" + ", ".join(parts) + "}"


def _to_applescript(val: Any) -> str:
    """Convert a Python value to its AppleScript literal representation."""
    if val is None:
        return "missing value"
    if isinstance(val, ASConst):
        return val.name
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, datetime):
        return val.strftime('date "%A, %B %d, %Y at %I:%M:%S %p"')
    if isinstance(val, list):
        return "{" + ", ".join(_to_applescript(v) for v in val) + "}"
    if isinstance(val, str):
        escaped = val.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    raise TypeError(f"Cannot convert {type(val)} to AppleScript")


def safe_str_handler() -> str:
    """AppleScript handler that safely converts any value to a string."""
    return """
on safeStr(v)
    try
        if v is missing value then return ""
        if class of v is date then
            return (v as string)
        end if
        if class of v is list then
            set _out to ""
            repeat with _i in v
                set _out to _out & (my safeStr(_i)) & ", "
            end repeat
            if length of _out > 1 then
                return text 1 thru -3 of _out
            end if
            return ""
        end if
        return v as string
    on error
        return ""
    end try
end safeStr
"""


def _message_lookup_snippet(ids: int | list[int], query_name: str = "mcp_lookup") -> str:
    """AppleScript lines that resolve one or more messages by their integer
    Maven ``identifier`` (NOT the text ``maven identifier`` property) and
    assign the result to ``_msgs``.

    Two strategies are used depending on how many identifiers are passed:

    - **Fewer than 5 identifiers**: Each message is resolved directly via
      the ``message <id>`` accessor inside a loop. No query or criterion
      objects are created, so this path does **not** require a
      ``with transaction`` block (though it is still safe inside one).
      Each lookup is wrapped in ``try/on error`` so that a stale or
      missing identifier is silently skipped rather than aborting the
      entire operation.

    - **5 or more identifiers**: Messages are resolved via a query using
      MailMaven's dedicated ``message criterion`` / ``message identifiers``
      property (see MailMaven.sdef). This path **must** be used inside a
      ``with transaction ... end transaction`` block - and anything that
      reads from ``_msgs`` must also stay inside that same block - since
      MailMaven's query/criterion objects are transient and become invalid
      once referenced after their creating transaction ends.
    """
    id_list = ids if isinstance(ids, list) else [ids]

    if len(id_list) < 5:
        # Direct-accessor path: resolve each message by "message <id>".
        # No query/criterion objects are created, so no transaction is
        # required (but the snippet is still safe inside one).
        lines = ["set _msgs to {}"]
        for mid in id_list:
            lines.append(f"        try")
            lines.append(f"            set end of _msgs to message id {mid}")
            lines.append("        end try")
        return "\n".join(lines)

    # Query/criterion path: use a message criterion with the
    # "message identifiers" property.  Requires a with-transaction block.
    crit_props = _json_record({"qualifier": QUALIFIER_INCLUDES, "message identifiers": id_list})
    return (
        f'set _q to make new query with properties {{name:"{query_name}"}}\n'
        '        set logic of _q to all criteria\n'
        f'        make new message criterion with properties {crit_props} at end of criteria of _q\n'
        '        set _msgs to messages of _q'
    )

def _parse_messages_tsv(raw: str, include_content: bool = False) -> list[dict]:
    """Parse tab-separated output from message-fetching scripts."""
    if not raw:
        return []
    lines = raw.strip().split("\r")
    if not lines:
        return []
    if len(lines) == 1 and "\n" in lines[0]:
        lines = lines[0].split("\n")
    if len(lines) < 2:
        return []
    headers = lines[0].split("\t")
    messages: list[dict] = []
    for line in lines[1:]:
        vals = line.split("\t")
        msg = {}
        for i, h in enumerate(headers):
            msg[h] = vals[i] if i < len(vals) else ""
        messages.append(msg)
    return messages


# ---------------------------------------------------------------------------
# MCP server & tools
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "MailMaven",
    instructions=(
        "MailMaven MCP server."
        "Tools talk to the MailMaven macOS email client via AppleScript. "
        "MailMaven must be installed and running."
    ),
)


# ── App-level ──────────────────────────────────────────────────────────────

@mcp.tool()
def get_app_info() -> dict:
    """Return MailMaven application name, version, and frontmost status."""
    script = _tell("""
    set _info to (name as string) & "|" & (version as string) & "|" & (frontmost as string)
    return _info
    """)
    raw = _run_applescript(script)
    parts = raw.split("|")
    return {
        "name": parts[0] if len(parts) > 0 else APP_NAME,
        "version": parts[1] if len(parts) > 1 else "",
        "frontmost": parts[2].lower() == "true" if len(parts) > 2 else False,
    }


@mcp.tool()
def check_new_mail(account_name: str | None = None) -> str:
    """Check for new mail. If account_name is omitted, checks all accounts."""
    if account_name:
        body = f'check new mail (first account whose name is "{account_name}")'
    else:
        body = "check new mail"
    script = _tell(body)
    _run_applescript(script)
    return f"Mail check initiated for {'account: ' + account_name if account_name else 'all accounts'}."


# ── Accounts ───────────────────────────────────────────────────────────────

@mcp.tool()
def list_accounts() -> list[dict]:
    """List all configured email accounts in MailMaven."""
    script = _tell("""
    set _out to ""
    repeat with a in accounts
        set _out to _out & (identifier of a as string) & "\\t" & (my safeStr(name of a)) & "\\n"
    end repeat
    return _out
    """)
    raw = _run_applescript(safe_str_handler() + "\n" + script)
    accounts: list[dict] = []
    for line in raw.strip().split("\n"):
        if not line.strip() or "\t" not in line:
            continue
        aid, aname = line.split("\t", 1)
        accounts.append({"id": int(aid) if aid.isdigit() else aid, "name": aname})
    return accounts


# ── Mailboxes ──────────────────────────────────────────────────────────────

@mcp.tool()
def list_mailboxes(account_name: str | None = None) -> list[dict]:
    """List mailboxes for a specific account, or all mailboxes if no account given."""
    if account_name:
        body = f'''
        set _out to ""
        set _acct to first account whose name is "{account_name}"
        repeat with mb in mailboxes of _acct
            set _out to _out & (my safeStr(name of mb)) & "\\t" & (my safeStr(path of mb)) & "\\t" & (name of _acct) & "\\n"
        end repeat
        return _out
        '''
    else:
        body = '''
        set _out to ""
        repeat with mb in mailboxes
            set _acctName to ""
            try
                set _acctName to name of account of mb
            end try
            set _out to _out & (my safeStr(name of mb)) & "\\t" & (my safeStr(path of mb)) & "\\t" & _acctName & "\\n"
        end repeat
        return _out
        '''
    script = safe_str_handler() + "\n" + _tell(body)
    raw = _run_applescript(script)
    mailboxes: list[dict] = []
    for line in raw.strip().split("\n"):
        if not line.strip() or "\t" not in line:
            continue
        parts = line.split("\t")
        mailboxes.append({
            "name": parts[0],
            "path": parts[1] if len(parts) > 1 else "",
            "account": parts[2] if len(parts) > 2 else "",
        })
    return mailboxes


@mcp.tool()
def get_unified_mailboxes() -> dict:
    """Return each account's special mailboxes (inbox, drafts, sent, trash,
    junk, archive).

    MailMaven's dictionary does not expose a true unified inbox/drafts/sent/
    etc. at the application level - those special-mailbox properties
    (``inbox``, ``draft mailbox``, ``sent mailbox``, ``trash mailbox``,
    ``junk mailbox``, ``archive mailbox``) are only defined on the
    ``account`` class (see MailMaven.sdef). So this loops over every
    account and reads them there instead. Each property read is wrapped in
    its own try/on error so one account missing a particular special
    mailbox (e.g. no archive mailbox configured) doesn't blank out the rest.
    """
    body = """
    set _out to ""
    repeat with a in accounts
        set _acctName to my safeStr(name of a)
        set _out to _out & "inbox\\t" & _acctName & "\\t"
        try
            set _out to _out & (my safeStr(name of inbox of a))
        end try
        set _out to _out & "\\n"

        set _out to _out & "drafts\\t" & _acctName & "\\t"
        try
            set _out to _out & (my safeStr(name of draft mailbox of a))
        end try
        set _out to _out & "\\n"

        set _out to _out & "sent\\t" & _acctName & "\\t"
        try
            set _out to _out & (my safeStr(name of sent mailbox of a))
        end try
        set _out to _out & "\\n"

        set _out to _out & "trash\\t" & _acctName & "\\t"
        try
            set _out to _out & (my safeStr(name of trash mailbox of a))
        end try
        set _out to _out & "\\n"

        set _out to _out & "junk\\t" & _acctName & "\\t"
        try
            set _out to _out & (my safeStr(name of junk mailbox of a))
        end try
        set _out to _out & "\\n"

        set _out to _out & "archive\\t" & _acctName & "\\t"
        try
            set _out to _out & (my safeStr(name of archive mailbox of a))
        end try
        set _out to _out & "\\n"
    end repeat
    return _out
    """
    script = safe_str_handler() + "\n" + _tell(body)
    raw = _run_applescript(script)

    result: dict[str, dict[str, str]] = {}
    for line in raw.strip("\n").split("\n"):
        if line.count("\t") != 2:
            continue
        mailbox_type, account_name, mailbox_name = line.split("\t")
        result.setdefault(account_name, {})[mailbox_type] = mailbox_name
    return result


# ── Messages: search (with transaction) ────────────────────────────────────

@mcp.tool()
def search_messages(
    subject: str | None = None,
    from_address: str | None = None,
    to_address: str | None = None,
    content: str | None = None,
    keyword: str | None = None,
    project: str | None = None,
    read_status: bool | None = None,
    flagged: str | None = None,
    importance: str | None = None,
    has_attachments: bool | None = None,
    days_back: int | None = None,          # convenience: last N days through today
    period_start_days_back: int | None = None,  # explicit: window starts N days ago
    period_length_days: int | None = None,      # explicit: window spans this many days from that start
    mailbox_name: str | None = None,
    account_name: str | None = None,
    limit: int = DEFAULT_LIMIT,
    include_content: bool = False,
) -> list[dict]:
    """Search for messages using MailMaven's query/criteria system.

    All filters are combined with AND logic. The query and all criteria
    are created inside a ``with transaction`` block as required by
    MailMaven's AppleScript dictionary.

    Args:
        subject: Filter by subject text (contains).
        from_address: Filter by sender address (contains).
        to_address: Filter by recipient address (contains).
        content: Filter by message body content (contains).
        keyword: Filter by keyword tag.
        project: Filter by project tag.
        read_status: True = read only, False = unread only.
        flagged: Flag colour: "red", "orange", "yellow", "green", "blue",
            "purple", "grey", or "no flag".
        importance: "very low", "low", "normal", "high", or "very high".
        has_attachments: True = only messages with attachments.
        days_back: Only messages received in the last N days.
        mailbox_name: Restrict to a named mailbox.
        account_name: Restrict to a named account.
        limit: Max messages to return (default 50, hard cap 500).
        include_content: Include full body text (default False).
    """
    limit = max(1, min(limit, MAX_LIMIT))

    crit_lines: list[str] = []

    def add_criterion(crit_class: str, props: dict[str, Any]) -> None:
        props_str = _json_record(props)
        crit_lines.append(
            f'make new {crit_class} with properties {props_str} '
            f'at end of criteria of _q'
        )

    if subject:
        add_criterion("subject criterion", {"qualifier": QUALIFIER_INCLUDES, "subject": subject})
    if from_address:
        add_criterion("from criterion", {"qualifier": QUALIFIER_INCLUDES, "address": from_address})
    if to_address:
        add_criterion("to recipient criterion", {"qualifier": QUALIFIER_INCLUDES, "address": to_address})
    if content:
        add_criterion("content criterion", {"qualifier": QUALIFIER_INCLUDES, "phrase": content})
    if keyword:
        add_criterion("keyword criterion", {"qualifier": QUALIFIER_INCLUDES, "keyword": keyword})
    if project:
        add_criterion("project criterion", {"qualifier": QUALIFIER_INCLUDES, "project": project})
    if read_status is not None:
        add_criterion("read status criterion", {"is read": read_status})
    if flagged:
        if flagged not in FLAG_CONSTANTS:
            raise ValueError(f"Invalid flag '{flagged}'. Must be one of: {', '.join(sorted(FLAG_CONSTANTS))}")
        add_criterion("flag criterion", {"qualifier": QUALIFIER_EQUALS_TO, "flag": FLAG_CONSTANTS[flagged]})
    if importance:
        if importance not in IMPORTANCE_CONSTANTS:
            raise ValueError(
                f"Invalid importance '{importance}'. Must be one of: {', '.join(sorted(IMPORTANCE_CONSTANTS))}"
            )
        add_criterion(
            "importance criterion",
            {"qualifier": QUALIFIER_EQUALS_TO, "level": IMPORTANCE_CONSTANTS[importance]},
        )
    if has_attachments:
        add_criterion(
            "attachment count criterion",
            {"qualifier": QUALIFIER_GREATER, "attachment count": 0},
        )
    if period_start_days_back is not None and period_length_days is not None:
        add_criterion("relative message date criterion", {
            "unit": UNIT_DAY,
            "starting at": -period_start_days_back,
            "for duration": period_length_days,
        })
    elif days_back is not None and days_back > 0:
        add_criterion("relative message date criterion", {
            "unit": UNIT_DAY,
            "starting at": -days_back,
            "for duration": days_back,
        })
    if mailbox_name:
        add_criterion("mailbox criterion", {"qualifier": QUALIFIER_EQUALS_TO, "mailbox": mailbox_name})
    if account_name:
        add_criterion("account criterion", {"qualifier": QUALIFIER_EQUALS_TO, "account": account_name})

    if not crit_lines:
        crit_lines.append(
            'make new relative message date criterion with properties '
            '{unit:day, starting at:30, for duration:1} '
            'at end of criteria of _q'
        )

    criteria_block = "\n        ".join(crit_lines)

    # ── with transaction block ──────────────────────────────────────────
    # MailMaven requires "make new query" and criteria creation to be
    # inside a transaction block. Standard AppleScript syntax is:
    #
    #   with transaction
    #       -- create objects
    #   end transaction
    #
    # The block must be inside the tell application block (which _tell
    # provides), so AppleScript knows to target MailMaven for the
    # transaction.
    body = f"""
    with transaction
        set _q to make new query with properties {{name:"mcp_query"}}
        set logic of _q to all criteria
        set sort order of _q to sort by date received
        set sort ascending of _q to false
        {criteria_block}

        set _msgs to messages of _q
        set _count to count of _msgs
        if _count > {limit} then set _count to {limit}

        set _out to ""
        set _headers to "identifier\\tsubject\\toriginalSubject\\talternateSubject\\tdateReceived\\treadStatus\\tjunkStatus\\tflagged\\timportance\\tmailboxName\\taccountName\\tsize\\tattachmentCount\\tkeywords\\tproject\\tnotes\\treviewDate\\tcontentIdentifier\\tmavenIdentifier\\tmessageIdUrl\\tmavenUrl\\tconversationId"
        {"set _headers to _headers & \"\\tcontent\"" if include_content else ""}
        set _out to _headers & return

        repeat with i from 1 to _count
            set m to item i of _msgs
            set _line to ""
            set _line to _line & (my safestr(identifier of m)) & "\\t"
            set _line to _line & (my safeStr(subject of m)) & "\\t"
            set _line to _line & (my safeStr(original subject of m)) & "\\t"
            set _line to _line & (my safeStr(alternate subject of m)) & "\\t"
            set _line to _line & (my safeStr(date received of m)) & "\\t"
            set _line to _line & (my safeStr(read status of m)) & "\\t"
            set _line to _line & (my safeStr(junk status of m)) & "\\t"
            set _line to _line & (my safeStr(flag of m)) & "\\t"
            set _line to _line & (my safeStr(importance of m)) & "\\t"
            set _line to _line & (my safeStr(name of mailbox of m)) & "\\t"
            set _line to _line & (my safeStr(name of account of m)) & "\\t"
            set _line to _line & (my safeStr(size of m)) & "\\t"
            set _line to _line & (my safeStr(attachment count of m)) & "\\t"
            set _line to _line & (my safeStr(keywords of m)) & "\\t"
            set _line to _line & (my safeStr(project of m)) & "\\t"
            set _line to _line & (my safeStr(notes of m)) & "\\t"
            set _line to _line & (my safeStr(review date of m)) & "\\t"
            set _line to _line & (my safeStr(content identifier of m)) & "\\t"
            set _line to _line & (my safeStr(maven identifier of m)) & "\\t"
            set _line to _line & (my safeStr(message id url of m)) & "\\t"
            set _line to _line & (my safeStr(maven url of m)) & "\\t"
            set _line to _line & (my safeStr(conversation id of m))
            {"set _line to _line & \"\\t\" & (my safeStr(content of m))" if include_content else ""}
            set _out to _out & _line & return
        end repeat
    end transaction

    return _out
    """

    script = safe_str_handler() + "\n" + _tell(body)
    raw = _run_applescript(script, timeout=120)
    return _parse_messages_tsv(raw, include_content)


@mcp.tool()
def get_message(identifier: int, include_content: bool = True) -> dict:
    """Get full details of a single message by its integer identifier.

    Args:
        identifier: The integer ``identifier`` value from search results
            (NOT the text mavenIdentifier - resolved via a message criterion
            query, per MailMaven.sdef).
        include_content: Include full body text (default True).
    """
    body = f'''
    with transaction
        {_message_lookup_snippet(identifier)}
        if (count of _msgs) is 0 then
            error "NOT FOUND"
        end if
        set m to item 1 of _msgs

        set _out to "identifier" & "\\t" & (my safeStr(identifier of m)) & return
    set _out to _out & "subject" & "\\t" & (my safeStr(subject of m)) & return
    set _out to _out & "originalSubject" & "\\t" & (my safeStr(original subject of m)) & return
    set _out to _out & "alternateSubject" & "\\t" & (my safeStr(alternate subject of m)) & return
    set _out to _out & "dateReceived" & "\\t" & (my safeStr(date received of m)) & return
    set _out to _out & "readStatus" & "\\t" & (my safeStr(read status of m)) & return
    set _out to _out & "junkStatus" & "\\t" & (my safeStr(junk status of m)) & return
    set _out to _out & "flag" & "\\t" & (my safeStr(flag of m)) & return
    set _out to _out & "importance" & "\\t" & (my safeStr(importance of m)) & return
    set _out to _out & "mailbox" & "\\t" & (my safeStr(name of mailbox of m)) & return
    set _out to _out & "account" & "\\t" & (my safeStr(name of account of m)) & return
    set _out to _out & "size" & "\\t" & (my safeStr(size of m)) & return
    set _out to _out & "attachmentCount" & "\\t" & (my safeStr(attachment count of m)) & return
    set _out to _out & "keywords" & "\\t" & (my safeStr(keywords of m)) & return
    set _out to _out & "project" & "\\t" & (my safeStr(project of m)) & return
    set _out to _out & "notes" & "\\t" & (my safeStr(notes of m)) & return
    set _out to _out & "reviewDate" & "\\t" & (my safeStr(review date of m)) & return
    set _out to _out & "contentIdentifier" & "\\t" & (my safeStr(content identifier of m)) & return
    set _out to _out & "mavenIdentifier" & "\\t" & (my safeStr(maven identifier of m)) & return
    set _out to _out & "messageIdUrl" & "\\t" & (my safeStr(message id url of m)) & return
    set _out to _out & "mavenUrl" & "\\t" & (my safeStr(maven url of m)) & return
    set _out to _out & "conversationId" & "\\t" & (my safeStr(conversation id of m)) & return
    set _out to _out & "modificationDate" & "\\t" & (my safeStr(modification date of m)) & return

    set _senders to ""
    try
        repeat with s in senders of m
            set _senders to _senders & (my safeStr(name of s)) & " <" & (my safeStr(address of s)) & ">, "
        end repeat
    end try
    set _out to _out & "senders" & "\\t" & _senders & return

    set _recips to ""
    try
        repeat with r in recipients of m
            set _recips to _recips & (my safeStr(name of r)) & " <" & (my safeStr(address of r)) & ">, "
        end repeat
    end try
    set _out to _out & "toRecipients" & "\\t" & _recips & return

    set _cc to ""
    try
        repeat with c in cc recipients of m
            set _cc to _cc & (my safeStr(name of c)) & " <" & (my safeStr(address of c)) & ">, "
        end repeat
    end try
    set _out to _out & "ccRecipients" & "\\t" & _cc & return

    set _atts to ""
    try
        repeat with a in message attachments of m
            set _atts to _atts & (my safeStr(name of a)) & " (" & (my safeStr(file size of a) as string) & " bytes, " & (my safeStr(MIME type of a)) & "), "
        end repeat
    end try
    set _out to _out & "attachments" & "\\t" & _atts & return
    '''
    if include_content:
        body += '\n    set _out to _out & "content" & "\\t" & (my safeStr(content of m)) & return\n'

    body += '\n    end transaction\n    return _out\n'
    script = safe_str_handler() + "\n" + _tell(body)
    try:
        raw = _run_applescript(script, timeout=60)
    except RuntimeError as exc:
        if "NOT FOUND" in str(exc):
            raise RuntimeError(f"No message found with identifier: {identifier}") from exc
        raise

    # Each field was appended in AppleScript as `key & "\t" & value & return`,
    # where AppleScript's `return` is a carriage return ("\r"), not "\n" - so
    # "\r" is the real field separator here. Splitting on "\n" instead (as
    # this used to do) breaks on every line break *inside* a field's own
    # value too - which shreds any multi-line value (e.g. a message body
    # with several paragraphs) into extra bogus "lines". Any such fragment
    # that happens to contain a literal tab character then gets misread as
    # its own key/value pair, silently overwriting real fields (e.g.
    # "content") with garbage and truncating them to just their first line.
    result: dict[str, Any] = {}
    for line in raw.strip("\r").split("\r"):
        if "\t" in line:
            key, val = line.split("\t", 1)
            result[key] = val
    return result


@mcp.tool()
def get_recent_messages(
    mailbox_name: str = "inbox",
    limit: int = 20,
    include_content: bool = False,
) -> list[dict]:
    """Get the most recent messages from a mailbox (default: unified inbox).

    Args:
        mailbox_name: Mailbox name (e.g. "inbox", "sent", "trash").
        limit: Number of messages (default 20, max 500).
        include_content: Include body text (default False).
    """
    return search_messages(
        mailbox_name=mailbox_name,
        limit=limit,
        include_content=include_content,
    )


# ── Message actions ────────────────────────────────────────────────────────

@mcp.tool()
def mark_read(identifier: int, read: bool = True) -> str:
    """Set the read status of a message.

    Args:
        identifier: The message's integer identifier.
        read: True to mark as read, False to mark as unread.
    """
    val = "true" if read else "false"
    script = _tell(f'''
    with transaction
        {_message_lookup_snippet(identifier)}
        if (count of _msgs) is 0 then error "Message not found"
        set read status of item 1 of _msgs to {val}
    end transaction
    return "OK"
    ''')
    _run_applescript(script)
    return f"message id {identifier} marked as {'read' if read else 'unread'}."


@mcp.tool()
def set_flag(identifier: int, flag: str) -> str:
    """Set the flag colour of a message.

    Args:
        identifier: The message's integer identifier.
        flag: One of "no flag", "red", "orange", "yellow", "green",
            "blue", "purple", "grey".
    """
    valid = {"no flag", "red", "orange", "yellow", "green", "blue", "purple", "grey"}
    if flag not in valid:
        raise ValueError(f"Invalid flag '{flag}'. Must be one of: {', '.join(sorted(valid))}")

    script = _tell(f'''
    with transaction
        {_message_lookup_snippet(identifier)}
        if (count of _msgs) is 0 then error "Message not found"
        set flag of item 1 of _msgs to {flag}
    end transaction
    return "OK"
    ''')
    _run_applescript(script)
    return f"Flag set to '{flag}' for message id {identifier}."

@mcp.tool()
def set_junk_status(identifier: int, is_junk: bool) -> str:
    """Set whether a message is marked as junk.

    Args:
        identifier: The message's integer identifier.
        is_junk: True to mark the message as junk, False to clear it.
    """
    applescript_bool = "true" if is_junk else "false"

    script = _tell(f'''
    with transaction
        {_message_lookup_snippet(identifier)}
        if (count of _msgs) is 0 then error "Message not found"
        set junk status of item 1 of _msgs to {applescript_bool}
    end transaction
    return "OK"
    ''')
    _run_applescript(script)
    return f"Junk status set to {is_junk} for message {identifier}."
    
@mcp.tool()
def set_importance(identifier: int, importance: str) -> str:
    """Set the importance level of a message.

    Args:
        identifier: The message's integer identifier.
        importance: One of "no importance", "very low", "low", "normal",
            "high", "very high".
    """
    valid = {"no importance", "very low", "low", "normal", "high", "very high"}
    if importance not in valid:
        raise ValueError(f"Invalid importance '{importance}'. Must be one of: {', '.join(sorted(valid))}")

    script = _tell(f'''
    with transaction
        {_message_lookup_snippet(identifier)}
        if (count of _msgs) is 0 then error "Message not found"
        set importance of item 1 of _msgs to {importance}
    end transaction
    return "OK"
    ''')
    _run_applescript(script)
    return f"Importance set to '{importance}' for message id {identifier}."


@mcp.tool()
def set_project(identifier: int, project: str | None = None) -> str:
    """Set or clear the project tag of a message.

    Args:
        identifier: The message's integer identifier.
        project: Project name, or None/empty to clear.
    """
    proj_val = _to_applescript(project) if project else "missing value"
    script = _tell(f'''
    with transaction
        {_message_lookup_snippet(identifier)}
        if (count of _msgs) is 0 then error "Message not found"
        set project of item 1 of _msgs to {proj_val}
    end transaction
    return "OK"
    ''')
    _run_applescript(script)
    return f"Project {'set to ' + repr(project) if project else 'cleared'} for {identifier}."


@mcp.tool()
def set_notes(identifier: int, notes: str | None = None) -> str:
    """Set or clear the notes on a message.

    Args:
        identifier: The message's integer identifier.
        notes: Note text, or None/empty to clear.
    """
    notes_val = _to_applescript(notes) if notes else "missing value"
    script = _tell(f'''
    with transaction
        {_message_lookup_snippet(identifier)}
        if (count of _msgs) is 0 then error "Message not found"
        set notes of item 1 of _msgs to {notes_val}
    end transaction
    return "OK"
    ''')
    _run_applescript(script)
    return f"Notes {'updated' if notes else 'cleared'} for {identifier}."


@mcp.tool()
def set_review_date(identifier: int, review_date: str | None = None) -> str:
    """Set or clear the review (tickler) date for a message.

    Args:
        identifier: The message's integer identifier.
        review_date: ISO date string (e.g. "2025-12-25"), or None to clear.
    """
    if review_date:
        try:
            dt = datetime.fromisoformat(review_date)
        except ValueError:
            raise ValueError(f"Invalid date format: {review_date}. Use ISO format like '2025-12-25'.")
        date_lit = dt.strftime('date "%A, %B %d, %Y"')
    else:
        date_lit = "missing value"

    script = _tell(f'''
    with transaction
        {_message_lookup_snippet(identifier)}
        if (count of _msgs) is 0 then error "Message not found"
        set review date of item 1 of _msgs to {date_lit}
    end transaction
    return "OK"
    ''')
    _run_applescript(script)
    return f"Review date {'set to ' + review_date if review_date else 'cleared'} for {identifier}."


@mcp.tool()
def set_alternate_subject(identifier: int, subject: str | None = None) -> str:
    """Set or clear the alternate subject of a message.

    Args:
        identifier: The message's integer identifier.
        subject: Alternate subject text, or None to clear.
    """
    subj_val = _to_applescript(subject) if subject else "missing value"
    script = _tell(f'''
    with transaction
        {_message_lookup_snippet(identifier)}
        if (count of _msgs) is 0 then error "Message not found"
        set alternate subject of item 1 of _msgs to {subj_val}
    end transaction
    return "OK"
    ''')
    _run_applescript(script)
    return f"Alternate subject {'set' if subject else 'cleared'} for {identifier}."


# ── Keywords ───────────────────────────────────────────────────────────────

@mcp.tool()
def add_keywords(identifier: int, keywords: list[str]) -> str:
    """Add keyword tags to a message.

    Args:
        identifier: The message's integer identifier.
        keywords: List of keyword strings to add.
    """
    if not keywords:
        raise ValueError("At least one keyword is required.")
    kw_list = ", ".join(_to_applescript(k) for k in keywords)
    script = _tell(f'''
    with transaction
        {_message_lookup_snippet(identifier)}
        if (count of _msgs) is 0 then error "Message not found"
        update keywords item 1 of _msgs adding {{{kw_list}}}
    end transaction
    return "OK"
    ''')
    _run_applescript(script)
    return f"Added keywords {keywords} to message id {identifier}."


@mcp.tool()
def remove_keywords(identifier: int, keywords: list[str]) -> str:
    """Remove keyword tags from a message.

    Args:
        identifier: The message's integer identifier.
        keywords: List of keyword strings to remove.
    """
    if not keywords:
        raise ValueError("At least one keyword is required.")
    kw_list = ", ".join(_to_applescript(k) for k in keywords)
    script = _tell(f'''
    with transaction
        {_message_lookup_snippet(identifier)}
        if (count of _msgs) is 0 then error "Message not found"
        update keywords item 1 of _msgs removing {{{kw_list}}}
    end transaction
    return "OK"
    ''')
    _run_applescript(script)
    return f"Removed keywords {keywords} from message id {identifier}."


@mcp.tool()
def clear_all_tags(identifier: int) -> str:
    """Clear all tags from a message.

    Args:
        identifier: The message's integer identifier.
    """
    script = _tell(f'''
    with transaction
        {_message_lookup_snippet(identifier)}
        if (count of _msgs) is 0 then error "Message not found"
        clear tags item 1 of _msgs
    end transaction
    return "OK"
    ''')
    _run_applescript(script)
    return f"All tags cleared for message id {identifier}."


# ── Message moving / deleting ──────────────────────────────────────────────

@mcp.tool()
def move_message(identifier: int, to_mailbox: str, to_account: str | None = None) -> str:
    """Move a message to a different mailbox, optionally in a different account.

    Args:
        identifier: The message's integer identifier.
        to_mailbox: Name of the destination mailbox. One of "Inbox", "Junk"
            (or "Spam"), "Trash", "Archive", "Sent", "Drafts" resolves to
            that account's own special mailbox property (a concrete, real
            mailbox object - not the app-level virtual "unified://" one,
            which doesn't support move as a destination). Any other name
            is resolved as a custom mailbox, scoped to the same account.
        to_account: Name of the destination account. If omitted (None),
            defaults to the source message's own current account - i.e.
            the message stays in its own account and just changes
            mailbox. Pass an explicit account name to move the message
            into a different account's mailbox instead.
    """
    special = {
        "inbox": "inbox",
        "junk": "junk mailbox",
        "spam": "junk mailbox",
        "trash": "trash mailbox",
        "archive": "archive mailbox",
        "sent": "sent mailbox",
        "drafts": "draft mailbox",
        "draft": "draft mailbox",
    }
    canonical_property = special.get(to_mailbox.strip().lower())

    if to_account is not None:
        acct_lookup = f'set _acct to first account whose name is "{to_account}"'
    else:
        acct_lookup = "set _acct to account of item 1 of _msgs"

    if canonical_property is not None:
        mailbox_lookup = f"set _mb to {canonical_property} of _acct"
    else:
        mailbox_lookup = f'set _mb to first mailbox of _acct whose name is "{to_mailbox}"'

    script = _tell(f'''
    with transaction
        {_message_lookup_snippet(identifier)}
        if (count of _msgs) is 0 then error "Message not found"
        {acct_lookup}
        {mailbox_lookup}
        move item 1 of _msgs to _mb
    end transaction
    return "OK"
    ''')
    _run_applescript(script)
    dest = f"'{to_mailbox}'" + (f" in account '{to_account}'" if to_account else "")
    return f"Message {identifier} moved to {dest}."

@mcp.tool()
def delete_message(identifier: int) -> str:
    """Delete a message (moves to trash).

    Args:
        identifier: The message's integer identifier.
    """
    script = _tell(f'''
    with transaction
        {_message_lookup_snippet(identifier)}
        if (count of _msgs) is 0 then error "Message not found"
        delete item 1 of _msgs
    end transaction
    return "OK"
    ''')
    _run_applescript(script)
    return f"message id {identifier} deleted."


# ── Composing & sending (with transaction) ─────────────────────────────────

@mcp.tool()
def compose_message(
    to: list[str],
    subject: str,
    body: str = "",
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    from_account: str | None = None,
    keywords: list[str] | None = None,
    project: str | None = None,
    importance: str | None = None,
    notes: str | None = None,
    show_window: bool = True,
    send: bool = False,
) -> dict:
    """Compose a new email message.

    The composer and any recipients are created inside a ``with transaction``
    block as required by MailMaven's AppleScript dictionary.

    Args:
        to: List of recipient email addresses.
        subject: Subject line.
        body: Plain text body.
        cc: List of CC email addresses (optional).
        bcc: List of BCC email addresses (optional).
        from_account: Account name or address to send from (optional).
        keywords: Keyword tags to apply (optional).
        project: Project tag (optional).
        importance: "very low", "low", "normal", "high", or "very high".
        notes: Internal notes (optional, not sent in message).
        show_window: If True (default), shows the composer in MailMaven.
            Once shown, the message cannot be modified or sent via script.
            Ignored if ``send`` is True, since a message cannot be sent via
            AppleScript once its composer window has been shown.
        send: If True, sends the message immediately via AppleScript instead
            of showing the composer. Default is False. Requires
            ``from_account`` to be set — MailMaven cannot send a message
            without an explicit from address.

    Returns:
        Dict with composerId, whether the message was sent, and a
        confirmation message.
    """
    if send and not from_account:
        raise ValueError(
            "from_account is required when send=True — MailMaven cannot "
            "send a message without an explicit from address."
        )

    from_setup, from_param = _from_account_clause(from_account) if from_account else ("", "")

    props: dict[str, Any] = {"subject": subject, "content": body}
    if keywords:
        props["keywords"] = keywords
    if project:
        props["project"] = project
    if importance:
        valid = {"very low", "low", "normal", "high", "very high"}
        if importance not in valid:
            raise ValueError(f"Invalid importance. Must be one of: {', '.join(sorted(valid))}")
        props["importance"] = importance
    if notes:
        props["notes"] = notes

    props_record = _json_record(props)

    recipient_lines: list[str] = []
    for t in to:
        recipient_lines.append(
            f'make new recipient with properties {{address:{_to_applescript(t)}}} at end of recipients of _comp'
        )
    for c in (cc or []):
        recipient_lines.append(
            f'make new cc recipient with properties {{address:{_to_applescript(c)}}} at end of cc recipients of _comp'
        )
    for b in (bcc or []):
        recipient_lines.append(
            f'make new bcc recipient with properties {{address:{_to_applescript(b)}}} at end of bcc recipients of _comp'
        )
    recipients_block = "\n        ".join(recipient_lines)

    # ── with transaction block for object creation ──────────────────────
    # Sending and showing are mutually exclusive: once a composer window is
    # shown, MailMaven no longer allows it to be sent via AppleScript. If
    # `send` is set, it takes priority and `show_window` is ignored.
    if send:
        action_line = '\n        set _sent to send _comp'
        result_expr = '_id & "|" & (_sent as string)'
    else:
        action_line = '\n        show composer _comp' if show_window else ''
        result_expr = '_id'

    body_script = f'''
    with transaction{from_setup}
        set _comp to compose new message{from_param} with properties {props_record}
        {recipients_block}
        set _id to identifier of _comp{action_line}
    end transaction
    '''
    body_script += f'\n    return {result_expr}'

    script = _tell(body_script)
    raw = _run_applescript(script)

    if send:
        composer_id, _, sent_flag = raw.partition("|")
        sent = sent_flag.strip().lower() == "true"
        status = "sent" if sent else "created but send reported failure"
    else:
        composer_id = raw
        sent = False
        status = "shown" if show_window else "created (use send_message)"

    return {
        "composerId": composer_id,
        "sent": sent,
        "message": f"Composed message to {to}. Composer {status}.",
    }


@mcp.tool()
def forward_message(
    identifier: int,
    to: list[str],
    body: str = "",
    from_account: str | None = None,
    as_attachment: bool = False,
    show_window: bool = True,
    send: bool = False,
) -> dict:
    """Forward an existing message.

    Args:
        identifier: The message's integer identifier.
        to: List of recipient email addresses to forward to.
        body: Additional body text (optional).
        from_account: Account to send from (optional).
        as_attachment: Forward as attachment instead of inline (default False).
        show_window: Show composer in MailMaven (default True). Ignored if
            ``send`` is True, since a message cannot be sent via AppleScript
            once its composer window has been shown.
        send: If True, sends the forwarded message immediately via
            AppleScript instead of showing the composer. Default is False.
            Requires ``from_account`` to be set — MailMaven cannot send a
            message without an explicit from address.

    Returns:
        Dict with composerId, whether the message was sent, and a
        confirmation message.
    """
    if send and not from_account:
        raise ValueError(
            "from_account is required when send=True — MailMaven cannot "
            "send a message without an explicit from address."
        )

    to_list = ", ".join(_to_applescript(t) for t in to)
    to_param = f' to {{{to_list}}}'
    from_setup, from_param = _from_account_clause(from_account) if from_account else ("", "")
    attach_param = " as attachment true" if as_attachment else ""

    # Lookup and composer creation both need to happen inside the same
    # transaction: the query used for lookup is transient and invalid once
    # its transaction ends, and the resolved message reference then feeds
    # directly into "forward". The composer reference itself is also only
    # reliably valid while the transaction is open, so any further work on
    # _comp (setting content, fetching its id, sending or showing the
    # window) happens inside the block too.
    content_line = f'\n        set content of _comp to {_to_applescript(body)}' if body else ''

    # Sending and showing are mutually exclusive: once a composer window is
    # shown, MailMaven no longer allows it to be sent via AppleScript. If
    # `send` is set, it takes priority and `show_window` is ignored.
    if send:
        action_line = '\n        set _sent to send _comp'
        result_expr = '_id & "|" & (_sent as string)'
    else:
        action_line = '\n        show composer _comp' if show_window else ''
        result_expr = '_id'

    body_script = f'''
    with transaction{from_setup}
        {_message_lookup_snippet(identifier)}
        if (count of _msgs) is 0 then error "Message not found"
        set _comp to forward item 1 of _msgs{attach_param}{from_param}{to_param}{content_line}
        set _id to identifier of _comp{action_line}
    end transaction
    '''
    body_script += f'\n    return {result_expr}'

    script = _tell(body_script)
    raw = _run_applescript(script)

    if send:
        composer_id, _, sent_flag = raw.partition("|")
        sent = sent_flag.strip().lower() == "true"
        status = "sent" if sent else "created but send reported failure"
    else:
        composer_id = raw
        sent = False
        status = "shown" if show_window else "created (use send_message)"

    return {
        "composerId": composer_id,
        "sent": sent,
        "message": f"Forwarded message id {identifier} to {to}. Composer {status}.",
    }


# ── Signatures & templates ─────────────────────────────────────────────────

@mcp.tool()
def list_signatures() -> list[dict]:
    """List all available email signatures in MailMaven."""
    script = _tell("""
    set _out to ""
    repeat with s in signatures
        set _out to _out & (identifier of s) & "\\t" & (my safeStr(name of s)) & "\\n"
    end repeat
    return _out
    """)
    raw = _run_applescript(safe_str_handler() + "\n" + script)
    sigs: list[dict] = []
    for line in raw.strip().split("\n"):
        if "\t" in line:
            sid, sname = line.split("\t", 1)
            sigs.append({"id": sid, "name": sname})
    return sigs


@mcp.tool()
def list_templates() -> list[dict]:
    """List all message templates (new, reply, forward) in MailMaven."""
    script = _tell("""
    set _out to ""
    repeat with t in new message templates
        set _out to _out & (identifier of t) & "\\t" & (my safeStr(name of t)) & "\\tnew\\n"
    end repeat
    repeat with t in reply templates
        set _out to _out & (identifier of t) & "\\t" & (my safeStr(name of t)) & "\\treply\\n"
    end repeat
    repeat with t in forward templates
        set _out to _out & (identifier of t) & "\\t" & (my safeStr(name of t)) & "\\tforward\\n"
    end repeat
    return _out
    """)
    raw = _run_applescript(safe_str_handler() + "\n" + script)
    templates: list[dict] = []
    for line in raw.strip().split("\n"):
        if "\t" in line:
            parts = line.split("\t")
            templates.append({
                "id": parts[0],
                "name": parts[1] if len(parts) > 1 else "",
                "type": parts[2] if len(parts) > 2 else "",
            })
    return templates


# ── Viewer (selected messages) ─────────────────────────────────────────────

@mcp.tool()
def get_selected_messages(include_content: bool = False) -> list[dict]:
    """Get the currently selected message(s) in the active MailMaven viewer.

    Args:
        include_content: Include full body text (default False).

    Returns:
        List of message dicts for the selected messages.
    """
    body = """
    set _out to ""
    set _headers to "identifier\\tsubject\\toriginalSubject\\talternateSubject\\tdateReceived\\treadStatus\\tjunkStatus\\tflagged\\timportance\\tmailboxName\\taccountName\\tsize\\tattachmentCount\\tkeywords\\tproject\\tnotes\\treviewDate\\tcontentIdentifier\\tmavenIdentifier\\tmessageIdUrl\\tmavenUrl\\tconversationId"
    """
    if include_content:
        body += '\n    set _headers to _headers & "\\tcontent"'
    body += '\n    set _out to _headers & return\n'

    body += '''
    set _v to viewer 1
    set _sels to selected messages of _v
    repeat with m in _sels
        set _line to ""
        set _line to _line & (my safestr(identifier of m)) & "\\t"
        set _line to _line & (my safeStr(subject of m)) & "\\t"
        set _line to _line & (my safeStr(original subject of m)) & "\\t"
        set _line to _line & (my safeStr(alternate subject of m)) & "\\t"
        set _line to _line & (my safeStr(date received of m)) & "\\t"
        set _line to _line & (my safeStr(read status of m)) & "\\t"
        set _line to _line & (my safeStr(junk status of m)) & "\\t"
        set _line to _line & (my safeStr(flag of m)) & "\\t"
        set _line to _line & (my safeStr(importance of m)) & "\\t"
        set _line to _line & (my safeStr(name of mailbox of m)) & "\\t"
        set _line to _line & (my safeStr(name of account of m)) & "\\t"
        set _line to _line & (my safeStr(size of m)) & "\\t"
        set _line to _line & (my safeStr(attachment count of m)) & "\\t"
        set _line to _line & (my safeStr(keywords of m)) & "\\t"
        set _line to _line & (my safeStr(project of m)) & "\\t"
        set _line to _line & (my safeStr(notes of m)) & "\\t"
        set _line to _line & (my safeStr(review date of m)) & "\\t"
        set _line to _line & (my safeStr(content identifier of m)) & "\\t"
        set _line to _line & (my safeStr(maven identifier of m)) & "\\t"
        set _line to _line & (my safeStr(message id url of m)) & "\\t"
        set _line to _line & (my safeStr(maven url of m)) & "\\t"
        set _line to _line & (my safeStr(conversation id of m))
        '''
    if include_content:
        body += '\n        set _line to _line & "\\t" & (my safeStr(content of m))'
    body += '\n        set _out to _out & _line & return\n'
    body += '    end repeat\n    return _out'

    script = safe_str_handler() + "\n" + _tell(body)
    raw = _run_applescript(script)
    return _parse_messages_tsv(raw, include_content)
    
# ── Archiving to EagleFiler ─────────────────────────────────────────────────

EAGLEFILER_APP_NAME = "EagleFiler"


@mcp.tool()
def export_and_archive_to_eaglefiler(
    identifiers: list[int],
    tag_names: list[str] | None = None,
    move_to_mailbox: str | None = None,
    eaglefiler_library_name: str | None = None,
    delete_temp_files: bool = True,
) -> dict:
    """Export messages as .eml files and import them into EagleFiler.

    Resolves all messages in a single query using MailMaven's message
    criterion ``message identifiers`` property (a list of integer Maven
    identifiers - see MailMaven.sdef), saves each as an .eml file to a
    temporary folder, then tells EagleFiler to import those files into a
    library (optionally tagging them). Optionally moves the original
    messages to a MailMaven mailbox afterward (e.g. "Archive").

    Args:
        identifiers: Integer identifiers of the messages to archive, as
            returned by search_messages / get_recent_messages / get_message
            (NOT the text mavenIdentifier).
        tag_names: Tags to apply to the imported EagleFiler records, e.g.
            ["Stripe", "Payments"]. Omit for no tags.
        move_to_mailbox: Name of a MailMaven mailbox to move the originals to
            after a successful import (e.g. "Archive"). Omit to leave the
            originals where they are.
        eaglefiler_library_name: Name of the EagleFiler library document to
            import into. Omit to use the frontmost open library.
        delete_temp_files: Whether EagleFiler should delete the exported
            .eml files from the temp folder after importing (default True).

    Returns:
        A dict with "count" (number of messages archived) and
        "record_names" (the names EagleFiler gave the imported records).
    """
    if not identifiers:
        raise ValueError("identifiers must contain at least one message identifier")

    delete_afterwards_as = _to_applescript(bool(delete_temp_files))

    if eaglefiler_library_name:
        library_ref = f"document {_to_applescript(eaglefiler_library_name)}"
    else:
        library_ref = "document 1"

    tag_clause = ""
    if tag_names:
        tag_clause = f" tag names {_to_applescript(list(tag_names))}"

    # Moving happens *after* a successful EagleFiler import (so a failed
    # import never strands messages out of place), reusing the same _msgs
    # resolved during the export transaction rather than looking them up
    # again - the message references remain valid once obtained, even
    # though the query object that produced them does not (see
    # _message_lookup_snippet's docstring).
    move_block = ""
    if move_to_mailbox:
        move_block = f'''
tell application "{APP_NAME}"
    set _mb to first mailbox whose name is {_to_applescript(move_to_mailbox)}
    repeat with m in _msgs
        move m to _mb
    end repeat
end tell
'''

    script = f"""
set tempFolderPOSIX to (POSIX path of (path to home folder)) & "Library/Caches/mavenmcp_eaglefiler_export/"
do shell script "mkdir -p " & quoted form of tempFolderPOSIX

set exportedFilePathsPOSIX to {{}}
tell application "{APP_NAME}"
    with transaction
        {_message_lookup_snippet(list(identifiers))}
        if (count of _msgs) is 0 then error "No messages found for the given identifiers"
        repeat with m in _msgs
       		set msgId to identifier of m
            set safeName to (my sanitizeFilename(subject of m)) & "-" & (msgId as text)
            set filePOSIX to tempFolderPOSIX & safeName & ".eml"
            save m in filePOSIX as eml format with replacing
            set end of exportedFilePathsPOSIX to filePOSIX
        end repeat
    end transaction
end tell

set exportedFilePaths to {{}}
repeat with p in exportedFilePathsPOSIX
    set end of exportedFilePaths to (POSIX file p)
end repeat

tell application "{EAGLEFILER_APP_NAME}"
    set targetLibrary to {library_ref}
    set importedRecords to import targetLibrary files exportedFilePaths{tag_clause} deleting afterwards {delete_afterwards_as}
end tell

set recordNames to {{}}
repeat with r in importedRecords
    set end of recordNames to (name of r)
end repeat
set AppleScript's text item delimiters to "|||"
set recordNamesText to recordNames as text
set AppleScript's text item delimiters to ""
{move_block}
return ((count of importedRecords) as text) & "::" & recordNamesText

on sanitizeFilename(theName)
    set badChars to {{"/", ":", (ASCII character 92), (ASCII character 34), "*", "?", "<", ">", "|"}}
    set newName to theName
    repeat with c in badChars
        set AppleScript's text item delimiters to c
        set theParts to text items of newName
        set AppleScript's text item delimiters to "-"
        set newName to theParts as text
    end repeat
    set AppleScript's text item delimiters to ""
    if (length of newName) > 80 then set newName to text 1 thru 80 of newName
    return newName
end sanitizeFilename
"""

    raw = _run_applescript(script, timeout=180)
    count_str, _, names_str = raw.partition("::")
    record_names = names_str.split("|||") if names_str else []
    return {
        "count": int(count_str) if count_str.strip().isdigit() else len(identifiers),
        "record_names": record_names,
    }


# ── Entry point ────────────────────────────────────────────────────────────

def main() -> None:
    """Run the MCP server with stdio transport."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    if "--http" in sys.argv:
        mcp.run(transport="streamable-http", host="127.0.0.1", port=8000)
    else:
        main()