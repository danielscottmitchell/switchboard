# imessage-mcp

MCP access to this Mac's iMessage, SMS and RCS history, with contact names and
the ability to send. Runs as two LaunchAgents and is reachable from anywhere
through a Cloudflare tunnel.

## Connecting a client

```json
{
  "type": "http",
  "url": "https://relay.example.com/mcp",
  "headers": { "Authorization": "Bearer <token>" }
}
```

```bash
security find-generic-password -s imessage-mcp-token -w   # the token
```

Anything without that header gets a 401 before it touches the database.

## Tools

| Tool | What it does |
|---|---|
| `list_chats(limit=30)` | Conversations by recent activity, named, with the handle in brackets |
| `get_conversation(chat, limit=50, before="")` | Recent messages in one conversation |
| `search_messages(query, limit=40, handle="")` | Case-insensitive substring search, newest first |
| `find_contact(query, limit=20)` | Name to handles, with message counts |
| `send_message(to, text)` | Sends. Registered only when `IMESSAGE_ALLOW_SEND` is not `0` |

## How an agent should use this

**Resolve a name before sending.** `send_message` takes a handle, never a name.

```
find_contact("jordan")
  -> Jordan Vance: +15550123456 (4821 msgs), jordan@example.com (112 msgs)
send_message("+15550123456", "running late")
```

Message counts are there so you can pick between several handles for one
person. Prefer the one with real traffic.

**Do not pick a service.** There is no SMS or RCS argument and there should not
be. The server reads which conversation the contact actually replies on and
routes there. Asking for a service by hand is how messages get lost.

**Read the return value. It is not decorative.**

| Return | Meaning |
|---|---|
| `sent to +1555… over iMessage` | Confirmed in the database, service named |
| `sent to +1555… over RCS` | Same, delivered over RCS |
| `not sent: Messages reported error 22 …` | Accepted then failed, usually a non-Apple device |
| `not sent: <error>` | AppleScript refused outright |
| `unknown: no Automation access … (-1712)` | Consent missing. **Never retry.** See below |
| `queued for +1555…, no delivery record yet` | No verdict within 25s. Check before resending |

**Never retry a send that did not clearly fail.** On `unknown` or `queued`, read
the conversation with `get_conversation` first. A `-1712` event is parked, not
dropped, and fires the moment consent is approved. This already sent one contact
three identical texts.

## Services, and why RCS matters here

Contacts fall into three groups and they are not interchangeable.

| Service | Who | Sending |
|---|---|---|
| iMessage | Apple devices | Verified working |
| RCS | Android contacts on this Mac | Verified working, via the SMS account |
| SMS | Older threads | Mostly superseded by RCS; many SMS chats are dead |

Targeting iMessage for an Android contact does not fail loudly. Messages accepts
the text, cannot deliver it, and records `error=22`. Two real replies were lost
that way on 2026-11-02 while the tool reported success.

Routing picks the **account** matching the service the contact was last
genuinely reached on. That is the last message that actually went through, not
the newest message. The distinction is load-bearing: a failed send still lands a
row, so those two dead iMessage attempts are newer than the RCS threads they
should have used. Ranking by recency alone would pick the corpse and fail
forever.

```
+15550111111  ->  SMS account       (last good message was RCS)
+15550122222  ->  SMS account       (last good message was RCS)
+15550133333  ->  iMessage account
5550133333    ->  iMessage account  (bare numbers normalize)
nobody@x.com  ->  iMessage account  (no history, best guess)
```

Everything that is not iMessage routes through the SMS account. The RCS-typed
account on this Mac reports itself disabled while RCS traffic rides the carrier
path, and Messages negotiates the RCS upgrade itself. The read-back names the
service the message actually left on, so the return value is the source of
truth, not this table.

Addressing a chat by its GUID would be more precise and does not work.
Messages only resolves the conversations it currently holds: 231 of the 3383 in
the database, as of 2026-11-03. Anything older fails with `-1728`, "Can't get
chat id". That bug silently broke sends to any contact whose thread had aged
out, which is most of them.

RCS delivery is proven end to end as of 2026-11-03: a send routed through the
SMS account landed as `service=RCS`, `error=0`, `is_delivered=1`. Messages does
the upgrade itself, so the SMS account is the correct route and the return value
names RCS.

RCS lands its database row more slowly than iMessage. The read-back waits 25
seconds; the first proven RCS send took over six. A `queued` return means the
wait ran out, not that the send failed, so read the conversation instead of
sending again.

## Contact names

843 contacts merged from all four AddressBook stores (iCloud, Google, local),
cached until one changes on disk. Keyed on the last ten digits of a phone number
or a lowercased email, so `5550133333` and `+1 (555) 013-3333` are the same
person.

Resolves 672 of 2383 handles, which is 28% of handles but **97% of message
volume**. The rest are shortcodes and one-off senders. An unknown handle shows
as the raw number, never a wrong name.

## Shape

```
client -> Cloudflare edge -> cloudflared -> 127.0.0.1:8765 -> chat.db snapshot
```

- `server.py` builds the MCP server and the tools. Usable directly over stdio.
- `http_server.py` adds streamable HTTP, reads the token from Keychain, binds
  loopback only. TLS terminates at the tunnel.
- `com.dsm.imessage-mcp.plist` runs the server. `KeepAlive`, `RunAtLoad`.
- `com.dsm.imessage-tunnel.plist` runs `cloudflared tunnel run imessage-mcp`.
- The tunnel's credentials and ingress live in `~/.cloudflared/`.

Reads go against a copy of `chat.db` plus its `-wal` and `-shm`, refreshed when
the source mtime moves, so the live WAL database is never locked.

About 30% of messages keep their text in an `attributedBody` NSArchiver blob
rather than the `text` column. SQLite cannot `LIKE` into it, so
`search_messages` runs a SQL pass and a Python blob scan and merges them.
Dropping the blob scan loses a third to two thirds of hits depending on the term.

## Permissions

Two macOS grants, neither of which inherits from anything.

**Full Disk Access** on `<repo>/.venv/bin/python3`,
for `chat.db` and the AddressBook stores. Granted per executable, and launchd
spawns the agent, so the host app's grant does nothing for it. The venv was
built with `--copies` so the interpreter is a real file at a stable path; a
Homebrew path is version pinned and `brew upgrade` would silently revoke it.
`ps` shows the Homebrew `Python.app` it re-execs through, and the grant still
applies.

**Automation** for Messages, needed only for sending. Keyed to the same binary,
so the host app's grant does not cover it, and the Automation pane cannot be
pre-filled because it only lists clients that have already asked. Trigger the
prompt without sending anything:

```bash
osascript -e 'tell application "Messages" to get count of chats'
```

run from a one-shot launchd job using `.venv/bin/python3`, so that binary is the
responsible process. Then confirm the row exists, rather than trusting that the
AppleScript succeeded:

```bash
sqlite3 ~/Library/Application\ Support/com.apple.TCC/TCC.db \
  "select client, auth_value from access
   where service='kTCCServiceAppleEvents'
     and indirect_object_identifier='com.apple.MobileSMS'"
```

`initialize` returns 200 without touching any database, so a healthy handshake
proves nothing about Full Disk Access. Call `list_chats`.

## Operating it

```bash
# after a code change
launchctl kickstart -k gui/$(id -u)/com.dsm.imessage-mcp

# after a plist change: kickstart keeps the cached job definition
launchctl bootout   gui/$(id -u)/com.dsm.imessage-mcp
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.dsm.imessage-mcp.plist

# rotate the token, then reload as above
security add-generic-password -U -s imessage-mcp-token -a "$USER" \
  -w "$(python3 -c 'import secrets; print(secrets.token_urlsafe(40))')"
```

Logs: `/tmp/imessage-mcp.log`, `/tmp/imessage-tunnel.log`. The startup line says
`send enabled` or `send disabled`, the quickest check that an env change landed.

Set `IMESSAGE_ALLOW_SEND=0` and reload for a read-only endpoint. `send_message`
is then never registered, so it is absent from `tools/list` rather than present
and refusing.

| Variable | Default | Meaning |
|---|---|---|
| `IMESSAGE_TOKEN` | Keychain `imessage-mcp-token` | Bearer token. Refuses to start without one, or under 32 chars |
| `IMESSAGE_PUBLIC_HOST` | unset | Public hostname, for Host header validation |
| `IMESSAGE_PORT` | `8765` | Loopback port |
| `IMESSAGE_ALLOW_SEND` | `1` | `0` hides the send tool |

## Gotchas worth keeping

- AppleScript returning success means Messages accepted the text, not that it
  left the machine. Always read the outcome back out of `chat.db`.
- Redirect rules are terminating. This zone had two rules 301ing the hostname;
  excluding the first just handed the request to the second, which looks exactly
  like the edit failing. The `location` header names which one fired.
- `cloudflared tunnel route dns` silently appends the authorized zone, producing
  junk like `relay-x.example.com.other-zone.info`, and has no delete.
- `cloudflared tunnel login` refuses to run while a `cert.pem` exists. A running
  tunnel does not need it; credentials live in the per-tunnel JSON.
- `ESCAPE '\'` in a Python string compiles to empty and SQLite rejects it. The
  source needs `ESCAPE '\\'`.
- `when` is a reserved word in SQLite.

## Exposure

The endpoint is public. The bearer token is the only control in front of the
entire message history and the ability to send as this account, and every client
holding it is another place it can leak from. Narrowing the hostname to known
client IP ranges with a Cloudflare WAF rule is not done yet.
