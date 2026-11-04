# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=2.0.0"]
# ///
"""Read-only iMessage access over MCP, plus a send tool via AppleScript.

Reads a snapshot of ~/Library/Messages/chat.db so the live WAL database is
never locked. Requires Full Disk Access for the host app.
"""

import glob
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

from mcp.server.mcpserver import MCPServer

SRC = os.path.expanduser("~/Library/Messages/chat.db")
SNAP = os.path.join(tempfile.gettempdir(), "imessage-mcp-snapshot.db")
ADDRESS_BOOK = os.path.expanduser(
    "~/Library/Application Support/AddressBook/**/AddressBook-v22.abcddb"
)
APPLE_EPOCH = 978307200  # 2001-01-01 in Unix seconds

# Apple stores message dates as nanoseconds since 2001 (seconds on <=10.12).
LOCAL_TIME = f"datetime(m.date/1000000000 + {APPLE_EPOCH}, 'unixepoch', 'localtime')"

server = MCPServer("imessage")


def db() -> sqlite3.Connection:
    """Refresh the snapshot if the live db moved on, then open it."""
    if not os.path.exists(SRC):
        raise RuntimeError(f"{SRC} not found")
    stale = (
        not os.path.exists(SNAP)
        or os.path.getmtime(SRC) > os.path.getmtime(SNAP)
    )
    if stale:
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(SRC + suffix):
                shutil.copy2(SRC + suffix, SNAP + suffix)
    conn = sqlite3.connect(SNAP)
    conn.row_factory = sqlite3.Row
    return conn


_contacts: dict[str, str] = {}
_contacts_stamp = 0.0


def contacts() -> dict[str, str]:
    """Map phone/email keys to contact names, cached until a store changes.

    Contacts live in several Core Data stores, one per account, so every
    AddressBook-v22.abcddb under the AddressBook directory is read and merged.
    Each is copied first for the same reason chat.db is: never hold a lock on
    a live database.
    """
    global _contacts, _contacts_stamp
    stores = glob.glob(ADDRESS_BOOK, recursive=True)
    newest = max((os.path.getmtime(p) for p in stores), default=0.0)
    if _contacts and newest <= _contacts_stamp:
        return _contacts

    found: dict[str, str] = {}
    for store in stores:
        copy = os.path.join(tempfile.gettempdir(), "imessage-mcp-contacts.db")
        try:
            shutil.copyfile(store, copy)
            conn = sqlite3.connect(copy)
            # A record may have only an organization, so fall back to it.
            name = (
                "COALESCE(NULLIF(TRIM(COALESCE(r.ZFIRSTNAME,'') || ' ' || "
                "COALESCE(r.ZLASTNAME,'')),''), r.ZORGANIZATION)"
            )
            for table, column in (
                ("ZABCDPHONENUMBER", "ZFULLNUMBER"),
                ("ZABCDEMAILADDRESS", "ZADDRESS"),
            ):
                try:
                    rows = conn.execute(
                        f"SELECT t.{column}, {name} FROM {table} t "
                        f"JOIN ZABCDRECORD r ON r.Z_PK = t.ZOWNER"
                    )
                except sqlite3.OperationalError:
                    continue  # store without that table
                for value, person in rows:
                    if value and person:
                        found.setdefault(normalize(value), person.strip())
            conn.close()
        except (OSError, sqlite3.DatabaseError):
            continue  # a store we cannot read should not break message reads

    _contacts, _contacts_stamp = found, newest
    return _contacts


def who(handle: str) -> str:
    """A contact name when one is known, else the raw handle."""
    if not handle:
        return "unknown"
    return contacts().get(normalize(handle), handle)


def escape_like(value: str) -> str:
    """Escape LIKE wildcards so a literal % or _ in a query matches itself."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def body(row: sqlite3.Row) -> str:
    """Message text, falling back to the attributedBody blob on newer macOS.

    attributedBody is an NSArchiver "streamtyped" stream. The message text is a
    length-prefixed UTF-8 run right after the NSString class name, where a
    prefix byte under 0x80 is the length itself and 0x81/0x82 introduce a
    2- or 4-byte little-endian length.
    """
    if row["text"]:
        return row["text"]
    blob = row["attributedBody"]
    if not blob:
        return ""
    anchor = blob.find(b"NSString")
    if anchor == -1:
        return ""
    plus = blob.find(b"+", anchor, anchor + 32)
    if plus == -1:
        return ""
    pos = plus + 1
    length = blob[pos]
    pos += 1
    if length == 0x81:
        length = int.from_bytes(blob[pos : pos + 2], "little")
        pos += 2
    elif length == 0x82:
        length = int.from_bytes(blob[pos : pos + 4], "little")
        pos += 4
    elif length >= 0x80:
        return ""
    return blob[pos : pos + length].decode("utf-8", errors="replace").strip()


def render(rows) -> str:
    """Format rows newest-last for reading."""
    out = []
    for r in rows:
        sender = "me" if r["is_from_me"] else who(r["handle"])
        text = body(r)
        if not text:
            text = "[attachment]" if r["has_attachment"] else "[no text]"
        out.append(f"[{r['ts']}] {sender}: {text}")
    return "\n".join(out) or "(no messages)"


@server.tool()
def list_chats(limit: int = 30) -> str:
    """List conversations by most recent activity, with their identifiers."""
    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT c.chat_identifier, c.display_name,
                   COUNT(m.ROWID) AS n,
                   MAX({LOCAL_TIME}) AS last
            FROM chat c
            JOIN chat_message_join j ON j.chat_id = c.ROWID
            JOIN message m ON m.ROWID = j.message_id
            GROUP BY c.ROWID
            ORDER BY MAX(m.date) DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        # Group chats keep their own name; one-to-one chats get the contact.
        label = r["display_name"] or who(r["chat_identifier"])
        ident = r["chat_identifier"]
        head = f"{label} [{ident}]" if label != ident else ident
        out.append(f"{head} — {r['n']} msgs, last {r['last']}")
    return "\n".join(out) or "(no chats)"


@server.tool()
def get_conversation(chat: str, limit: int = 50, before: str = "") -> str:
    """Read the most recent messages in one conversation.

    chat: a chat_identifier or display name from list_chats (substring match).
    before: optional 'YYYY-MM-DD' to page backwards past older messages.
    """
    sql = f"""
        SELECT m.text, m.attributedBody, m.is_from_me,
               m.cache_has_attachments AS has_attachment,
               h.id AS handle, {LOCAL_TIME} AS ts
        FROM message m
        JOIN chat_message_join j ON j.message_id = m.ROWID
        JOIN chat c ON c.ROWID = j.chat_id
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        WHERE (c.chat_identifier LIKE ? ESCAPE '\\'
               OR IFNULL(c.display_name,'') LIKE ? ESCAPE '\\')
    """
    args = [f"%{escape_like(chat)}%", f"%{escape_like(chat)}%"]
    if before:
        sql += f" AND {LOCAL_TIME} < ?"
        args.append(before)
    sql += " ORDER BY m.date DESC LIMIT ?"
    args.append(limit)
    with db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return render(reversed(rows))


@server.tool()
def search_messages(query: str, limit: int = 40, handle: str = "") -> str:
    """Search all messages for a substring, case-insensitive, newest first.

    handle: optional phone number or email to restrict the search to.

    Messages whose text lives only in attributedBody cannot be matched in SQL,
    because the blob's NUL bytes defeat LIKE, so those are decoded and filtered
    in Python. Both halves are merged and re-sorted before truncating.
    """
    needle = query.lower()
    cols = f"""m.text, m.attributedBody, m.is_from_me, m.date AS raw,
               m.cache_has_attachments AS has_attachment,
               h.id AS handle, {LOCAL_TIME} AS ts"""
    hits = []
    with db() as conn:
        sql = f"""SELECT {cols} FROM message m
                  LEFT JOIN handle h ON h.ROWID = m.handle_id
                  WHERE m.text LIKE ? ESCAPE '\\'"""
        args = [f"%{escape_like(query)}%"]
        if handle:
            sql += " AND h.id LIKE ? ESCAPE '\\'"
            args.append(f"%{escape_like(handle)}%")
        sql += " ORDER BY m.date DESC LIMIT ?"
        args.append(limit)
        hits.extend(conn.execute(sql, args).fetchall())

        blob_sql = f"""SELECT {cols} FROM message m
                       LEFT JOIN handle h ON h.ROWID = m.handle_id
                       WHERE (m.text IS NULL OR m.text = '')
                         AND m.attributedBody IS NOT NULL"""
        blob_args = []
        if handle:
            blob_sql += " AND h.id LIKE ? ESCAPE '\\'"
            blob_args.append(f"%{escape_like(handle)}%")
        blob_sql += " ORDER BY m.date DESC"
        found = 0
        for row in conn.execute(blob_sql, blob_args):
            if needle in body(row).lower():
                hits.append(row)
                found += 1
                if found >= limit:
                    break

    hits.sort(key=lambda r: r["raw"], reverse=True)
    return render(hits[:limit])


@server.tool()
def find_contact(query: str, limit: int = 20) -> str:
    """Look up a contact by name, returning the handles to message them on.

    Use this to turn "Nellie" into a number before calling send_message;
    send_message takes a handle, not a name.
    """
    needle = query.strip().lower()
    if not needle:
        return "(empty query)"

    hits: dict[str, list[str]] = {}
    for key, person in contacts().items():
        if needle in person.lower():
            hits.setdefault(person, []).append(key)
    if not hits:
        return f"(no contact matching {query!r})"

    # Prefer the handles this person actually has conversations on.
    with db() as conn:
        rows = conn.execute(
            """
            SELECT h.id AS handle, COUNT(m.ROWID) AS n
            FROM handle h LEFT JOIN message m ON m.handle_id = h.ROWID
            GROUP BY h.id
            """
        ).fetchall()
    traffic = {normalize(r["handle"]): (r["handle"], r["n"]) for r in rows}

    out = []
    for person in sorted(hits)[:limit]:
        parts = []
        for key in sorted(hits[person]):
            handle, count = traffic.get(key, (key, 0))
            parts.append(f"{handle} ({count} msgs)" if count else handle)
        out.append(f"{person}: " + ", ".join(parts))
    return "\n".join(out)


def sent_verdict(row: sqlite3.Row) -> str:
    """What Messages recorded for one outgoing message."""
    if row["error"]:
        if row["error"] == 22:
            return (
                f"failed, error 22, not reachable on {row['service']}, "
                "usually a non-Apple device"
            )
        return f"failed, error {row['error']}"
    if row["is_read"]:
        return "read"
    if row["is_delivered"]:
        return "delivered"
    if row["is_sent"]:
        return "sent, no delivery receipt"
    return "not sent"


@server.tool()
def check_sent(to: str = "", limit: int = 10) -> str:
    """Confirm what became of messages you sent, newest first.

    Messages accepting a text and the text reaching the other end are separate
    events, and can be hours apart on a bad connection. Call this whenever
    send_message returned "queued", and any time you need to state that
    something arrived rather than assume it.

    Pass a handle or a group name to narrow it, or nothing for everything sent
    recently. RCS and SMS rarely produce a read receipt, so "delivered" is the
    strongest verdict you will see for most non-Apple contacts.
    """
    where, params = "", []
    if to:
        if looks_like_handle(to):
            key = normalize(to)
            with db() as conn:
                ids = [
                    r["ROWID"]
                    for r in conn.execute("SELECT ROWID, id FROM handle")
                    if normalize(r["id"]) == key
                ]
            if not ids:
                return f"no handle in the history matches {to!r}"
            where = f"AND m.handle_id IN ({','.join('?' * len(ids))})"
            params = ids
        else:
            guids = group_candidates(to)
            if not guids:
                return f"no group chat matching {to!r}"
            where = f"AND c.guid IN ({','.join('?' * len(guids))})"
            params = guids

    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT {LOCAL_TIME} AS ts, m.service AS service, m.error AS error,
                   m.is_sent AS is_sent, m.is_delivered AS is_delivered,
                   m.is_read AS is_read, m.text AS text,
                   m.attributedBody AS attributedBody,
                   m.has_attachment AS has_attachment,
                   h.id AS handle, c.display_name AS display_name,
                   c.chat_identifier AS chat_identifier
            FROM message m
            LEFT JOIN handle h ON h.ROWID = m.handle_id
            LEFT JOIN chat_message_join j ON j.message_id = m.ROWID
            LEFT JOIN chat c ON c.ROWID = j.chat_id
            WHERE m.is_from_me = 1 {where}
            ORDER BY m.date DESC LIMIT ?
            """,
            (*params, max(1, min(limit, 100))),
        ).fetchall()

    out = []
    for r in rows:
        label = who(r["handle"]) if r["handle"] else (
            r["display_name"] or r["chat_identifier"] or "group"
        )
        # An attachment-only message carries U+FFFC, the object replacement
        # character, where its text would be.
        text = body(r).replace("\ufffc", "").strip()
        if not text:
            text = "[attachment]" if r["has_attachment"] else "[no text]"
        out.append(f"[{r['ts']}] {label} over {r['service']}: {sent_verdict(r)}"
                   f"\n    {text}")
    return "\n".join(out) or "(nothing sent from this Mac yet)"


def automation_grant() -> str:
    """Whether this binary may drive Messages, read out of the TCC database."""
    tcc = os.path.expanduser(
        "~/Library/Application Support/com.apple.TCC/TCC.db"
    )
    try:
        conn = sqlite3.connect(f"file:{tcc}?mode=ro", uri=True)
        rows = conn.execute(
            """
            SELECT client, auth_value FROM access
            WHERE service = 'kTCCServiceAppleEvents'
              AND indirect_object_identifier = 'com.apple.MobileSMS'
            """
        ).fetchall()
        conn.close()
    except sqlite3.Error as err:
        return f"unknown, cannot read TCC.db ({err})"
    for client, auth in rows:
        if client == sys.executable:
            return "granted" if auth else "denied, reset it in Privacy & Security"
    if not rows:
        return "not requested yet; no client may drive Messages"
    return (
        f"not held by this interpreter; {len(rows)} other client(s) have it. "
        "Sending will fail with -1712 until this binary asks"
    )


@server.tool()
def status() -> str:
    """Whether this server can actually do its job, and what it runs as.

    The two macOS grants fail independently: a server that reads every message
    may still be unable to send one, and a clean MCP handshake proves neither
    because it never touches the database. Check here before concluding that a
    contact or a conversation does not exist.
    """
    lines = [f"interpreter: {sys.executable}"]

    try:
        with db() as conn:
            counts = conn.execute(
                """
                SELECT (SELECT COUNT(*) FROM message) AS messages,
                       (SELECT COUNT(*) FROM chat) AS chats,
                       (SELECT COUNT(*) FROM handle) AS handles
                """
            ).fetchone()
            newest = conn.execute(
                f"SELECT {LOCAL_TIME} AS ts FROM message m"
                " ORDER BY m.date DESC LIMIT 1"
            ).fetchone()
        lines.append(
            f"chat.db: readable, {counts['messages']:,} messages across "
            f"{counts['chats']:,} chats and {counts['handles']:,} handles"
        )
        lines.append(f"newest message: {newest['ts'] if newest else 'none'}")
        age = time.time() - os.path.getmtime(SNAP)
        lines.append(f"snapshot: {age:.0f}s old, refreshed when the source moves")
    except Exception as err:
        lines.append(f"chat.db: UNREADABLE ({err})")
        lines.append(
            "Full Disk Access is granted per executable and does not inherit. "
            "Grant it to the interpreter named above."
        )
        return "\n".join(lines)

    names = contacts()
    with db() as conn:
        resolved = sum(
            1
            for r in conn.execute("SELECT id FROM handle")
            if normalize(r["id"]) in names
        )
    lines.append(
        f"contacts: {len(names):,} names, resolving {resolved:,} of "
        f"{counts['handles']:,} handles"
    )

    allowed = os.environ.get("IMESSAGE_ALLOW_SEND", "1") != "0"
    lines.append(f"send tool: {'registered' if allowed else 'not registered'}")
    if allowed:
        lines.append(f"Messages automation: {automation_grant()}")
        running = subprocess.run(
            ["/usr/bin/pgrep", "-x", "Messages"], capture_output=True
        )
        lines.append(
            "Messages app: running"
            if running.returncode == 0
            else "Messages app: not running, it will be launched by a send"
        )
    return "\n".join(lines)


def normalize(handle: str) -> str:
    """Last ten digits for a phone number, lowercase for an email."""
    if "@" in handle:
        return handle.strip().lower()
    digits = re.sub(r"\D", "", handle)
    return digits[-10:] if len(digits) >= 10 else digits


def preferred_service(to: str) -> str:
    """The Messages service this contact was last actually reached on, or "".

    Ranked by the last message that genuinely went through, not the newest
    message. A failed send still lands a row, so an iMessage attempt that died
    with error 22 is newer than the SMS that should have carried it; ranking by
    recency alone would pick the dead service every time and never recover.
    """
    key = normalize(to)
    with db() as conn:
        ids = [
            r["ROWID"]
            for r in conn.execute("SELECT ROWID, id FROM handle")
            if normalize(r["id"]) == key
        ]
        if not ids:
            return ""
        row = conn.execute(
            f"""
            SELECT service FROM message
            WHERE handle_id IN ({",".join("?" * len(ids))})
              AND error = 0 AND (is_from_me = 0 OR is_sent = 1)
            ORDER BY date DESC LIMIT 1
            """,
            ids,
        ).fetchone()
    return (row["service"] or "") if row else ""


def group_candidates(name: str) -> list[str]:
    """Guids of group chats matching name, last genuine activity first.

    One group is usually several chat rows: renaming it forks the thread, and a
    mixed-device group carries an iMessage row and an RCS row under the same
    identifier. Only one of them is the one Messages will talk to, so every
    candidate comes back ranked and the caller picks against the live list.
    """
    like = f"%{escape_like(name)}%"
    with db() as conn:
        rows = conn.execute(
            """
            SELECT c.guid AS guid
            FROM chat c
            JOIN chat_message_join j ON j.chat_id = c.ROWID
            JOIN message m ON m.ROWID = j.message_id
            WHERE (c.chat_identifier LIKE ? ESCAPE '\\'
                   OR IFNULL(c.display_name,'') LIKE ? ESCAPE '\\')
              AND (SELECT COUNT(*) FROM chat_handle_join h
                   WHERE h.chat_id = c.ROWID) > 1
              AND m.error = 0 AND (m.is_from_me = 0 OR m.is_sent = 1)
            GROUP BY c.ROWID
            ORDER BY MAX(m.date) DESC
            """,
            (like, like),
        ).fetchall()
    return [r["guid"] for r in rows]


def live_chats() -> list[str]:
    """Chat guids Messages currently holds, or [] when it will not say.

    Messages resolves a slice of chat.db and nothing else, 296 of 3515 chats on
    the machine this was built on, and a guid outside that slice fails with
    -1728. Asking up front beats finding out by sending: a send that failed for
    some other reason cannot be safely retried against the next candidate.
    """
    result = subprocess.run(
        ["osascript", "-e", 'tell application "Messages" to get id of chats'],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    return [part.strip() for part in result.stdout.split(",") if part.strip()]


def resolve_group(name: str) -> tuple[str, str]:
    """The guid to address a group on, or ("", reason it cannot be reached)."""
    candidates = group_candidates(name)
    if not candidates:
        return "", f"no group chat matching {name!r}"
    live = set(live_chats())
    if not live:
        # Messages would not answer, so let the send attempt report why.
        return candidates[0], ""
    for guid in candidates:
        if guid in live:
            return guid, ""
    return "", (
        f"{name!r} is in the message history but not among the {len(live)} "
        "conversations Messages currently holds, so AppleScript cannot address "
        "it (-1728). Open the conversation in Messages and try again"
    )


HANDLE = re.compile(r"[\d\s()+.\-]+")


def looks_like_handle(to: str) -> bool:
    """True for a phone number or email, false for a group name or chat id."""
    return "@" in to or bool(HANDLE.fullmatch(to.strip()))


def delivery_result(to: str, since: float) -> str:
    """Read back what Messages recorded for the send, or "" if nothing yet."""
    key = normalize(to)
    cutoff = since - APPLE_EPOCH
    with db() as conn:
        rows = conn.execute(
            """
            SELECT h.id AS handle, m.service AS service, m.error AS error,
                   m.is_sent AS is_sent
            FROM message m JOIN handle h ON h.ROWID = m.handle_id
            WHERE m.is_from_me = 1 AND m.date / 1000000000 > ?
            ORDER BY m.date DESC LIMIT 20
            """,
            (cutoff,),
        ).fetchall()
    for row in rows:
        if normalize(row["handle"]) != key:
            continue
        if row["error"]:
            hint = ""
            if row["error"] == 22:
                hint = (
                    " - that error means the recipient is not reachable on "
                    f"{row['service']}, usually a non-Apple device"
                )
            return f"not sent: Messages reported error {row['error']}{hint}"
        if row["is_sent"]:
            return f"sent to {to} over {row['service']}"
    return ""


def chat_delivery(guid: str, since: float, label: str) -> str:
    """The same read-back for a group send, or "" if nothing yet.

    An outgoing group message carries handle_id = 0, so it cannot be found by
    recipient the way a one-to-one send is. The chat row is the only link back.
    """
    cutoff = since - APPLE_EPOCH
    with db() as conn:
        row = conn.execute(
            """
            SELECT m.service AS service, m.error AS error, m.is_sent AS is_sent
            FROM message m
            JOIN chat_message_join j ON j.message_id = m.ROWID
            JOIN chat c ON c.ROWID = j.chat_id
            WHERE c.guid = ? AND m.is_from_me = 1
              AND m.date / 1000000000 > ?
            ORDER BY m.date DESC LIMIT 1
            """,
            (guid, cutoff),
        ).fetchone()
    if not row:
        return ""
    if row["error"]:
        return f"not sent: Messages reported error {row['error']}"
    if row["is_sent"]:
        return f"sent to {label} over {row['service']}"
    return ""


# Messages exposes one account per service type. The SMS account is the
# carrier path and Messages negotiates RCS over it on its own, so everything
# that is not iMessage routes through SMS.
SEND_ACCOUNT = {
    "iMessage": "iMessage",
    "SMS": "SMS",
    "RCS": "SMS",
    "SatelliteSMS": "SMS",
}

SEND = """
on run argv
    set target to item 1 of argv
    set body to item 2 of argv
    with timeout of 20 seconds
        tell application "Messages"
            set svc to 1st account whose service type = %s
            send body to participant target of svc
        end tell
    end timeout
end run
"""

# A group has no single participant to address, so it goes to the chat itself.
# The guid names the service, which is why this one picks no account. The class
# is "chat": "text chat id" parses as the text of a chat and fails with -1728,
# which reads exactly like a stale chat and cost this project a wrong answer.
SEND_CHAT = """
on run argv
    set target to item 1 of argv
    set body to item 2 of argv
    with timeout of 20 seconds
        tell application "Messages"
            send body to chat id target
        end tell
    end timeout
end run
"""


def send_message(to: str, text: str) -> str:
    """Send a message to a person or a group chat. This really sends it.

    to: a phone number or email for one person, or a group's name or
    chat_identifier as list_chats prints it.

    A person gets the account matching the service they were last reached on,
    so an Android number goes out as a text instead of dying as a failed
    iMessage. A group is addressed by its chat guid, which carries its own
    service; Messages only resolves the few hundred conversations it currently
    holds, so a long-dormant group cannot be addressed at all and says so
    rather than guessing.

    A -1712 timeout means the running binary has no Automation access to
    Messages. The event is parked, not dropped, and fires on approval, so never
    retry a send that timed out.
    """
    guid = ""
    if looks_like_handle(to):
        script = SEND % SEND_ACCOUNT.get(preferred_service(to), "iMessage")
        target = to
    else:
        guid, reason = resolve_group(to)
        if not guid:
            return f"not sent: {reason}"
        script, target = SEND_CHAT, guid

    started = time.time()
    result = subprocess.run(
        ["osascript", "-e", script, target, text], capture_output=True, text=True
    )
    if result.returncode != 0:
        err = result.stderr.strip()
        if "-1712" in err or "timed out" in err:
            return (
                "unknown: no Automation access to Messages (-1712). The event "
                "is parked and will fire if the grant is approved, so do not "
                "retry. Check the conversation before sending again."
            )
        if "-1728" in err:
            return (
                "not sent: Messages will not address that conversation (-1728), "
                "which means it is outside the set it currently holds. Open it "
                "in Messages and try again."
            )
        return f"not sent: {err}"

    # The AppleScript returning says Messages accepted the text, not that it
    # left the machine: the two error 22 failures both reported success here.
    # RCS takes longer to land its row than iMessage; the first proven RCS send
    # needed more than the six seconds this used to wait, and reported "queued"
    # for a message that had in fact been delivered.
    for _ in range(50):
        verdict = (
            chat_delivery(guid, started, to) if guid else delivery_result(to, started)
        )
        if verdict:
            return verdict
        time.sleep(0.5)
    return f"queued for {to}, no delivery record yet - check the conversation"


# Registered last and conditionally, so a remote client can be handed the read
# tools only, by setting IMESSAGE_ALLOW_SEND=0.
if os.environ.get("IMESSAGE_ALLOW_SEND", "1") != "0":
    send_message = server.tool()(send_message)


if __name__ == "__main__":
    server.run()
