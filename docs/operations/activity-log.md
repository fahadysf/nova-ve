# Activity log

The activity log is the record of **who did what, from where, on which lab**. Open it from the main menu under **System → Logs → Activity log**. It is visible to administrators only.

## What is recorded

Each entry names the user, the source address, the time, and the object involved.

| Area | Events |
|---|---|
| Authentication | Sign-in, **failed sign-in**, sign-out |
| Users | Account created, updated (with the fields that changed), deleted |
| Labs | Created, deleted, moved, settings changed, and the lab-wide *stop all* / *wipe all* actions |
| Nodes | Created, updated, started, stopped, wiped, deleted; a changed interface MAC address |
| Networks | Created, updated, deleted |
| Links | Connected, updated, disconnected |
| Folders | Created, renamed, deleted |
| Consoles | Opened, closed (with how long it was open) |
| Recordings | Recording started, stopped, deleted |
| Images | Uploaded, deleted, Docker image marked / unmarked / pulled, Dynamips image calibrated |
| Templates | Edited (with the fields that changed) |
| System | Settings changed, IOL license replaced |

### Consoles

Opening a console in the browser is one entry. Closing it is another, with how long it was open: the lab page reports every console that ends, whether you close its tab, close the whole console workspace, remove its node, move to another lab, reload it, or leave the page.

A console handed to an external or native client (the `telnet://`, `vnc://` and `rdp://` launches) is recorded as opened at the moment it is handed over. That client connects to the node directly, so the server never sees it end: there is an open and no close.

### A few rules

- **An entry means it happened.** Actions are recorded after they succeed. A refused or failed action (a start that errored, a delete that was denied) leaves no entry — except for failed sign-ins, which are recorded because repeated failures are the signal worth seeing.
- **Saving without changing anything is not an entry.** Re-opening a node's edit dialog and clicking save, or stopping a lab in which nothing is running, writes nothing.
- **Passwords and licenses are never recorded.** A password change appears only as "password changed"; the IOL license only as replaced.
- **The trail outlives the account.** Deleting a user does not remove their earlier entries.
- **A session that times out is not a sign-out.** Only an explicit logout is recorded.

### What is left out on purpose

- **Layout.** Node positions, port positions and link styling do not affect what a lab does.
- **Automatic housekeeping.** The bridge reconciliation the canvas runs when a lab opens, and re-computing an image's checksum, change nothing a user did.
- **Reading.** Opening or downloading a recording is not recorded.

### Limits worth knowing

- A console closed by a crashed browser or a dropped connection may be reported late or not at all; its open then has no close.
- How long a console was open is known only while the backend has been running since it was opened, and is left out otherwise.

## Searching

- **Search** matches the user, the object, the lab, the source address and the details as free text. It runs once you stop typing; press `Enter` to run it immediately.
- **Area**, **Action** and **User** narrow the result to one kind of event or one account.
- The two **date-time** fields bound the result in your local time zone.
- **Clear** resets every filter.

The list is newest first, a hundred entries per page, with **← Newer** / **Older →** to move through it. **Auto (10s)** reloads the current page every ten seconds.

## Export

**Export CSV** downloads every entry matching the current filters — not only the page on screen — as a UTF-8 file with one row per entry and a readable description column. An export is capped at the newest 20,000 matching entries.

## Last sign-in per account

**Management → Users** shows a **Last login** column with the time of each account's most recent successful sign-in and, when known, the address it came from. An account that has never signed in says `never`. Accounts that existed before this feature was added show `never` until their next sign-in.

## Source addresses

nova-ve's backend listens on the host behind a reverse proxy, so the connection it sees is always the proxy's. The activity log resolves the real client from the address the proxy forwards. Only the local proxy is trusted to supply that address: a client that connects directly cannot choose what the log records about it.

## Where it lives

Entries are stored in the `audit_log` table of the Postgres database, so they are included in a database backup (see [Backups](backups.md)). The table is not pruned automatically; on a busy instance it grows steadily. Each entry is small, so this takes a long time to matter, but it is worth knowing when planning long-term storage.

## API

`GET /api/system/logs/activity` — administrator only.

| Parameter | Meaning |
|---|---|
| `q` | Free-text search |
| `category` | `auth`, `user`, `lab`, `node`, `network`, `link`, `folder`, `console`, `recording`, `image`, `template`, `system` |
| `action` | For example `create`, `start`, `open`, `close`, `mark`, `pull`, `settings`, `login_failed` |
| `actor` | A username |
| `from`, `to` | Epoch-second bounds, inclusive |
| `limit`, `offset` | Paging; `limit` is at most 1000 |

The response holds the page of entries, the `total` that matches the filters, and the list of `actors` seen in the log.

```
GET /api/system/logs/activity?category=auth&action=login_failed&limit=50
```
