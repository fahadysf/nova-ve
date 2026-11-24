# Activity log

The activity log is the record of **who did what, from where, on which lab**. Open it from the main menu under **System → Logs → Activity log**. It is visible to administrators only.

## What is recorded

Each entry names the user, the source address, the time, and the object involved.

| Area | Events |
|---|---|
| Authentication | Sign-in, **failed sign-in**, sign-out |
| Users | Account created, updated (with the fields that changed), deleted |
| Labs | Created, deleted, moved, and the lab-wide *stop all* / *wipe all* actions |
| Nodes | Created, updated, started, stopped, wiped, deleted |
| Networks | Created, updated, deleted |
| Links | Connected, updated, disconnected |
| Folders | Created, renamed, deleted |

A few rules decide what does and does not become an entry:

- **An entry means it happened.** Actions are recorded after they succeed. A refused or failed action (a start that errored, a delete that was denied) leaves no entry — except for failed sign-ins, which are recorded because repeated failures are the signal worth seeing.
- **Saving without changing anything is not an entry.** Re-opening a node's edit dialog and clicking save, or stopping a lab in which nothing is running, writes nothing.
- **Passwords are never recorded.** A password change appears only as "password changed".
- **The trail outlives the account.** Deleting a user does not remove their earlier entries.
- **A session that times out is not a sign-out.** Only an explicit logout is recorded.

Node positions and link styling (layout changes) are not recorded, as they do not affect what a lab does.

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
| `category` | `auth`, `user`, `lab`, `node`, `network`, `link`, `folder` |
| `action` | For example `create`, `start`, `login_failed` |
| `actor` | A username |
| `from`, `to` | Epoch-second bounds, inclusive |
| `limit`, `offset` | Paging; `limit` is at most 1000 |

The response holds the page of entries, the `total` that matches the filters, and the list of `actors` seen in the log.

```
GET /api/system/logs/activity?category=auth&action=login_failed&limit=50
```
