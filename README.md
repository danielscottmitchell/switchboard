# Switchboard

An MCP server that patches an AI agent onto your Mac's Messages history:
iMessage, SMS and RCS, with contact names resolved, and optionally the ability
to send.

The name is the hard part of the problem. An operator's whole job is putting a
call on the right line, and that is what sending here turns out to be: pick the
wrong Messages account for a contact and the text is accepted, never delivered,
and reported as a success.

Runs over stdio for a local client, or over HTTP behind a bearer token if you
want to reach it from somewhere else. No daemon of its own, no database of its
own, no network calls except the ones your client makes.

Read the [Security](#security) section before you expose this to anything.

## Requirements

- macOS. The whole thing is built on `~/Library/Messages/chat.db` and
  AppleScript, so there is no Linux or Windows story.
- Python 3.11 or newer. Dependencies are declared inline with PEP 723, so
  [uv](https://docs.astral.sh/uv/) can run it with nothing installed.
- Messages signed in to the account whose history you want.
- Full Disk Access for whichever interpreter runs the server. This is the step
  people get stuck on, so it has [its own section](#full-disk-access).

## Quick start

```bash
git clone https://github.com/danielscottmitchell/switchboard
cd switchboard
uv run server.py        # speaks MCP over stdio, exits when the client closes
```

Register it with a client. For Claude Code:

```bash
claude mcp add switchboard -- uv run --directory /path/to/switchboard server.py
```

Or by hand, in any client that takes a stdio command:

```json
{
  "mcpServers": {
    "switchboard": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/switchboard", "server.py"]
    }
  }
}
```

Sending is on by default. Set `IMESSAGE_ALLOW_SEND=0` in the environment for a
read-only server; the send tool is then never registered, so it is absent from
`tools/list` instead of present and refusing.

## Tools

| Tool | What it does |
|---|---|
| `list_chats(limit=30)` | Conversations by recent activity, named, with the handle in brackets |
| `get_conversation(chat, limit=50, before="")` | Recent messages in one conversation |
| `search_messages(query, limit=40, handle="")` | Case-insensitive substring search, newest first |
| `find_contact(query, limit=20)` | Name to handles, with message counts |
| `send_message(to, text)` | Sends. Registered only when `IMESSAGE_ALLOW_SEND` is not `0` |

## Full Disk Access

`chat.db` is protected by macOS privacy controls. The grant is **per
executable** and does not inherit, so granting it to your terminal or to Claude
Desktop does nothing for a server that something else launches.

Grant it to the interpreter that actually runs the code. If you use a project
venv, build it so the interpreter is a real file at a path you own:

```bash
python3 -m venv --copies .venv
.venv/bin/python3 -m pip install 'mcp>=2.0.0' 'uvicorn>=0.30'
```

Then System Settings, Privacy & Security, Full Disk Access, and add
`<repo>/.venv/bin/python3`. Use Command-Shift-G in the file picker to type a
path beginning with a dot.

Two things that bite:

- Granting a Homebrew interpreter works until `brew upgrade` moves it, which
  silently revokes the grant. A `--copies` venv in your own directory survives.
- `ps` will show the server running through Homebrew's `Python.app`, because
  that is what the venv re-execs into. The grant still applies.

A successful MCP handshake proves nothing here, because `initialize` never
touches the database. Call `list_chats` to confirm.

## Sending, and the Automation grant

Sending drives Messages through AppleScript, which needs a second, separate
grant: Automation, for Messages, keyed to the same binary. The Automation pane
cannot be pre-filled, because it only lists clients that have already asked.

Trigger the prompt without sending anything:

```bash
osascript -e 'tell application "Messages" to get count of chats'
```

Run that from whatever process will later do the sending, so that binary is the
responsible one. Then check the row landed, instead of trusting that the
AppleScript returned cleanly:

```bash
sqlite3 ~/Library/Application\ Support/com.apple.TCC/TCC.db \
  "select client, auth_value from access
   where service='kTCCServiceAppleEvents'
     and indirect_object_identifier='com.apple.MobileSMS'"
```

**A send that times out with `-1712` has not failed.** The Apple Event is parked
awaiting consent, and every parked event fires the moment you approve the
prompt. Retrying a `-1712` send is how one contact here received the same text
three times.

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
| `unknown: no Automation access … (-1712)` | Consent missing. **Never retry** |
| `queued for +1555…, no delivery record yet` | No verdict within 25s. Check before resending |

On `unknown` or `queued`, read the conversation with `get_conversation` before
doing anything else.

## Services, and why RCS matters

Contacts fall into three groups and they are not interchangeable.

| Service | Who | Sending |
|---|---|---|
| iMessage | Apple devices | Works |
| RCS | Android contacts, on macOS 15 and newer | Works, routed through the SMS account |
| SMS | Older threads | Largely superseded by RCS; many SMS chats are dead |

Targeting iMessage for an Android contact does not fail loudly. Messages
accepts the text, cannot deliver it, and records `error=22`. Two real replies
were lost that way here before this was fixed, while the tool reported success.

Routing picks the Messages **account** matching the service the contact was last
genuinely reached on. "Genuinely" means the last message with `error = 0` that
was either inbound or actually sent, not simply the newest row. That distinction
is load-bearing: a failed send still lands a row, so a dead iMessage attempt is
newer than the thread that works, and ranking by recency alone picks the corpse
and fails forever.

```
+15550111111  ->  SMS account       (last good message was RCS)
+15550133333  ->  iMessage account
5550133333    ->  iMessage account  (bare numbers normalize)
nobody@x.com  ->  iMessage account  (no history, best guess)
```

Everything that is not iMessage routes through the SMS account. The RCS-typed
account reports itself disabled while RCS traffic rides the carrier path, and
Messages negotiates the RCS upgrade on its own. Confirmed by sending: the
message went out through the SMS account and landed as `service=RCS`,
`error=0`, `is_delivered=1`.

RCS writes its database row several seconds slower than iMessage, so the
read-back waits 25 seconds. A `queued` return means the wait ran out, not that
the send failed.

**Addressing a chat by its GUID looks right and does not work.** AppleScript
only resolves the conversations Messages currently holds, which was 231 of 3383
on the machine this was built on. Anything older fails with `-1728`, "Can't get
chat id", which looks like a missing contact rather than a stale chat list.

## Contact names

Names come from every AddressBook store on the machine, iCloud and Google and
local, merged, cached until one changes on disk. Keyed on the last ten digits of
a phone number or a lowercased email, so `5550133333` and `+1 (555) 013-3333`
are the same person.

On the machine this was built on, that resolves 672 of 2383 handles. 28% of
handles, but 97% of message volume; the rest are shortcodes and one-off senders.
An unresolved handle shows as the raw number, never a guessed name.

## Remote access

`http_server.py` serves the same tools over streamable HTTP, binds loopback
only, and rejects anything without a bearer token before MCP sees the request.
Put a tunnel in front of it for remote access.

```bash
export IMESSAGE_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(40))')"
export IMESSAGE_PUBLIC_HOST=relay.example.com
uv run http_server.py
```

On macOS it will also read the token from Keychain, so launchd can exec the
interpreter directly without a shell wrapper:

```bash
security add-generic-password -U -s imessage-mcp-token -a "$USER" -w "$TOKEN"
```

The Keychain service name predates the project being named, and is left alone so
an existing install keeps working. Pass `IMESSAGE_TOKEN` directly if you would
rather not use Keychain at all.

| Variable | Default | Meaning |
|---|---|---|
| `IMESSAGE_TOKEN` | Keychain `imessage-mcp-token` | Bearer token. Refuses to start without one, or under 32 chars |
| `IMESSAGE_PUBLIC_HOST` | unset | Public hostname, for Host header validation |
| `IMESSAGE_PORT` | `8765` | Loopback port |
| `IMESSAGE_ALLOW_SEND` | `1` | `0` hides the send tool |

Client config:

```json
{
  "type": "http",
  "url": "https://relay.example.com/mcp",
  "headers": { "Authorization": "Bearer <token>" }
}
```

The included plists are templates for running the server and a Cloudflare tunnel
as LaunchAgents. Replace `YOUR_USER` and the hostname, then substitute your own
label below if you install them under a different one.

```bash
# after a code change
launchctl kickstart -k gui/$(id -u)/com.switchboard.mcp

# after a plist change: kickstart keeps the cached job definition
launchctl bootout   gui/$(id -u)/com.switchboard.mcp
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.switchboard.mcp.plist
```

The server identifies itself to MCP as `imessage`, which is cosmetic; the name
you register it under in your client is what prefixes the tools.

## How it works

```
client -> MCP -> snapshot of chat.db
```

Reads go against a copy of `chat.db` plus its `-wal` and `-shm`, refreshed when
the source mtime moves, so the live WAL database is never locked and Messages
never blocks.

About 30% of messages keep their text in an `attributedBody` NSArchiver blob
instead of the `text` column. SQLite cannot `LIKE` into it, so
`search_messages` runs a SQL pass and a Python blob scan and merges the results.
Dropping the blob scan loses between a third and two thirds of hits, depending
on the term.

Sending reads its own outcome back out of `chat.db`, because AppleScript
returning cleanly only means Messages accepted the text, not that it left the
machine.

## Security

This is the whole of your message history and, with sending on, the ability to
speak as you. Decide deliberately.

- **The token is the only control.** Anyone holding it reads every message you
  have sent or received and can text anyone in your address book. There is one
  token, no expiry, and no per-client revocation: rotating it cuts off every
  client at once.
- **Prompt injection is the sharpest edge.** The agent reads text other people
  wrote and holds a tool that sends. Anyone who can text you can put
  instructions in front of your agent. Run with `IMESSAGE_ALLOW_SEND=0` unless
  you specifically need to send.
- **A tunnel provider sees plaintext.** TLS terminates at the edge, so whoever
  runs your tunnel can read everything passing through it.
- **There is no audit trail.** The server logs session IDs, not tool calls. After
  a leak you cannot tell what was read.
- **A hostname is not a secret, and also not nothing.** A Cloudflare wildcard
  certificate keeps a tunnel subdomain out of Certificate Transparency, so it is
  not enumerable that way. Do not treat that as a control. There is no rate
  limiting and no IP allowlist unless you add them at the edge.
- **Other people's privacy is in here too.** Your history is also their history,
  and they did not agree to any of this.

Local stdio, read-only, is the safe default. Everything beyond that is a
deliberate trade.

## Gotchas worth keeping

- AppleScript returning success means Messages accepted the text, not that it
  left the machine. Always read the outcome back out of `chat.db`.
- Cloudflare redirect rules are terminating. One zone here had two rules 301ing
  the tunnel hostname; excluding the first just handed the request to the
  second, which looks exactly like the edit failing. The `location` header names
  which one fired.
- `cloudflared` defaults to QUIC. Behind a VPN with a low MTU, UDP breaks and the
  tunnel dies every few minutes. `--protocol http2` fixes it.
- `cloudflared tunnel route dns` silently appends the authorized zone, producing
  junk like `relay.example.com.other-zone.info`, and has no delete.
- `cloudflared tunnel login` refuses to run while a `cert.pem` exists. A running
  tunnel does not need it; credentials live in the per-tunnel JSON.
- `ESCAPE '\'` in a Python string compiles to empty and SQLite rejects it. The
  source needs `ESCAPE '\\'`.
- `when` is a reserved word in SQLite.
- Apple stores message dates as nanoseconds since 2001-01-01, and as seconds on
  macOS 10.12 and earlier.

## License

MIT. See [LICENSE](LICENSE).
