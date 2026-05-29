---
name: clickup
description: Manage ClickUp tasks, lists, spaces, and comments via the REST API. Search, create, update, and investigate tickets. Uses personal API key auth (no OAuth needed). All operations via curl — no dependencies.
version: 1.0.0
author: Hermes Agent
license: MIT
prerequisites:
  env_vars: [CLICKUP_API_KEY]
  commands: [curl]
metadata:
  hermes:
    tags: [ClickUp, Project Management, Tasks, API, Productivity]
---

# ClickUp — Task & Project Management

Manage ClickUp tasks, lists, and spaces directly via the REST API using `curl`. No MCP server, no OAuth flow, no extra dependencies.

## Setup

1. Get a personal API key from **ClickUp Settings > Apps > API Token > Generate**
2. Set `CLICKUP_API_KEY` in your environment (via `hermes setup` or your env config)

## API Basics

- **Base URL:** `https://api.clickup.com/api/v2`
- **Auth header:** `Authorization: $CLICKUP_API_KEY` (no "Bearer" prefix for personal tokens)
- **Content-Type:** `application/json` on all POST/PUT requests
- **IDs:** numeric strings for `team_id`/`space_id`/`folder_id`/`list_id`; alphanumeric for `task_id` (e.g. `86c9bekw9`); custom task IDs (e.g. `CU-ab12cd`) require `custom_task_ids=true&team_id=<team_id>` query params

Base curl pattern:
```bash
curl -s "https://api.clickup.com/api/v2/user" \
  -H "Authorization: $CLICKUP_API_KEY" | python3 -m json.tool
```

## Hierarchy

ClickUp organizes data in a strict hierarchy — **Workspace → Space → (optional Folder) → List → Task**.

| Level | ClickUp term | API term | Typical use |
|-------|--------------|----------|-------------|
| Workspace | Workspace | `team` | The org root |
| Category | Space | `space` | Department / product area |
| Group | Folder | `folder` | Optional grouping of lists |
| Board | List | `list` | Where tasks live |
| Ticket | Task | `task` | The unit of work |

**Note:** The API calls a Workspace a `team`. Don't let that confuse you — `team_id` is your Workspace ID.

## Priority & Status

**Priority values:** 1 = Urgent, 2 = High, 3 = Normal, 4 = Low, `null` = No priority

**Statuses are per-list and user-defined.** Common conventions:

| Type | Typical names |
|------|---------------|
| `open` | "backlog", "to do", "open" |
| `custom` | "in progress", "review", "blocked", "ready for testing" |
| `closed` | "done", "complete", "cancelled" |

Always query the list's statuses before assuming a name exists — see "List statuses" below.

## Common Queries

### Get current user
```bash
curl -s "https://api.clickup.com/api/v2/user" \
  -H "Authorization: $CLICKUP_API_KEY" | python3 -m json.tool
```

### List workspaces (teams)
```bash
curl -s "https://api.clickup.com/api/v2/team" \
  -H "Authorization: $CLICKUP_API_KEY" | python3 -m json.tool
```

### List spaces in a workspace
```bash
curl -s "https://api.clickup.com/api/v2/team/$TEAM_ID/space?archived=false" \
  -H "Authorization: $CLICKUP_API_KEY" | python3 -m json.tool
```

### List folders in a space
```bash
curl -s "https://api.clickup.com/api/v2/space/$SPACE_ID/folder?archived=false" \
  -H "Authorization: $CLICKUP_API_KEY" | python3 -m json.tool
```

### List lists in a space (folderless) or folder
```bash
# Folderless lists in a space
curl -s "https://api.clickup.com/api/v2/space/$SPACE_ID/list?archived=false" \
  -H "Authorization: $CLICKUP_API_KEY" | python3 -m json.tool

# Lists inside a folder
curl -s "https://api.clickup.com/api/v2/folder/$FOLDER_ID/list?archived=false" \
  -H "Authorization: $CLICKUP_API_KEY" | python3 -m json.tool
```

### List statuses (on a list)
`GET /list/{list_id}` returns the list metadata including its `statuses[]` array — each has `status`, `type`, `orderindex`.
```bash
curl -s "https://api.clickup.com/api/v2/list/$LIST_ID" \
  -H "Authorization: $CLICKUP_API_KEY" | python3 -m json.tool
```

### List tasks in a list (with filters)
```bash
curl -s "https://api.clickup.com/api/v2/list/$LIST_ID/task?archived=false&include_closed=false&page=0" \
  -H "Authorization: $CLICKUP_API_KEY" | python3 -m json.tool
```

Useful query params (repeat bracketed params for multi-value):
- `statuses[]=backlog&statuses[]=in%20progress` — filter by status name (URL-encode spaces)
- `assignees[]=<user_id>` — filter by assignee
- `tags[]=bug` — filter by tag
- `date_updated_gt=<unix_ms>` — only tasks updated after this timestamp
- `order_by=created|updated|due_date` and `reverse=true`
- `include_subtasks=true`
- `page=N` — pagination (0-indexed, 100 per page)

### Get a single task (rich)
```bash
curl -s "https://api.clickup.com/api/v2/task/$TASK_ID?include_subtasks=true" \
  -H "Authorization: $CLICKUP_API_KEY" | python3 -m json.tool
```

Returns: `id`, `name`, `description` (markdown), `status`, `priority`, `assignees`, `watchers`, `tags`, `custom_fields`, `attachments`, `linked_tasks`, `url`, `date_created`, `date_updated`, subtasks.

### Get a task by custom id (e.g. CU-ab12cd)
```bash
curl -s "https://api.clickup.com/api/v2/task/CU-ab12cd?custom_task_ids=true&team_id=$TEAM_ID" \
  -H "Authorization: $CLICKUP_API_KEY" | python3 -m json.tool
```

### List comments on a task
```bash
curl -s "https://api.clickup.com/api/v2/task/$TASK_ID/comment" \
  -H "Authorization: $CLICKUP_API_KEY" | python3 -m json.tool
```

### List attachments on a task
Attachments are returned inside `GET /task/$TASK_ID` under `attachments[]`, each with `url`, `title`, `mimetype`, `size`.

### Search tasks across a workspace (filtered view)
ClickUp has no single global search endpoint. The recommended paths:

1. **Filter inside a known list** — use `/list/{list_id}/task?<filters>` (above).
2. **Run a saved view** — if the workspace has a view saved for this query, call it:
   ```bash
   curl -s "https://api.clickup.com/api/v2/view/$VIEW_ID/task?page=0" \
     -H "Authorization: $CLICKUP_API_KEY" | python3 -m json.tool
   ```
3. **Filtered team tasks** — `/team/{team_id}/task` with filters walks the workspace:
   ```bash
   curl -s "https://api.clickup.com/api/v2/team/$TEAM_ID/task?page=0&statuses[]=in%20progress&order_by=updated&reverse=true" \
     -H "Authorization: $CLICKUP_API_KEY" | python3 -m json.tool
   ```

### List custom fields on a list
```bash
curl -s "https://api.clickup.com/api/v2/list/$LIST_ID/field" \
  -H "Authorization: $CLICKUP_API_KEY" | python3 -m json.tool
```

## Common Mutations

### Create a task
```bash
curl -s -X POST "https://api.clickup.com/api/v2/list/$LIST_ID/task" \
  -H "Authorization: $CLICKUP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Fix login bug",
    "description": "Users cannot login with SSO.\n\nRepro: ...",
    "priority": 2,
    "assignees": [],
    "tags": ["bug"],
    "status": "backlog"
  }' | python3 -m json.tool
```

### Update a task (status, priority, assignees, name, description, due_date)
```bash
curl -s -X PUT "https://api.clickup.com/api/v2/task/$TASK_ID" \
  -H "Authorization: $CLICKUP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"status": "in progress"}' | python3 -m json.tool
```

Assignees need add/remove arrays (not a replacement list):
```bash
curl -s -X PUT "https://api.clickup.com/api/v2/task/$TASK_ID" \
  -H "Authorization: $CLICKUP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"assignees": {"add": [123456], "rem": []}}' | python3 -m json.tool
```

### Add a comment
```bash
curl -s -X POST "https://api.clickup.com/api/v2/task/$TASK_ID/comment" \
  -H "Authorization: $CLICKUP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "comment_text": "Triage complete. Root cause: SSO redirect drops PKCE verifier on step-up auth. See attached log.",
    "notify_all": false
  }' | python3 -m json.tool
```

### Set a custom field value
```bash
curl -s -X POST "https://api.clickup.com/api/v2/task/$TASK_ID/field/$FIELD_ID" \
  -H "Authorization: $CLICKUP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"value": "value-for-the-field"}' | python3 -m json.tool
```

### Add a tag
```bash
curl -s -X POST "https://api.clickup.com/api/v2/task/$TASK_ID/tag/needs-triage" \
  -H "Authorization: $CLICKUP_API_KEY" | python3 -m json.tool
```

### Link two tasks
```bash
curl -s -X POST "https://api.clickup.com/api/v2/task/$TASK_ID/link/$OTHER_TASK_ID" \
  -H "Authorization: $CLICKUP_API_KEY" | python3 -m json.tool
```

### Delete a task
```bash
curl -s -X DELETE "https://api.clickup.com/api/v2/task/$TASK_ID" \
  -H "Authorization: $CLICKUP_API_KEY"
```

## Pagination

ClickUp uses 0-indexed page numbers with a fixed page size of **100 tasks per page** for list endpoints:

```bash
# Page 0 (first 100)
curl -s "https://api.clickup.com/api/v2/list/$LIST_ID/task?page=0" \
  -H "Authorization: $CLICKUP_API_KEY"

# Page 1 (next 100)
curl -s "https://api.clickup.com/api/v2/list/$LIST_ID/task?page=1" \
  -H "Authorization: $CLICKUP_API_KEY"
```

Keep paging until the response's `tasks[]` comes back empty or shorter than 100.

## Webhooks (inbound events)

ClickUp signs webhook payloads with HMAC-SHA256 using the secret returned when the webhook was created. The signature arrives in the `X-Signature` header as a raw hex digest (no `sha256=` prefix).

Verify in a Hermes webhook route by configuring `secret:` in `config.yaml` under `platforms.webhook.extra.routes.<route>`; Hermes' generic HMAC check will accept it.

Useful event types: `taskCreated`, `taskUpdated`, `taskStatusUpdated`, `taskCommentPosted`, `taskDeleted`.

## Typical Workflow (triage / ops)

1. **Find the list** — `GET /team/$TEAM_ID/space` → `/space/$SPACE_ID/folder` (or `/list`) → `GET /list/$LIST_ID` to see its statuses
2. **Pull active work** — `GET /list/$LIST_ID/task?statuses[]=backlog&statuses[]=in%20progress&include_closed=false`
3. **Investigate one** — `GET /task/$TASK_ID?include_subtasks=true` + `GET /task/$TASK_ID/comment` for the full picture
4. **Post findings** — `POST /task/$TASK_ID/comment` with the triage summary
5. **Move it along** — `PUT /task/$TASK_ID` with `{"status": "in progress"}` or assign someone
6. **Close the loop** — transition to the list's closed-type status when done

## Rate Limits

- **100 requests/minute per personal token** (workspace-level limit shared across all calls on that token)
- Response header `X-RateLimit-Remaining` tracks budget; `X-RateLimit-Reset` is the unix epoch at which it resets
- Use `page` to avoid over-fetching and `statuses[]` filters to narrow results

## Important Notes

- Always use `terminal` tool with `curl` for API calls — do NOT use `web_extract` or `browser`
- Always inspect the `err` field in responses — ClickUp returns HTTP 200 for some errors with an `err` body
- The `description` field accepts markdown; `text_content` is the plaintext render
- Status names are **case-insensitive** on write but the API returns them in the casing the list owner chose
- The Workspace is called `team` in the URL; `team_id` and workspace id are the same thing
- URL-encode spaces in status filters (`in%20progress`, not `in progress`)
- Custom task IDs (`CU-xxx`) require `custom_task_ids=true&team_id=<team_id>` query params on every task endpoint
- Use `python3 -m json.tool` or `jq` to format JSON responses for readability
