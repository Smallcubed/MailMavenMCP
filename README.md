# MailMavenMCP

A [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server that lets Claude read, search, and manage your email in [MailMaven](https://smallcubed.com/) — the macOS email client from Smallcubed.

The server drives MailMaven through AppleScript, so it works with your existing accounts and mailboxes exactly as they're set up in the app. No extra IMAP credentials, API keys, or cloud sync required.

> **macOS only.** MailMaven is a macOS app, and this server controls it via AppleScript (`osascript`). It will not run on Windows or Linux.

---

## What it lets Claude do

Once connected, Claude gains numerous tools for working with MailMaven:

**Accounts & mailboxes**
- `get_app_info`, `check_new_mail`
- `list_accounts`, `list_mailboxes`, `get_unified_mailboxes`

**Reading & searching mail**
- `search_messages` — filter by subject, sender, recipient, body content, keyword, project, read/flag/importance status, attachments, date range, mailbox, or account
- `get_message`, `get_recent_messages`, `get_selected_messages`

**Organizing messages**
- `mark_read`, `set_flag`, `set_importance`, `set_junk_status`
- `set_project`, `set_notes`, `set_review_date`, `set_alternate_subject`
- `add_keywords`, `remove_keywords`, `clear_all_tags`
- `move_message`, `delete_message`

**Composing**
- `compose_message`, `forward_message`
- `list_signatures`, `list_templates`

**Archiving**
- `export_and_archive_to_eaglefiler`

Ask Claude things like *"find unread emails from my accountant this month"*, *"flag anything from my boss as high importance"*, or *"draft a reply using my work signature"* and it will call these tools directly against your running copy of MailMaven.

---

## Requirements

- macOS with **MailMaven** installed and configured with at least one account
- **Python 3.10+**
- The `mcp` Python package (provides `mcp.server.fastmcp.FastMCP`)
- Claude Desktop or Claude Code

---

## 1. Install

Clone the repo and install the one dependency:

```bash
git clone https://github.com/Smallcubed/MailMavenMCP.git
cd MailMavenMCP
python3 -m venv .venv
source .venv/bin/activate
pip install mcp
```

Note the full path to `MavenMCP.py` — you'll need it in the config step below. You can get it with:

```bash
pwd  # from inside the MailMavenMCP folder
```

## 2. Connect it to Claude

### Claude Desktop

If you are using Claude Desktop, open **Settings → Developer → Edit Config** in Claude Desktop (or open the file directly):

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`

Add a `mailmaven` entry under `mcpServers`, using the **full paths** to your virtual environment's Python and to `MavenMCP.py`:  (if there is not a `mcpServers` key, you will need to add it)

```json
...
  "mcpServers": {
    "mailmaven": {
      "command": "/absolute/path/to/MailMavenMCP/.venv/bin/python3",
      "args": ["/absolute/path/to/MailMavenMCP/MavenMCP.py"]
    }
  }
...
```

Ensure the config file is well formed json and save the file and fully restart Claude Desktop (quit from the menu bar, don't just close the window).

### Claude Code

If you are using Claude code, enter the following command in the Terminal:

```bash
claude mcp add mailmaven -- /absolute/path/to/MailMavenMCP/.venv/bin/python3 /absolute/path/to/MailMavenMCP/MavenMCP.py
```

Run `claude mcp list` afterward to confirm it shows as connected.

## 3. Grant automation permissions

The server calls MailMaven through AppleScript, so macOS needs to let the process that launches it (Claude Desktop, Terminal, or Claude Code) control MailMaven.

1. Open **MailMaven** and leave it running — the server can't reach it otherwise.
2. Open **System Settings → Privacy & Security → Automation**.
3. The first time Claude tries to use a MailMaven tool, macOS will prompt you to allow Claude (or Terminal) to control "MailMaven." Click **Allow**.
4. If you don't see a prompt, or accidentally denied it, find the app in the Automation list and manually check the **MailMaven** box next to it.

## 4. Verify it worked

Make sure MailMaven is open, then ask Claude something simple like:

> "List my MailMaven accounts."

or 

> "Summarize the first selected message of the front MailMaven viewer"

If it responds with information from Maven, the connection is working. If a MailMaven tool appears in Claude's tool list but calls fail, check the troubleshooting section below.

---

## MCP configuration

By default, the MailMaven MCP does not allow sending from the Assistant.  Composed emails should be displayed before manually sending.  To allow AI Assistants to send without this manual confirmation, edit the Assistant's config file to set the "ENABLE_SENDING" environment variable to "true".

```json
...
  "mcpServers": {
    "mailmaven": {
      "command": "/absolute/path/to/MailMavenMCP/.venv/bin/python3",
      "args": ["/absolute/path/to/MailMavenMCP/MavenMCP.py"],
      "env": {"ENABLE_SENDING": "true"}
    }
  }
...
```


---

## Troubleshooting

**"AppleScript error: ... not allowed to send Apple events"** (or errno `-1743`)
macOS is blocking the automation permission. Revisit step 3 above — check System Settings → Privacy & Security → Automation, and make sure the app that's launching the server (Claude Desktop or Terminal) has permission to control MailMaven.

**Tools time out or return nothing**
MailMaven must be running for AppleScript to reach it. Open the app and try again.

**"command not found" or the server doesn't appear in Claude**
Double-check that both paths in your config are absolute (not `~` or relative paths) and that they point at the Python interpreter *inside* the virtual environment you created (`.venv/bin/python3`), not your system Python — that's what has the `mcp` package installed.

**Changes to `MavenMCP.py` aren't taking effect**
Restart Claude Desktop, or in Claude Code run `claude mcp remove mailmaven` and re-add it — MCP servers are only reloaded on startup/reconnect.

---

## Privacy & safety notes

- This server only talks to the MailMaven app on your own Mac — it doesn't send your mail anywhere else.
- `delete_message` moves messages to Trash; nothing is permanently deleted through these tools.
- Because Claude can read message content and send/compose mail through this server, treat it like any other tool with access to your inbox: review what Claude is about to send before you approve it.

## License

MIT — see [LICENSE](LICENSE).
