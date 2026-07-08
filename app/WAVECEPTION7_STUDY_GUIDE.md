# `waveception7.py` Study Guide

## 1. What the program does

`waveception7.py` is a bridge between two systems:

1. It watches **Inception** for access-granted and access-denied events.
2. It retrieves the Inception user associated with each event.
3. It stores the user and event in a local **SQLite database**.
4. It uses a door-to-camera JSON map to decide what to create in **Wave**:
   - A mapped door produces a camera bookmark.
   - An unmapped door produces a generic Wave event.
5. It updates the database to record whether Wave delivery succeeded or failed.

The same file also provides a Tkinter configuration editor and optional Windows Service support. It is therefore a single module with three entry modes rather than three separate programs.

## 2. The most important distinction: classes, methods, and functions

These constructs can look similar because all callable definitions use `def`, but they play different roles.

### Classes

A `class` is a blueprint for objects that keep related state and behavior together.

| Class | Purpose | State retained on each object |
|---|---|---|
| `WaveceptionService` | Adapts the worker to the Windows Service API | stop event handles and stop state |
| `InceptionClient` | Communicates with Inception | URL, credentials/token, and an HTTP session |
| `WaveClient` | Communicates with Wave | URL, credentials, TLS choice, bookmark timing, login state, and an HTTP session |

`WaveceptionService` is conditionally defined only when `pywin32` imports successfully. `InceptionClient` and `WaveClient` are always defined.

### Methods

A method is a function defined inside a class. It is called through the class or an instance.

```python
client = InceptionClient(...)
client.login()
```

Here, `InceptionClient` is the class, `client` is an instance, and `login` is an instance method. Its `self` parameter refers to that particular `client` object.

`WaveClient.require_success` is slightly different: `@staticmethod` makes it a utility method associated with the class but gives it no automatic `self` parameter. It only evaluates the response passed to it.

`__init__` methods are initializers. They run when an instance is created and save values on `self`; they are not ordinary top-level setup functions.

### Top-level functions

Definitions beginning at the left margin, such as `load_config`, `connect_database`, `process_event`, and `run_worker`, are module-level functions. They do not automatically retain object state. The state they need must be passed as arguments or obtained from module constants.

### Nested GUI functions

`add_entry`, `refresh_tree`, `selected_mapping`, `add_or_update_mapping`, `delete_mapping`, `collect_config`, and `save_all` are functions defined *inside* `open_config_window`. They are not methods because they are not inside a class.

They work as GUI callbacks and form **closures**: they can access names such as `tree`, `door_map`, `fields`, and `root` from the surrounding `open_config_window` scope. They exist only while that invocation of the configuration window is alive.

## 3. Major sections of the file

### Imports and constants

- Standard-library imports cover command-line parsing, JSON, logging, SQLite, subprocesses, threading, dates, paths, and type hints.
- `requests` performs HTTP calls.
- `pywin32` imports are optional. Import failure assigns `None` to their names instead of preventing console/config use.
- `ACCESS_EVENT_TYPES` translates Inception category `"2006"` to `"granted"` and `"2007"` to `"denied"`.
- Path constants determine where configuration, the door map, logs, and the default database live.

### Configuration and small helpers

| Function | Role |
|---|---|
| `deep_merge` | Recursively overlays saved settings on defaults without losing unspecified nested defaults. |
| `load_config` | Loads JSON, merges defaults, and then applies environment-variable overrides. |
| `save_config` | Writes formatted configuration JSON. |
| `parse_bool` | Converts booleans and common truth-like strings to `True`/`False`. |
| `required_config` | Reads a required setting or raises `RuntimeError`; removes a trailing slash from base URLs. |
| `utc_now` | Returns the current timezone-aware UTC timestamp as ISO text. |
| `load_door_map` | Loads/validates the JSON door map and creates a default file if absent. |
| `save_door_map` | Writes formatted door-map JSON. |

Environment variables override the JSON configuration, so the value in the file is not necessarily the value used by the worker.

### `InceptionClient`

- `__init__`: stores credentials and creates a persistent `requests.Session`. `trust_env = False` prevents inherited proxy settings from interfering on local security networks. If supplied, the API token is placed in the session's `Authorization` header.
- `login`: API-token mode needs no login request. Username/password mode posts credentials and stores the returned login ID as a cookie.
- `latest_event_reference`: fetches the newest review event to establish the starting reference for long polling.
- `monitor_events`: performs the long-poll request for only the configured access-event categories.
- `get_user`: retrieves full information for one Inception user.
- `review_events_since`: pages through historical events in groups of 100. This is the reconnect/catch-up path.

### `WaveClient`

- `__init__`: stores Wave configuration and creates its HTTP session.
- `require_success`: raises a detailed `RuntimeError` for unsuccessful Wave responses.
- `login`: creates a Wave login session and cookie.
- `create_ticket`: ensures login and obtains the one-time authorization ticket used by later requests.
- `create_bookmark`: translates an access event into a bookmark for a mapped camera, including pre-roll and post-roll time.
- `create_generic_event`: translates an event for an unmapped door into a generic Wave event.

The classes have similar HTTP-oriented method names, but they represent different remote systems and authentication flows. `InceptionClient` reads source data; `WaveClient` delivers derived output.

## 4. SQLite and SQL: detailed walkthrough

SQLite is the program's durable memory. It prevents completed events from being delivered repeatedly, supports retry decisions, and provides a timestamp for catch-up after a restart.

### Opening and initializing the database

`connect_database(database_path)` does four things:

1. Creates the database's parent directory.
2. Calls `sqlite3.connect`, creating the file if needed.
3. Sets `row_factory = sqlite3.Row`, allowing query rows to support column-name access as well as numeric indexing. This file mostly uses numeric indexing, but the connection is prepared for both.
4. Enables foreign-key enforcement and creates the tables if necessary.

```sql
PRAGMA foreign_keys = ON;
```

SQLite does not reliably enforce declared foreign keys unless this is enabled for each connection.

### Table: `users`

One row represents the latest fetched snapshot of an Inception user.

| Column | Meaning |
|---|---|
| `inception_user_id` | Primary key from Inception |
| `name` | Required display name |
| `email_address` | Optional email |
| `user_json` | Full source object serialized as JSON text |
| `inception_updated_at` | Update time supplied by Inception |
| `fetched_at` | Time Waveception retrieved this snapshot |

The normalized columns make common values easy to query, while `user_json` preserves the complete upstream record.

### Table: `access_events`

One row represents one Inception access event and its Wave delivery state.

| Column | Meaning |
|---|---|
| `inception_event_id` | Primary key; enforces event uniqueness |
| `inception_user_id` | Required foreign key to `users` |
| `event_type` | Only `granted` or `denied`, enforced by `CHECK` |
| `occurred_at` | Inception event time |
| `event_json` | Complete source event as JSON text |
| `wave_status` | Starts as `pending`, later becomes `failed` or `delivered` |
| `wave_response` | Wave response body or an error string |
| `created_at` | Local insertion time |
| `delivered_at` | Set only for successful delivery |

The foreign key means an event cannot reference a user row that does not exist. The code therefore upserts the user before inserting the event, in the same transaction.

### Saving a user and event

`save_user_and_event` first uses a parameterized existence query:

```sql
SELECT 1
FROM access_events
WHERE inception_event_id = ?;
```

`?` is a placeholder. The event ID is supplied separately as a one-item tuple `(event_id,)`. This is safer than constructing SQL with an f-string and lets the database driver bind the value correctly.

If the event is new, the user is written with an **upsert**:

```sql
INSERT INTO users (...)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(inception_user_id) DO UPDATE SET
    name = excluded.name,
    ...;
```

The insertion is attempted normally. If the primary key already exists, SQLite updates that user instead. `excluded.name` means the `name` value from the row that the program attempted to insert.

The event is then inserted with its default `wave_status` of `pending`. The `with database:` block is a transaction context manager: normal exit commits both writes; an exception rolls the transaction back. This keeps user and event persistence atomic.

The function returns `True` for a newly inserted event and `False` for an unsupported or already-stored event. That return value controls later retry logic.

### Duplicate detection versus retry behavior

An existing event is not automatically ignored. `process_event` queries its status:

```sql
SELECT wave_status
FROM access_events
WHERE inception_event_id = ?;
```

- `delivered`: stop; the event has already reached Wave.
- `pending` or `failed`: attempt Wave delivery again.

This distinction is crucial: the primary key provides idempotent local storage, while `wave_status` decides whether external work remains.

### Recording delivery results

`record_wave_result` executes a parameterized update inside another transaction:

```sql
UPDATE access_events
SET wave_status = ?, wave_response = ?, delivered_at = ?
WHERE inception_event_id = ?;
```

For `delivered`, `delivered_at` receives `utc_now()`. For failure, it receives SQL `NULL` because Python `None` is bound as `NULL`. The response column stores either Wave's response text or the exception message.

### Restart and catch-up query

At worker startup/reconnection, this query finds the newest event time known to have reached Wave:

```sql
SELECT MAX(occurred_at)
FROM access_events
WHERE wave_status = 'delivered';
```

`MAX` is an aggregate. It returns one row containing the latest timestamp, or `NULL` when there are no delivered rows. When a timestamp exists, the worker asks Inception for events since that point. Duplicate records are safe because the primary-key/status checks handle them.

### SQL/Python mechanics worth remembering

- `database.execute(...).fetchone()` executes a query and returns one row.
- `.fetchone()[0]` extracts the first selected column from that row.
- `(event_id,)` is a one-element tuple; the comma is required.
- `json.dumps(...)` turns dictionaries into JSON strings for SQLite `TEXT` columns.
- `None` becomes SQL `NULL` when bound as a parameter.
- SQL identifiers and fixed SQL structure are written in the query; changing values are passed as parameters.
- No explicit `commit()` appears because `with database:` commits or rolls back automatically.
- `closing(connect_database(...))` closes the connection when `run_worker` exits; it is separate from the transaction contexts.

## 5. End-to-end program flow

### Entry-point routing

```text
Operating system / Python
        |
        v
if __name__ == "__main__"
        |
        v
      main()
        |
        +-- recognized service command --> run_service_command(...)
        |
        +-- --config --------------------> open_config_window()
        |
        +-- otherwise -------------------> run_worker()
```

`--console` is accepted by `argparse`, but the value is not separately tested: worker mode is already the default for any non-service, non-`--config` invocation.

### Worker startup

1. `run_worker` loads configuration.
2. Logging is configured for a stream or service log file.
3. `build_clients` validates important settings and constructs both client objects plus database/map paths.
4. `connect_database` opens and initializes SQLite.
5. The outer loop continues until the stop event is set.

### Monitor and delivery cycle

1. Log in to Inception (or confirm token mode).
2. If no live-monitor reference exists:
   - Query SQLite for the last delivered time.
   - Ask Inception for possible missed events since that time.
   - Process each missed event.
   - Fetch the newest Inception event as the live-monitor reference.
3. Long-poll Inception for updates.
4. For each event:
   - Reload the door map, so mapping changes can take effect without restarting.
   - Call `process_event`.
   - Advance the live reference ID and timestamp.
5. On expected network/data errors, log the exception, wait five seconds in a stop-aware way, and retry.

### `process_event` flow

```text
event received
    |
    +-- no real user ID --> log and skip
    |
    v
fetch user from Inception
    |
    v
store/upsert user and insert event in SQLite
    |
    +-- existing + delivered --> stop
    |
    +-- existing + pending/failed --> retry delivery
    |
    v
look up event door in door_map["doors"]
    |
    +-- mapped ----> create Wave camera bookmark
    |
    +-- unmapped --> create Wave generic event
    |
    +-- exception -> SQL status = failed
    |
    +-- success ---> SQL status = delivered
```

### GUI flow

`open_config_window` loads current JSON, creates widgets, and defines nested callback functions. Button and selection events call those functions later. Adding/deleting a mapping changes the in-memory `door_map`; pressing **Save** writes both configuration and mapping JSON files.

### Windows Service flow

The service class is available only with `pywin32`. Windows calls `SvcDoRun`, which calls the same `run_worker` used by console mode. Windows calls `SvcStop`, which sets a `threading.Event`; worker loops and retry waits check that event and exit cooperatively. Install/update commands also call `set_service_recovery` to request automatic restarts after failures.

## 6. Atypical or easily missed Python syntax

### Future annotations

```python
from __future__ import annotations
```

This postpones evaluation of type annotations. Type hints can refer to types more flexibly and have less runtime impact.

### Modern generic type hints

```python
dict[str, Any]
list[dict[str, Any]]
tuple[InceptionClient, WaveClient, Path, Path]
Optional[str]
```

These describe expected types but Python does not enforce them automatically. `Any` means unrestricted type. `Optional[str]` means `str | None`.

### Conditional class definition

```python
if win32serviceutil is not None:
    class WaveceptionService(...):
```

The class statement itself executes only when the condition is true. Without `pywin32`, the class name is never defined, but service handling rejects service commands before trying to use it.

### Inheritance

```python
class WaveceptionService(win32serviceutil.ServiceFramework):
```

The service class inherits behavior required by pywin32. Its initializer explicitly calls the parent initializer.

### Decorator

```python
@staticmethod
def require_success(...):
```

The decorator changes how the method is bound. It can be called without an instance-specific `self` argument.

### Context managers

```python
with CONFIG_PATH.open(...) as file:
with database:
with closing(connect_database(...)) as database:
```

All use `with`, but they clean up different resources: file handles, database transactions, and the database connection itself.

### Exception chaining

```python
raise RuntimeError(...) from error
```

This presents a friendlier application error while preserving the original JSON exception as its cause.

### Dictionary unpacking is not used, but dictionary traversal is

`deep_merge` recursively visits nested dictionaries. In `load_config`, `path[:-1]` means every path component except the last, while `path[-1]` means the final component.

### Truthy fallback expressions

```python
event.get("What") or event.get("Where") or "Unknown door"
```

Python evaluates left to right and returns the first truthy value. This is concise fallback selection, not a Boolean-only result.

### Conditional expression

```python
utc_now() if status == "delivered" else None
```

This is Python's inline `if/else`; it chooses the value passed into SQL.

### Comprehension-like joining

```python
",".join(ACCESS_EVENT_TYPES)
```

Iterating a dictionary yields its keys, so this becomes `"2006,2007"` in insertion order.

### Starred argument expansion

```python
tree.delete(*tree.get_children())
[sys.argv[0], *argv]
```

`*` expands an iterable into separate positional arguments or list elements.

### Default callback argument

```python
def selected_mapping(_event: object = None) -> None:
```

Tkinter passes an event object when invoked by `bind`, but the default also permits calling the function with no argument. The leading underscore signals that the value is intentionally unused.

### The main guard

```python
if __name__ == "__main__":
    main()
```

This runs `main` when the file is executed directly, but not when it is imported as a module.

## 7. Suggested reading order

For a first pass, read the file in execution order rather than line order:

1. `main`
2. `run_service_command` and `parse_args`
3. `run_worker`
4. `build_clients`
5. `connect_database`
6. `InceptionClient`
7. `process_event`
8. `save_user_and_event` and `record_wave_result`
9. `WaveClient`
10. Configuration helpers
11. `open_config_window`
12. `WaveceptionService` and `set_service_recovery`

The central chain to memorize is:

```text
main -> run_worker -> Inception monitor -> process_event
     -> SQLite persistence -> Wave delivery -> SQLite result update
```

## 8. Review questions

1. Why is `InceptionClient` a class while `process_event` is a top-level function?
2. How is a nested GUI callback different from a class method?
3. Why must the user row be written before the access-event row?
4. What does `ON CONFLICT ... DO UPDATE` accomplish?
5. Why are SQL values represented by `?` rather than inserted with f-strings?
6. What is the difference between `pending`, `failed`, and `delivered`?
7. Why can an existing database event still be processed again?
8. What causes a bookmark instead of a generic event?
9. How does the program recover events missed during downtime?
10. What cleanup does each of the program's three important `with` patterns perform?

