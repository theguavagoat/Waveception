"""Waveception production worker, configuration editor, and optional Windows Service.

This file is a standalone successor to waveception6.py. It intentionally does
not import waveception5.py, so it can be deployed as a single script.

Typical use:

    python waveception7.py --config
        Opens a configuration window for Inception, Wave, SecurOS, and
        destination-specific door/camera mappings.

    python waveception7.py --console
        Runs the worker in the current terminal using the saved config.

    python waveception7.py install
    python waveception7.py start
    python waveception7.py stop
    python waveception7.py remove
        Optional Windows Service commands. These require pywin32:
        python -m pip install pywin32

The worker still supports environment variables as overrides, but the preferred
production path is waveception_config.json plus the Wave and SecurOS mapping
JSON files.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlencode

import requests

try:
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil
except ImportError:
    servicemanager = None
    win32event = None
    win32service = None
    win32serviceutil = None


ACCESS_EVENT_TYPES = {"2006": "granted", "2007": "denied"}
APP_DIR = Path(__file__).resolve().parent
WINDOWS_PROGRAMDATA = Path(os.getenv("PROGRAMDATA", str(APP_DIR)))
DEFAULT_DATA_DIR = Path(os.getenv("WAVECEPTION_DATA_DIR", WINDOWS_PROGRAMDATA / "Waveception"))
LOCAL_CONFIG_PATH = APP_DIR / "waveception_config.json"
CONFIG_PATH = Path(os.getenv("WAVECEPTION_CONFIG", DEFAULT_DATA_DIR / "waveception_config.json"))
DEFAULT_DOOR_MAP_PATH = DEFAULT_DATA_DIR / "door_camera_map.json"
DEFAULT_SECUROS_MAP_PATH = DEFAULT_DATA_DIR / "securos_camera_map.json"
POLL_TIMEOUT_SECONDS = 70  # Inception may hold a long-poll request for 60 seconds.
RETRY_DELAY_SECONDS = 5
SERVICE_NAME = "Waveception"
SERVICE_DISPLAY_NAME = "Waveception Inception/Wave Integration"
SERVICE_DESCRIPTION = "Monitors Inception access events and delivers them to Wave and/or SecurOS."


if win32serviceutil is not None:
    class WaveceptionService(win32serviceutil.ServiceFramework):  # type: ignore[union-attr]
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY_NAME
        _svc_description_ = SERVICE_DESCRIPTION

        def __init__(self, args: list[str]) -> None:
            win32serviceutil.ServiceFramework.__init__(self, args)
            self.stop_event_handle = win32event.CreateEvent(None, 0, 0, None)
            self.stop_requested = threading.Event()

        def SvcStop(self) -> None:
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            self.stop_requested.set()
            win32event.SetEvent(self.stop_event_handle)

        def SvcDoRun(self) -> None:
            servicemanager.LogInfoMsg(f"{SERVICE_DISPLAY_NAME} started")
            run_worker(stop_event=self.stop_requested, service_mode=True)
            servicemanager.LogInfoMsg(f"{SERVICE_DISPLAY_NAME} stopped")


DEFAULT_CONFIG: dict[str, Any] = {
    "inception": {
        "base_url": "",
        "api_token": "",
        "username": "",
        "password": "",
    },
    "wave": {
        "base_url": "",
        "username": "",
        "password": "",
        "verify_tls": False,
        "bookmark_pre_roll_ms": 5000,
        "bookmark_post_roll_ms": 5000,
    },
    "securos": {
        "base_url": "",
        "username": "",
        "password": "",
        "verify_tls": False,
        "event_gate_url": "",
        "pre_roll_ms": 5000,
        "post_roll_ms": 5000,
        "media_client_id": "",
    },
    "database_path": str(DEFAULT_DATA_DIR / "waveception.sqlite"),
    "door_map_path": str(DEFAULT_DOOR_MAP_PATH),
    "securos_map_path": str(DEFAULT_SECUROS_MAP_PATH),
    "log_level": "INFO",
}


def deep_merge(defaults: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Return defaults recursively updated with values from overrides."""
    merged = dict(defaults)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config() -> dict[str, Any]:
    """Load waveception_config.json and apply environment variable overrides."""
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open(encoding="utf-8") as file:
                config = deep_merge(DEFAULT_CONFIG, json.load(file))
        except json.JSONDecodeError as error:
            raise RuntimeError(f"Invalid JSON in {CONFIG_PATH}: {error}") from error
    else:
        config = deep_merge(DEFAULT_CONFIG, {})

    env_map = {
        "INCEPTION_BASE_URL": ("inception", "base_url"),
        "INCEPTION_API_TOKEN": ("inception", "api_token"),
        "INCEPTION_USERNAME": ("inception", "username"),
        "INCEPTION_PASSWORD": ("inception", "password"),
        "WAVE_BASE_URL": ("wave", "base_url"),
        "WAVE_USERNAME": ("wave", "username"),
        "WAVE_PASSWORD": ("wave", "password"),
        "SECUROS_BASE_URL": ("securos", "base_url"),
        "SECUROS_USERNAME": ("securos", "username"),
        "SECUROS_PASSWORD": ("securos", "password"),
        "SECUROS_EVENT_GATE_URL": ("securos", "event_gate_url"),
        "WAVECEPTION_DATABASE": ("database_path",),
        "WAVECEPTION_DOOR_MAP": ("door_map_path",),
        "WAVECEPTION_SECUROS_MAP": ("securos_map_path",),
        "LOG_LEVEL": ("log_level",),
    }
    for env_name, path in env_map.items():
        value = os.getenv(env_name)
        if not value:
            continue
        target = config
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = value

    if os.getenv("WAVE_VERIFY_TLS"):
        config["wave"]["verify_tls"] = parse_bool(os.getenv("WAVE_VERIFY_TLS", "false"))
    if os.getenv("WAVE_BOOKMARK_PRE_ROLL_MS"):
        config["wave"]["bookmark_pre_roll_ms"] = int(os.getenv("WAVE_BOOKMARK_PRE_ROLL_MS", "5000"))
    if os.getenv("WAVE_BOOKMARK_POST_ROLL_MS"):
        config["wave"]["bookmark_post_roll_ms"] = int(os.getenv("WAVE_BOOKMARK_POST_ROLL_MS", "5000"))
    if os.getenv("SECUROS_VERIFY_TLS"):
        config["securos"]["verify_tls"] = parse_bool(os.getenv("SECUROS_VERIFY_TLS", "false"))

    return config


def save_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG_PATH.open("w", encoding="utf-8") as file:
        json.dump(config, file, indent=2)
        file.write("\n")


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def required_config(config: dict[str, Any], section: str, key: str) -> str:
    value = str(config.get(section, {}).get(key, "")).strip()
    if not value:
        raise RuntimeError(f"Set {section}.{key} in {CONFIG_PATH} before starting the worker.")
    return value.rstrip("/") if key == "base_url" else value


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_door_map(path: Path) -> dict[str, Any]:
    """Load the editable Inception-door to Wave-camera mapping."""
    try:
        with path.open(encoding="utf-8") as file:
            config = json.load(file)
    except FileNotFoundError:
        config = {"doors": {}, "unmapped_door": {"source": "Waveception"}}
        save_door_map(path, config)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Invalid JSON in {path}: {error}") from error
    if not isinstance(config.get("doors"), dict):
        raise RuntimeError(f"{path.name} must contain a 'doors' object.")
    config.setdefault("unmapped_door", {"source": "Waveception"})
    return config


def save_door_map(path: Path, config: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(config, file, indent=2)
        file.write("\n")


def load_securos_map(path: Path) -> dict[str, Any]:
    """Load the editable Inception-door to SecurOS-camera mapping."""
    try:
        with path.open(encoding="utf-8") as file:
            config = json.load(file)
    except FileNotFoundError:
        config = {"doors": {}}
        save_door_map(path, config)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Invalid JSON in {path}: {error}") from error
    if not isinstance(config.get("doors"), dict):
        raise RuntimeError(f"{path.name} must contain a 'doors' object.")
    return config


def connect_database(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            inception_user_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            email_address TEXT,
            user_json TEXT NOT NULL,
            inception_updated_at TEXT,
            fetched_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS access_events (
            inception_event_id TEXT PRIMARY KEY,
            inception_user_id TEXT NOT NULL,
            event_type TEXT NOT NULL CHECK (event_type IN ('granted', 'denied')),
            occurred_at TEXT,
            event_json TEXT NOT NULL,
            wave_status TEXT NOT NULL DEFAULT 'pending',
            wave_response TEXT,
            securos_status TEXT NOT NULL DEFAULT 'pending',
            securos_response TEXT,
            created_at TEXT NOT NULL,
            delivered_at TEXT,
            securos_delivered_at TEXT,
            FOREIGN KEY (inception_user_id) REFERENCES users(inception_user_id)
        );
        """
    )
    # Upgrade databases created by earlier Wave-only releases in place.
    existing_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(access_events)")
    }
    migrations = {
        "securos_status": "TEXT NOT NULL DEFAULT 'pending'",
        "securos_response": "TEXT",
        "securos_delivered_at": "TEXT",
    }
    for column, declaration in migrations.items():
        if column not in existing_columns:
            connection.execute(
                f"ALTER TABLE access_events ADD COLUMN {column} {declaration}"
            )
            if column == "securos_status":
                # Events predating SecurOS support must not flood a newly
                # configured SecurOS system during the first upgrade.
                connection.execute(
                    "UPDATE access_events SET securos_status = 'disabled'"
                )
    connection.commit()
    return connection


class InceptionClient:
    def __init__(
        self,
        base_url: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        api_token: Optional[str] = None,
    ) -> None:
        self.base_url = base_url
        self.username = username
        self.password = password
        self.api_token = api_token
        self.session = requests.Session()
        # Services can inherit machine/user proxy settings that are wrong for
        # local security networks. Keep Waveception's internal API calls direct.
        self.session.trust_env = False
        if api_token:
            # Inception's API Token Linking UI specifies this exact scheme.
            self.session.headers["Authorization"] = f"APIToken {api_token}"

    def login(self) -> None:
        if self.api_token:
            logging.info("Using Inception API-token authentication.")
            return
        if not self.username or not self.password:
            raise RuntimeError(
                f"Set inception.api_token, or both inception.username and inception.password in {CONFIG_PATH}."
            )
        response = self.session.post(
            f"{self.base_url}/api/v1/authentication/login",
            json={"Username": self.username, "Password": self.password},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("Response", {}).get("Result") != "Success" or not payload.get("UserID"):
            raise RuntimeError(f"Inception login failed: {payload}")
        self.session.cookies.set("LoginSessId", payload["UserID"])
        logging.info("Logged in to Inception.")

    def latest_event_reference(self) -> tuple[str, str]:
        response = self.session.get(
            f"{self.base_url}/api/v1/review",
            params={"dir": "desc", "limit": 1},
            timeout=20,
        )
        response.raise_for_status()
        events = response.json().get("Data", [])
        if not events:
            raise RuntimeError("Inception returned no review events to use as a monitor reference.")
        return events[0]["ID"], str(events[0]["WhenTicks"])

    def monitor_events(self, reference_id: str, reference_time: str) -> dict[str, Any]:
        request_body = [
            {
                "ID": "Waveception_AccessEvents",
                "RequestType": "LiveReviewEvents",
                "InputData": {
                    "referenceId": reference_id,
                    "referenceTime": reference_time,
                    "messageTypeIdFilter": ",".join(ACCESS_EVENT_TYPES),
                },
            }
        ]
        response = self.session.post(
            f"{self.base_url}/api/v1/monitor-updates",
            json=request_body,
            timeout=POLL_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.json() if response.content else {}

    def get_user(self, user_id: str) -> dict[str, Any]:
        response = self.session.get(
            f"{self.base_url}/api/v1/config/user/{user_id}", timeout=20
        )
        response.raise_for_status()
        return response.json()

    def review_events_since(self, start_time: str) -> list[dict[str, Any]]:
        """Retrieve all access events after the last successfully delivered one."""
        events: list[dict[str, Any]] = []
        offset = 0
        while True:
            response = self.session.get(
                f"{self.base_url}/api/v1/review",
                params={
                    "messageTypeIdFilter": ",".join(ACCESS_EVENT_TYPES),
                    "start": start_time,
                    "end": datetime.now(timezone.utc).isoformat(),
                    "limit": 100,
                    "offset": offset,
                },
                timeout=30,
            )
            response.raise_for_status()
            page = response.json().get("Data", [])
            events.extend(page)
            if len(page) < 100:
                return events
            offset += len(page)


def save_user_and_event(
    database: sqlite3.Connection, event: dict[str, Any], user: dict[str, Any]
) -> bool:
    """Save the current user and event. Returns False for an already processed event."""
    event_id = event["ID"]
    event_type = ACCESS_EVENT_TYPES.get(str(event.get("MessageCategory")))
    if not event_type:
        logging.warning("Ignoring unrecognized event type: %s", event.get("MessageCategory"))
        return False

    with database:
        already_exists = database.execute(
            "SELECT 1 FROM access_events WHERE inception_event_id = ?", (event_id,)
        ).fetchone()
        if already_exists:
            return False
        database.execute(
            """
            INSERT INTO users (inception_user_id, name, email_address, user_json,
                               inception_updated_at, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(inception_user_id) DO UPDATE SET
                name = excluded.name,
                email_address = excluded.email_address,
                user_json = excluded.user_json,
                inception_updated_at = excluded.inception_updated_at,
                fetched_at = excluded.fetched_at
            """,
            (
                user["ID"],
                user.get("Name", "Unnamed user"),
                user.get("EmailAddress"),
                json.dumps(user),
                user.get("DateTimeUpdated"),
                utc_now(),
            ),
        )
        database.execute(
            """
            INSERT INTO access_events (inception_event_id, inception_user_id, event_type,
                                       occurred_at, event_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (event_id, user["ID"], event_type, event.get("When"), json.dumps(event), utc_now()),
        )
    return True


class WaveClient:
    """Creates device bookmarks using Wave's session and one-time ticket flow."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        verify_tls: bool,
        pre_roll_ms: int,
        post_roll_ms: int,
    ) -> None:
        self.base_url = base_url
        self.username = username
        self.password = password
        self.verify_tls = verify_tls
        self.pre_roll_ms = pre_roll_ms
        self.post_roll_ms = post_roll_ms
        self.session = requests.Session()
        # Avoid accidental proxy use when running as a Windows Service.
        self.session.trust_env = False
        self.logged_in = False

    @staticmethod
    def require_success(response: requests.Response, action: str) -> None:
        """Raise an error that retains Wave's useful response detail."""
        if response.ok:
            return
        detail = response.text.strip().replace("\n", " ")[:500]
        raise RuntimeError(
            f"Wave {action} failed (HTTP {response.status_code}): "
            f"{detail or response.reason}"
        )

    def login(self) -> None:
        response = self.session.post(
            f"{self.base_url}/rest/v3/login/sessions",
            json={"username": self.username, "password": self.password, "setCookie": True},
            timeout=20,
            verify=self.verify_tls,
        )
        self.require_success(response, "login")
        self.logged_in = True
        logging.info("Logged in to Wave.")

    def create_ticket(self) -> str:
        if not self.logged_in:
            self.login()
        # setCookie=True during login puts the valid Wave session in this
        # requests.Session, which is then sent with this authorization request.
        response = self.session.post(
            f"{self.base_url}/rest/v3/login/tickets", timeout=20, verify=self.verify_tls
        )
        self.require_success(response, "ticket creation")
        ticket = response.json().get("token")
        if not ticket:
            raise RuntimeError("Wave did not return an authorization ticket.")
        return ticket

    def create_bookmark(
        self, event: dict[str, Any], user: dict[str, Any], device_id: str
    ) -> requests.Response:
        event_type = ACCESS_EVENT_TYPES[str(event["MessageCategory"])]
        start_time = datetime.fromisoformat(event["When"].replace("Z", "+00:00"))
        event_time_ms = int(start_time.timestamp() * 1000)
        user_name = user.get("Name", "Unnamed user")
        payload = {
            "name": user_name,
            "description": (
                f"Inception access {event_type}. "
                f"{event.get('Description', '')} "
                f"Door: {event.get('What') or event.get('Where') or 'Unknown door'}"
            ).strip(),
            # Start before the access event and extend after it to create a clip.
            "startTimeMs": event_time_ms - self.pre_roll_ms,
            "durationMs": self.pre_roll_ms + self.post_roll_ms,
            "tags": ["inception", "access", event_type, user_name],
        }
        response = self.session.post(
            f"{self.base_url}/rest/v3/devices/{device_id}/bookmarks",
            params={"_ticket": self.create_ticket()},
            json=payload,
            timeout=20,
            verify=self.verify_tls,
        )
        self.require_success(response, "bookmark creation")
        return response

    def create_generic_event(
        self, event: dict[str, Any], user: dict[str, Any], config: dict[str, Any]
    ) -> requests.Response:
        if not self.logged_in:
            self.login()
        event_type = ACCESS_EVENT_TYPES[str(event["MessageCategory"])]
        timestamp = datetime.fromisoformat(event["When"].replace("Z", "+00:00")).astimezone(timezone.utc)
        settings = config.get("unmapped_door", {})
        payload = {
            "source": settings.get("source", "Waveception"),
            "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
            "caption": f"Access {event_type.title()}",
            "description": (
                f"User: {user.get('Name', 'Unnamed user')}\n"
                f"Door: {event.get('What') or event.get('Where') or 'Unknown door'}"
            ),
            "metadata": {"inception_event_id": event["ID"], "inception_user_id": user["ID"]},
            "state": "Active",
        }
        response = self.session.post(
            f"{self.base_url}/api/createEvent",
            params={"_ticket": self.create_ticket()},
            json=payload,
            timeout=20,
            verify=self.verify_tls,
        )
        self.require_success(response, "Generic Event creation")
        return response


class SecurOSClient:
    """Uses SecurOS REST for discovery and HTTP Event Gate for access events."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        event_gate_url: str,
        verify_tls: bool,
        pre_roll_ms: int = 5000,
        post_roll_ms: int = 5000,
        media_client_id: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.event_gate_url = event_gate_url.rstrip("/")
        self.verify_tls = verify_tls
        self.pre_roll_ms = pre_roll_ms
        self.post_roll_ms = post_roll_ms
        self.media_client_id = media_client_id.strip()
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.auth = (username, password)
        self.gate_session = requests.Session()
        self.gate_session.trust_env = False

    @staticmethod
    def require_success(response: requests.Response, action: str) -> None:
        if response.ok:
            return
        detail = response.text.strip().replace("\n", " ")[:500]
        raise RuntimeError(
            f"SecurOS {action} failed (HTTP {response.status_code}): "
            f"{detail or response.reason}"
        )

    def list_cameras(self) -> list[dict[str, Any]]:
        response = self.session.get(
            f"{self.base_url}/api/v1/cameras", timeout=20, verify=self.verify_tls
        )
        self.require_success(response, "camera discovery")
        payload = response.json()
        if payload.get("status") != "success" or not isinstance(payload.get("data"), list):
            raise RuntimeError("SecurOS camera discovery returned an unexpected response.")
        return payload["data"]

    def create_access_event(
        self,
        event: dict[str, Any],
        user: dict[str, Any],
        camera_id: str,
        camera_name: str,
    ) -> requests.Response:
        if not self.event_gate_url:
            raise RuntimeError("Set the SecurOS HTTP Event Gate URL before delivering events.")
        event_type = ACCESS_EVENT_TYPES[str(event["MessageCategory"])]
        door_name = event.get("What") or event.get("Where") or "Unknown door"
        user_name = user.get("Name", "Unnamed user")
        endpoint = (
            self.event_gate_url
            if self.event_gate_url.lower().endswith("/event")
            else f"{self.event_gate_url}/event"
        )
        params = {
            "source": "Waveception",
            "caption": f"Access {event_type.title()}",
            "access_result": event_type,
            "user": user_name,
            "door": door_name,
            "camera_id": camera_id,
            "camera_name": camera_name,
            "inception_event_id": event["ID"],
            "inception_user_id": user["ID"],
            "timestamp": event.get("When", ""),
            "pre_roll_ms": str(self.pre_roll_ms),
            "post_roll_ms": str(self.post_roll_ms),
            "media_client_id": self.media_client_id,
        }
        body = {
            "source": "Waveception",
            "access_result": event_type,
            "user": user_name,
            "door": door_name,
            "camera_id": camera_id,
            "camera_name": camera_name,
            "inception_event_id": event["ID"],
            "inception_user_id": user["ID"],
            "timestamp": event.get("When"),
            "pre_roll_ms": self.pre_roll_ms,
            "post_roll_ms": self.post_roll_ms,
            "media_client_id": self.media_client_id,
        }
        # Event Gate exposes query parameters as individual SecurOS event
        # fields and the JSON document as the event's _body value.
        # SecurOS HTTP Event Gate treats '+' literally rather than as a space,
        # so encode query values with %20 instead of application/x-www-form-urlencoded '+' characters.
        event_url = f"{endpoint}?{urlencode(params, quote_via=quote)}"
        response = self.gate_session.post(
            event_url,
            json=body,
            timeout=20,
            verify=self.verify_tls,
        )
        self.require_success(response, "HTTP Event Gate delivery")
        return response


def record_destination_result(
    database: sqlite3.Connection,
    event_id: str,
    destination: str,
    status: str,
    response: Optional[str],
) -> None:
    if destination not in {"wave", "securos"}:
        raise ValueError(f"Unknown delivery destination: {destination}")
    delivered_column = "delivered_at" if destination == "wave" else "securos_delivered_at"
    with database:
        database.execute(
            f"""
            UPDATE access_events
            SET {destination}_status = ?, {destination}_response = ?, {delivered_column} = ?
            WHERE inception_event_id = ?
            """,
            (status, response, utc_now() if status == "delivered" else None, event_id),
        )


def process_event(
    database: sqlite3.Connection,
    client: InceptionClient,
    wave: Optional[WaveClient],
    securos: Optional[SecurOSClient],
    wave_map: dict[str, Any],
    securos_map: dict[str, Any],
    event: dict[str, Any],
) -> None:
    user_id = event.get("WhoID")
    if not user_id or user_id == "00000000-0000-0000-0000-000000000000":
        logging.warning("Skipping event %s because it has no associated user.", event.get("ID"))
        return
    user = client.get_user(user_id)
    is_new = save_user_and_event(database, event, user)
    statuses = database.execute(
        "SELECT wave_status, securos_status FROM access_events WHERE inception_event_id = ?",
        (event["ID"],),
    ).fetchone()
    if not is_new:
        logging.info(
            "Revisiting stored event %s (Wave: %s, SecurOS: %s).",
            event["ID"], statuses["wave_status"], statuses["securos_status"],
        )

    door_name = event.get("What") or event.get("Where") or "Unknown door"
    delivered_any = False

    if wave is None:
        if statuses["wave_status"] not in {"disabled", "delivered"}:
            record_destination_result(database, event["ID"], "wave", "disabled", None)
    elif statuses["wave_status"] != "delivered":
        try:
            device_id = wave_map["doors"].get(door_name)
            if device_id:
                response = wave.create_bookmark(event, user, device_id)
                description = f"Wave bookmark for {door_name}"
            else:
                response = wave.create_generic_event(event, user, wave_map)
                description = "Wave Generic Event for unmapped door"
        except (requests.RequestException, RuntimeError, ValueError) as error:
            logging.exception("Wave delivery failed for event %s.", event["ID"])
            record_destination_result(database, event["ID"], "wave", "failed", str(error))
        else:
            record_destination_result(database, event["ID"], "wave", "delivered", response.text)
            delivered_any = True
            logging.info("Created %s.", description)

    if securos is None:
        if statuses["securos_status"] not in {"disabled", "delivered"}:
            record_destination_result(database, event["ID"], "securos", "disabled", None)
    elif statuses["securos_status"] != "delivered":
        mapping = securos_map.get("doors", {}).get(door_name, {})
        if isinstance(mapping, str):
            mapping = {"camera_id": mapping, "camera_name": ""}
        camera_id = str(mapping.get("camera_id", "")).strip()
        camera_name = str(mapping.get("camera_name", "")).strip()
        try:
            response = securos.create_access_event(
                event, user, camera_id=camera_id, camera_name=camera_name
            )
        except (requests.RequestException, RuntimeError, ValueError) as error:
            logging.exception("SecurOS delivery failed for event %s.", event["ID"])
            record_destination_result(database, event["ID"], "securos", "failed", str(error))
        else:
            record_destination_result(
                database, event["ID"], "securos", "delivered", response.text
            )
            delivered_any = True
            logging.info(
                "Created SecurOS access event for %s%s.",
                door_name,
                f" on camera {camera_name or camera_id}" if camera_id else " (unmapped)",
            )

    if delivered_any:
        logging.info(
            "Stored %s event for %s.",
            ACCESS_EVENT_TYPES[str(event["MessageCategory"])],
            user.get("Name"),
        )


def build_clients(
    config: dict[str, Any],
) -> tuple[InceptionClient, Optional[WaveClient], Optional[SecurOSClient], Path, Path, Path]:
    inception = config["inception"]
    wave_config = config["wave"]
    securos_config = config["securos"]
    client = InceptionClient(
        required_config(config, "inception", "base_url"),
        username=str(inception.get("username", "")).strip() or None,
        password=str(inception.get("password", "")).strip() or None,
        api_token=str(inception.get("api_token", "")).strip() or None,
    )
    wave_base_url = str(wave_config.get("base_url", "")).strip().rstrip("/")
    wave: Optional[WaveClient] = None
    if wave_base_url:
        wave = WaveClient(
            wave_base_url,
            required_config(config, "wave", "username"),
            required_config(config, "wave", "password"),
            verify_tls=parse_bool(wave_config.get("verify_tls", False)),
            pre_roll_ms=int(wave_config.get("bookmark_pre_roll_ms", 5000)),
            post_roll_ms=int(wave_config.get("bookmark_post_roll_ms", 5000)),
        )
    securos_base_url = str(securos_config.get("base_url", "")).strip().rstrip("/")
    securos: Optional[SecurOSClient] = None
    if securos_base_url:
        securos = SecurOSClient(
            securos_base_url,
            required_config(config, "securos", "username"),
            required_config(config, "securos", "password"),
            str(securos_config.get("event_gate_url", "")).strip(),
            verify_tls=parse_bool(securos_config.get("verify_tls", False)),
            pre_roll_ms=int(securos_config.get("pre_roll_ms", 5000)),
            post_roll_ms=int(securos_config.get("post_roll_ms", 5000)),
            media_client_id=str(securos_config.get("media_client_id", "")),
        )
    if wave is None and securos is None:
        raise RuntimeError("Configure at least one destination: Wave or SecurOS.")
    database_path = Path(config.get("database_path") or DEFAULT_CONFIG["database_path"])
    door_map_path = Path(config.get("door_map_path") or DEFAULT_DOOR_MAP_PATH)
    securos_map_path = Path(config.get("securos_map_path") or DEFAULT_SECUROS_MAP_PATH)
    return client, wave, securos, database_path, door_map_path, securos_map_path


def configure_logging(config: dict[str, Any], service_mode: bool = False) -> None:
    log_level = str(config.get("log_level", "INFO")).upper()
    handlers: list[logging.Handler] = []
    if service_mode:
        DEFAULT_DATA_DIR.mkdir(parents=True, exist_ok=True)
        log_path = DEFAULT_DATA_DIR / "waveception_service.log"
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    else:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )


def retry_incomplete_deliveries(
    database: sqlite3.Connection,
    client: InceptionClient,
    wave: Optional[WaveClient],
    securos: Optional[SecurOSClient],
    wave_map_path: Path,
    securos_map_path: Path,
) -> None:
    conditions: list[str] = []
    if wave is not None:
        conditions.append("wave_status IN ('pending', 'failed')")
    if securos is not None:
        conditions.append("securos_status IN ('pending', 'failed')")
    if not conditions:
        return
    rows = database.execute(
        f"""
        SELECT event_json FROM access_events
        WHERE {' OR '.join(conditions)}
        ORDER BY occurred_at
        LIMIT 100
        """
    ).fetchall()
    if rows:
        logging.info("Retrying %s incomplete destination delivery/deliveries.", len(rows))
    for row in rows:
        process_event(
            database,
            client,
            wave,
            securos,
            load_door_map(wave_map_path),
            load_securos_map(securos_map_path),
            json.loads(row["event_json"]),
        )


def run_worker(stop_event: Optional[threading.Event] = None, service_mode: bool = False) -> None:
    config = load_config()
    configure_logging(config, service_mode=service_mode)
    client, wave, securos, database_path, door_map_path, securos_map_path = build_clients(config)
    stop_event = stop_event or threading.Event()
    with closing(connect_database(database_path)) as database:
        reference_id: Optional[str] = None
        reference_time: Optional[str] = None
        while not stop_event.is_set():
            try:
                client.login()
                if reference_id is None or reference_time is None:
                    retry_incomplete_deliveries(
                        database, client, wave, securos, door_map_path, securos_map_path
                    )
                    last_stored = database.execute(
                        "SELECT MAX(occurred_at) FROM access_events"
                    ).fetchone()[0]
                    if last_stored:
                        missed_events = client.review_events_since(last_stored)
                        logging.info("Checking %s missed Inception event(s).", len(missed_events))
                        for event in missed_events:
                            if stop_event.is_set():
                                return
                            process_event(
                                database, client, wave, securos,
                                load_door_map(door_map_path),
                                load_securos_map(securos_map_path), event,
                            )
                    reference_id, reference_time = client.latest_event_reference()
                    logging.info("Monitoring new events after %s.", reference_id)
                while not stop_event.is_set():
                    update = client.monitor_events(reference_id, reference_time)
                    events = update.get("Result", [])
                    for event in events:
                        if stop_event.is_set():
                            return
                        process_event(
                            database, client, wave, securos,
                            load_door_map(door_map_path),
                            load_securos_map(securos_map_path), event,
                        )
                        reference_id = event["ID"]
                        reference_time = str(event["WhenTicks"])
                    retry_incomplete_deliveries(
                        database, client, wave, securos, door_map_path, securos_map_path
                    )
            except (requests.RequestException, RuntimeError, KeyError, ValueError) as error:
                logging.exception(
                    "Inception monitor interrupted: %s; retrying in %s seconds.",
                    error,
                    RETRY_DELAY_SECONDS,
                )
                # Force a source catch-up after connectivity returns.
                reference_id = None
                reference_time = None
                stop_event.wait(RETRY_DELAY_SECONDS)


def open_config_window() -> None:
    """Open the configuration GUI for Inception, Wave, and SecurOS."""
    import tkinter as tk
    from tkinter import messagebox, ttk

    config = load_config()
    wave_map_path = Path(config.get("door_map_path") or DEFAULT_DOOR_MAP_PATH)
    securos_map_path = Path(config.get("securos_map_path") or DEFAULT_SECUROS_MAP_PATH)
    wave_map = load_door_map(wave_map_path)
    securos_map = load_securos_map(securos_map_path)

    root = tk.Tk()
    root.title("Waveception Configuration")
    root.geometry("940x620")

    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True, padx=10, pady=10)

    general_tab = ttk.Frame(notebook)
    wave_tab = ttk.Frame(notebook)
    securos_tab = ttk.Frame(notebook)
    wave_mapping_tab = ttk.Frame(notebook)
    securos_mapping_tab = ttk.Frame(notebook)
    notebook.add(general_tab, text="Inception / General")
    notebook.add(wave_tab, text="Wave")
    notebook.add(securos_tab, text="SecurOS")
    notebook.add(wave_mapping_tab, text="Wave Door Mapping")
    notebook.add(securos_mapping_tab, text="SecurOS Door Mapping")

    fields: dict[str, tk.Variable] = {}

    def add_entry(
        parent: ttk.Frame, row: int, label: str, key: str, value: Any, show: str = ""
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=6)
        variable = tk.StringVar(value="" if value is None else str(value))
        entry = ttk.Entry(parent, textvariable=variable, show=show, width=75)
        entry.grid(row=row, column=1, sticky="ew", padx=8, pady=6)
        fields[key] = variable

    for tab in (general_tab, wave_tab, securos_tab):
        tab.columnconfigure(1, weight=1)

    add_entry(general_tab, 0, "Inception Base URL", "inception.base_url", config["inception"].get("base_url", ""))
    add_entry(general_tab, 1, "Inception API Token", "inception.api_token", config["inception"].get("api_token", ""), show="*")
    add_entry(general_tab, 2, "Inception Username", "inception.username", config["inception"].get("username", ""))
    add_entry(general_tab, 3, "Inception Password", "inception.password", config["inception"].get("password", ""), show="*")
    add_entry(general_tab, 4, "Database Path", "database_path", config.get("database_path", ""))
    add_entry(general_tab, 5, "Wave Mapping JSON", "door_map_path", str(wave_map_path))
    add_entry(general_tab, 6, "SecurOS Mapping JSON", "securos_map_path", str(securos_map_path))
    add_entry(general_tab, 7, "Log Level", "log_level", config.get("log_level", "INFO"))

    add_entry(wave_tab, 0, "Wave Base URL (blank disables Wave)", "wave.base_url", config["wave"].get("base_url", ""))
    add_entry(wave_tab, 1, "Wave Username", "wave.username", config["wave"].get("username", ""))
    add_entry(wave_tab, 2, "Wave Password", "wave.password", config["wave"].get("password", ""), show="*")
    add_entry(wave_tab, 3, "Bookmark Pre-Roll MS", "wave.bookmark_pre_roll_ms", config["wave"].get("bookmark_pre_roll_ms", 5000))
    add_entry(wave_tab, 4, "Bookmark Post-Roll MS", "wave.bookmark_post_roll_ms", config["wave"].get("bookmark_post_roll_ms", 5000))
    wave_verify_tls_var = tk.BooleanVar(value=parse_bool(config["wave"].get("verify_tls", False)))
    ttk.Checkbutton(
        wave_tab,
        text="Verify Wave TLS certificate",
        variable=wave_verify_tls_var,
    ).grid(row=5, column=1, sticky="w", padx=8, pady=6)

    add_entry(securos_tab, 0, "SecurOS REST Base URL (blank disables SecurOS)", "securos.base_url", config["securos"].get("base_url", ""))
    add_entry(securos_tab, 1, "SecurOS Username", "securos.username", config["securos"].get("username", ""))
    add_entry(securos_tab, 2, "SecurOS Password", "securos.password", config["securos"].get("password", ""), show="*")
    add_entry(securos_tab, 3, "HTTP Event Gate URL", "securos.event_gate_url", config["securos"].get("event_gate_url", ""))
    add_entry(securos_tab, 4, "Recording Pre-Roll MS", "securos.pre_roll_ms", config["securos"].get("pre_roll_ms", 5000))
    add_entry(securos_tab, 5, "Recording Post-Roll MS", "securos.post_roll_ms", config["securos"].get("post_roll_ms", 5000))
    add_entry(securos_tab, 6, "Dedicated SecurOS Media Client ID", "securos.media_client_id", config["securos"].get("media_client_id", ""))
    securos_verify_tls_var = tk.BooleanVar(value=parse_bool(config["securos"].get("verify_tls", False)))
    ttk.Checkbutton(
        securos_tab, text="Verify SecurOS TLS certificate", variable=securos_verify_tls_var
    ).grid(row=7, column=1, sticky="w", padx=8, pady=6)
    ttk.Label(
        securos_tab,
        text="Example REST URL: http://192.168.202.74:8888    Event Gate: http://192.168.202.74:88",
    ).grid(row=8, column=0, columnspan=2, sticky="w", padx=8, pady=6)

    def copy_securos_bridge() -> None:
        bridge_path = APP_DIR / "securos_waveception_bridge.js"
        try:
            script = bridge_path.read_text(encoding="utf-8")
        except OSError as error:
            messagebox.showerror("Bridge script unavailable", str(error))
            return
        root.clipboard_clear()
        root.clipboard_append(script)
        root.update()
        messagebox.showinfo(
            "Bridge script copied",
            "Create a Node.js Scripts group and Node.js Script object beneath "
            "EB-ANALYTICS > Integration & Automation, paste the script, then Apply it.",
        )

    ttk.Button(
        securos_tab, text="Copy SecurOS Recording Bridge Script", command=copy_securos_bridge
    ).grid(row=9, column=1, sticky="w", padx=8, pady=10)

    wave_tree = ttk.Treeview(wave_mapping_tab, columns=("door", "device"), show="headings", height=14)
    wave_tree.heading("door", text="Inception Door Name")
    wave_tree.heading("device", text="Wave Camera Device ID")
    wave_tree.column("door", width=300)
    wave_tree.column("device", width=500)
    wave_tree.grid(row=0, column=0, columnspan=4, sticky="nsew", padx=8, pady=8)
    wave_mapping_tab.rowconfigure(0, weight=1)
    wave_mapping_tab.columnconfigure(1, weight=1)
    wave_mapping_tab.columnconfigure(3, weight=1)
    wave_door_var = tk.StringVar()
    wave_device_var = tk.StringVar()
    source_var = tk.StringVar(value=wave_map.get("unmapped_door", {}).get("source", "Waveception"))

    def refresh_wave_tree() -> None:
        wave_tree.delete(*wave_tree.get_children())
        for door, device in sorted(wave_map.get("doors", {}).items()):
            wave_tree.insert("", "end", values=(door, device))

    def selected_wave_mapping(_event: object = None) -> None:
        selected = wave_tree.selection()
        if not selected:
            return
        door, device = wave_tree.item(selected[0], "values")
        wave_door_var.set(door)
        wave_device_var.set(device)

    def add_or_update_wave_mapping() -> None:
        door = wave_door_var.get().strip()
        device = wave_device_var.get().strip()
        if not door or not device:
            messagebox.showwarning("Missing value", "Enter both a door name and a Wave camera device ID.")
            return
        wave_map.setdefault("doors", {})[door] = device
        refresh_wave_tree()

    def delete_wave_mapping() -> None:
        door = wave_door_var.get().strip()
        if door in wave_map.get("doors", {}):
            del wave_map["doors"][door]
            wave_door_var.set("")
            wave_device_var.set("")
            refresh_wave_tree()

    wave_tree.bind("<<TreeviewSelect>>", selected_wave_mapping)
    refresh_wave_tree()
    ttk.Label(wave_mapping_tab, text="Door Name").grid(row=1, column=0, sticky="w", padx=8, pady=6)
    ttk.Entry(wave_mapping_tab, textvariable=wave_door_var).grid(row=1, column=1, sticky="ew", padx=8, pady=6)
    ttk.Label(wave_mapping_tab, text="Device ID").grid(row=1, column=2, sticky="w", padx=8, pady=6)
    ttk.Entry(wave_mapping_tab, textvariable=wave_device_var).grid(row=1, column=3, sticky="ew", padx=8, pady=6)
    ttk.Button(wave_mapping_tab, text="Add / Update", command=add_or_update_wave_mapping).grid(row=2, column=1, sticky="w", padx=8, pady=6)
    ttk.Button(wave_mapping_tab, text="Delete Selected", command=delete_wave_mapping).grid(row=2, column=3, sticky="w", padx=8, pady=6)
    ttk.Label(wave_mapping_tab, text="Unmapped Door Event Source").grid(row=3, column=0, sticky="w", padx=8, pady=6)
    ttk.Entry(wave_mapping_tab, textvariable=source_var).grid(row=3, column=1, sticky="ew", padx=8, pady=6)

    # SecurOS mappings use stable camera IDs while displaying friendly names.
    securos_tree = ttk.Treeview(
        securos_mapping_tab, columns=("door", "camera", "id"), show="headings", height=14
    )
    securos_tree.heading("door", text="Inception Door Name")
    securos_tree.heading("camera", text="SecurOS Camera")
    securos_tree.heading("id", text="Camera ID")
    securos_tree.column("door", width=300)
    securos_tree.column("camera", width=390)
    securos_tree.column("id", width=100)
    securos_tree.grid(row=0, column=0, columnspan=4, sticky="nsew", padx=8, pady=8)
    securos_mapping_tab.rowconfigure(0, weight=1)
    securos_mapping_tab.columnconfigure(1, weight=1)
    securos_mapping_tab.columnconfigure(3, weight=1)

    observed_doors: set[str] = set(wave_map.get("doors", {})) | set(securos_map.get("doors", {}))
    try:
        db_path = Path(config.get("database_path") or DEFAULT_CONFIG["database_path"])
        if db_path.exists():
            with closing(sqlite3.connect(db_path)) as mapping_db:
                for (raw_event,) in mapping_db.execute("SELECT event_json FROM access_events"):
                    event_data = json.loads(raw_event)
                    name = event_data.get("What") or event_data.get("Where")
                    if name:
                        observed_doors.add(str(name))
    except (OSError, sqlite3.Error, json.JSONDecodeError):
        logging.debug("Could not load observed door names for configuration UI.", exc_info=True)

    securos_door_var = tk.StringVar()
    securos_camera_var = tk.StringVar()
    camera_choices: dict[str, dict[str, Any]] = {}
    door_combo = ttk.Combobox(
        securos_mapping_tab, textvariable=securos_door_var, values=sorted(observed_doors)
    )
    camera_combo = ttk.Combobox(securos_mapping_tab, textvariable=securos_camera_var, state="readonly")

    def refresh_securos_tree() -> None:
        securos_tree.delete(*securos_tree.get_children())
        for door, mapping in sorted(securos_map.get("doors", {}).items()):
            if isinstance(mapping, str):
                mapping = {"camera_id": mapping, "camera_name": ""}
            securos_tree.insert(
                "", "end", values=(door, mapping.get("camera_name", ""), mapping.get("camera_id", ""))
            )

    def selected_securos_mapping(_event: object = None) -> None:
        selected = securos_tree.selection()
        if not selected:
            return
        door, camera_name, camera_id = securos_tree.item(selected[0], "values")
        securos_door_var.set(door)
        display = next(
            (label for label, camera in camera_choices.items() if str(camera.get("id")) == str(camera_id)),
            f"{camera_name} - ID {camera_id}",
        )
        securos_camera_var.set(display)

    def current_securos_client() -> SecurOSClient:
        base_url = fields["securos.base_url"].get().strip().rstrip("/")
        username = fields["securos.username"].get().strip()
        password = fields["securos.password"].get()
        if not base_url or not username or not password:
            raise RuntimeError("Enter the SecurOS REST URL, username, and password first.")
        return SecurOSClient(
            base_url, username, password,
            fields["securos.event_gate_url"].get().strip(), securos_verify_tls_var.get(),
            int(fields["securos.pre_roll_ms"].get().strip() or "5000"),
            int(fields["securos.post_roll_ms"].get().strip() or "5000"),
            fields["securos.media_client_id"].get().strip(),
        )

    def load_securos_cameras() -> None:
        try:
            cameras = current_securos_client().list_cameras()
        except (requests.RequestException, RuntimeError, ValueError) as error:
            messagebox.showerror("SecurOS connection failed", str(error))
            return
        camera_choices.clear()
        for camera in sorted(cameras, key=lambda item: str(item.get("name", "")).lower()):
            status = camera.get("status", {})
            online = parse_bool(status.get("valid", False))
            label = f"{camera.get('name', 'Unnamed camera')} - ID {camera.get('id')} - {'Online' if online else 'Unavailable'}"
            camera_choices[label] = camera
        camera_combo["values"] = list(camera_choices)
        messagebox.showinfo("SecurOS connected", f"Loaded {len(camera_choices)} camera(s).")

    def add_or_update_securos_mapping() -> None:
        door = securos_door_var.get().strip()
        selected_label = securos_camera_var.get().strip()
        camera = camera_choices.get(selected_label)
        if not door or camera is None:
            messagebox.showwarning("Missing value", "Choose a door and load/select a SecurOS camera.")
            return
        securos_map.setdefault("doors", {})[door] = {
            "camera_id": str(camera.get("id", "")),
            "camera_name": str(camera.get("name", "")),
        }
        observed_doors.add(door)
        door_combo["values"] = sorted(observed_doors)
        refresh_securos_tree()

    def delete_securos_mapping() -> None:
        door = securos_door_var.get().strip()
        if door in securos_map.get("doors", {}):
            del securos_map["doors"][door]
            securos_door_var.set("")
            securos_camera_var.set("")
            refresh_securos_tree()

    securos_tree.bind("<<TreeviewSelect>>", selected_securos_mapping)
    refresh_securos_tree()
    ttk.Label(securos_mapping_tab, text="Door Name").grid(row=1, column=0, sticky="w", padx=8, pady=6)
    door_combo.grid(row=1, column=1, sticky="ew", padx=8, pady=6)
    ttk.Label(securos_mapping_tab, text="SecurOS Camera").grid(row=1, column=2, sticky="w", padx=8, pady=6)
    camera_combo.grid(row=1, column=3, sticky="ew", padx=8, pady=6)
    ttk.Button(securos_mapping_tab, text="Load Cameras / Test Connection", command=load_securos_cameras).grid(row=2, column=0, sticky="w", padx=8, pady=6)
    ttk.Button(securos_mapping_tab, text="Add / Update", command=add_or_update_securos_mapping).grid(row=2, column=1, sticky="w", padx=8, pady=6)
    ttk.Button(securos_mapping_tab, text="Delete Selected", command=delete_securos_mapping).grid(row=2, column=3, sticky="w", padx=8, pady=6)

    def collect_config() -> dict[str, Any]:
        updated = deep_merge(DEFAULT_CONFIG, config)
        updated["inception"]["base_url"] = fields["inception.base_url"].get().strip().rstrip("/")
        updated["inception"]["api_token"] = fields["inception.api_token"].get().strip()
        updated["inception"]["username"] = fields["inception.username"].get().strip()
        updated["inception"]["password"] = fields["inception.password"].get()
        updated["wave"]["base_url"] = fields["wave.base_url"].get().strip().rstrip("/")
        updated["wave"]["username"] = fields["wave.username"].get().strip()
        updated["wave"]["password"] = fields["wave.password"].get()
        updated["wave"]["verify_tls"] = wave_verify_tls_var.get()
        updated["wave"]["bookmark_pre_roll_ms"] = int(fields["wave.bookmark_pre_roll_ms"].get().strip() or "5000")
        updated["wave"]["bookmark_post_roll_ms"] = int(fields["wave.bookmark_post_roll_ms"].get().strip() or "5000")
        updated["database_path"] = fields["database_path"].get().strip()
        updated["door_map_path"] = fields["door_map_path"].get().strip()
        updated["securos_map_path"] = fields["securos_map_path"].get().strip()
        updated["securos"]["base_url"] = fields["securos.base_url"].get().strip().rstrip("/")
        updated["securos"]["username"] = fields["securos.username"].get().strip()
        updated["securos"]["password"] = fields["securos.password"].get()
        updated["securos"]["event_gate_url"] = fields["securos.event_gate_url"].get().strip().rstrip("/")
        updated["securos"]["verify_tls"] = securos_verify_tls_var.get()
        updated["securos"]["pre_roll_ms"] = int(fields["securos.pre_roll_ms"].get().strip() or "5000")
        updated["securos"]["post_roll_ms"] = int(fields["securos.post_roll_ms"].get().strip() or "5000")
        updated["securos"]["media_client_id"] = fields["securos.media_client_id"].get().strip()
        updated["log_level"] = fields["log_level"].get().strip() or "INFO"
        if not updated["wave"]["base_url"] and not updated["securos"]["base_url"]:
            raise ValueError("Configure a Wave Base URL, a SecurOS Base URL, or both.")
        if updated["securos"]["base_url"] and not updated["securos"]["event_gate_url"]:
            raise ValueError("SecurOS is enabled, but its HTTP Event Gate URL is blank.")
        if updated["securos"]["base_url"] and not updated["securos"]["media_client_id"]:
            raise ValueError("SecurOS is enabled, but its dedicated Media Client ID is blank.")
        return updated

    def save_all() -> None:
        try:
            updated_config = collect_config()
            wave_map.setdefault("unmapped_door", {})["source"] = source_var.get().strip() or "Waveception"
            save_config(updated_config)
            save_door_map(Path(updated_config["door_map_path"]), wave_map)
            save_door_map(Path(updated_config["securos_map_path"]), securos_map)
        except ValueError as error:
            messagebox.showerror("Invalid number", f"Pre/post roll values must be whole numbers.\n\n{error}")
            return
        except OSError as error:
            messagebox.showerror("Save failed", str(error))
            return
        messagebox.showinfo(
            "Saved",
            f"Saved config:\n{CONFIG_PATH}\n\n"
            f"Saved Wave map:\n{updated_config['door_map_path']}\n\n"
            f"Saved SecurOS map:\n{updated_config['securos_map_path']}\n\n"
            "Restart the Waveception service for connection-setting changes.",
        )

    button_bar = ttk.Frame(root)
    button_bar.pack(fill="x", padx=10, pady=(0, 10))
    ttk.Button(button_bar, text="Save", command=save_all).pack(side="right", padx=4)
    ttk.Button(button_bar, text="Close", command=root.destroy).pack(side="right", padx=4)

    root.mainloop()


def set_service_recovery() -> None:
    """Ask Windows Service Control Manager to restart this service after failures."""
    commands = [
        ["sc.exe", "failure", SERVICE_NAME, "reset=", "86400", "actions=", "restart/60000/restart/60000/restart/60000"],
        ["sc.exe", "failureflag", SERVICE_NAME, "1"],
    ]
    for command in commands:
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            logging.warning("Could not set service recovery with %s: %s", command, completed.stderr.strip())


def run_service_command(argv: list[str]) -> bool:
    """Handle pywin32 service commands. Returns True when a service command was handled."""
    service_commands = {"install", "update", "remove", "start", "stop", "restart", "debug"}
    if not argv or argv[0].lower() not in service_commands:
        return False
    if win32serviceutil is None:
        raise RuntimeError(
            "Windows Service commands require pywin32. Install it with: python -m pip install pywin32"
        )

    win32serviceutil.HandleCommandLine(WaveceptionService, argv=[sys.argv[0], *argv])
    if argv[0].lower() in {"install", "update"}:
        set_service_recovery()
    return True


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Waveception worker/config/service entry point.")
    parser.add_argument("--config", action="store_true", help="Open the basic configuration window.")
    parser.add_argument("--console", action="store_true", help="Run the worker in the current terminal.")
    return parser.parse_args(argv)


def main() -> None:
    if run_service_command(sys.argv[1:]):
        return
    args = parse_args(sys.argv[1:])
    if args.config:
        open_config_window()
        return
    # Default to console mode so double-click/terminal use is still straightforward.
    run_worker(service_mode=False)


if __name__ == "__main__":
    main()


