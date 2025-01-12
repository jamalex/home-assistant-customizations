import attridict
import copy
import json
import os
import slugify
import time
import threading
from websocket import (
    create_connection,
    WebSocketConnectionClosedException,
    WebSocketTimeoutException,
)

from service_utils import generate_function_code

HASS_TOKEN = os.environ["HASS_TOKEN"]
HASS_URL = f"wss://{os.environ['HASS_HOST']}/api/websocket"

# A map of registry names (used in event_type and config calls)
# to the ID field each record uses.
REGISTRIES = {
    "entity_registry": "entity_id",
    "device_registry": "id",
    "area_registry": "area_id",
    "label_registry": "id",
    "floor_registry": "floor_id",
    # etc., as needed
}


class HassClient:
    """
    The HassClient manages a WebSocket connection to Home Assistant, including
    automatic reconnection and message handling. A ping thread monitors
    connectivity and triggers reconnections as necessary, while a receiver
    thread continually reads incoming messages.
    """

    def __init__(self, url, token, verbose=False):
        self.url = url
        self.token = token
        self.verbose = verbose

        # Underlying WebSocket connection
        self._ws = None

        # Event used to stop threads
        self._stop_event = threading.Event()

        # Message ID tracking and callbacks
        self._current_id = 1
        self._callbacks = {}

        # Keepalive and ping/pong tracking
        self._last_ping_id = None
        self._ping_interval = 10
        self._last_ping_time = 0

        # Thread references
        self._receiver_thread = None
        self._ping_thread = None

        # Track whether authentication has completed so we don't send other messages too soon
        self._authenticated = False

        # Used to prevent automatic reconnect attempts when user explicitly stops
        self._manual_stop = False

        # Local store holds information about HA registries and states
        self.store = {registry_name: {} for registry_name in REGISTRIES}
        self.store["states"] = {}

    def connect(self):
        """
        Initializes the client by stopping any existing run,
        creating receiver and ping threads, and attempting an initial connection.
        """
        if self._ws:
            print("Already connected.")
            return

        # Clean up old threads if they were running
        self.stop()

        self._manual_stop = False
        self._stop_event.clear()

        # Attempt to establish the initial connection.
        try:
            self._establish_ws()
            # self._post_auth_init()
        except Exception as e:
            print(f"Initial connect failed: {e}")
            self._close_ws()
            self._ws = None

        # Ensure the receiver thread is running
        if not self._receiver_thread or not self._receiver_thread.is_alive():
            self._receiver_thread = threading.Thread(
                target=self._receiver_loop, name="receiver", daemon=True
            )
            self._receiver_thread.start()

        # Ensure the ping thread is running
        if not self._ping_thread or not self._ping_thread.is_alive():
            self._ping_thread = threading.Thread(
                target=self._ping_loop, name="pinger", daemon=True
            )
            self._ping_thread.start()

    def _establish_ws(self):
        """
        Opens a new WebSocket connection to the configured Home Assistant URL.
        Raises an exception if unable to connect.
        """
        print(f"Connecting to {self.url}...")
        self._authenticated = False
        self._ws = create_connection(self.url, timeout=10)

    def _close_ws(self):
        """
        Safely closes the WebSocket if it exists, then sets the reference to None.
        """
        if self._ws:
            try:
                self._ws.close()
            except:
                pass
        self._ws = None
        self._authenticated = False

    def _receiver_loop(self):
        """
        Continually reads messages from the WebSocket, if available.
        If the connection is not open or a read error occurs, it
        waits briefly and tries again. The ping thread handles all
        reconnection logic.
        """
        while not self._stop_event.is_set():
            if not self._ws:
                # If there's no socket, wait briefly and loop again
                time.sleep(1)
                continue

            try:
                msg = self._ws.recv()
                if not msg:
                    continue
            except WebSocketTimeoutException:
                # A timeout is not critical; just continue receiving
                continue
            except WebSocketConnectionClosedException:
                print("Receiver: WebSocket connection closed.")
                self._ws = None
                continue
            except Exception as e:
                print(f"Receiver loop error: {e}")
                self._ws = None
                continue

            self._handle_message(msg)

        print("Receiver thread exiting...")

    def _ping_loop(self):
        """
        Periodically checks connectivity and sends ping messages.
        If a ping goes unacknowledged or if the WebSocket is unavailable,
        this loop immediately attempts reconnection (with optional backoff).
        """
        while not self._stop_event.is_set():
            if not self._ws:
                # If no connection, try reconnecting immediately
                print("[Ping] _ws is None, reconnecting proactively...")
                self._attempt_reconnect()
            else:
                # Check if it's time to ping
                now = time.time()
                if (
                    now - self._last_ping_time >= self._ping_interval
                    and self._authenticated
                ):
                    self._last_ping_time = now

                    # If we had a ping waiting for a pong, reconnect
                    if self._last_ping_id is not None:
                        print("[Ping] Previous ping unanswered, forcing reconnect...")
                        self._attempt_reconnect()
                    else:
                        # Send a new ping
                        self._last_ping_id = self.send_message({"type": "ping"})

            time.sleep(1)

        print("Ping thread exiting...")

    def _attempt_reconnect(self):
        """
        Closes any existing WebSocket, then tries to re-establish it.
        If it fails, _ws remains None so the ping thread can try again later.
        """
        self._close_ws()

        if self._manual_stop:
            return

        # Attempt to reconnect after a brief delay for good measure
        time.sleep(1)
        try:
            self._establish_ws()
            # self._post_auth_init()
            print("[Ping] Reconnected successfully.")
        except Exception as e:
            print(f"[Ping] Reconnection failed: {e}")
            # Close again to ensure no partial connection remains
            self._close_ws()
            self._ws = None
            return

        # If successful, reset the ping ID
        self._last_ping_id = None

    def _handle_message(self, raw_msg):
        """
        Processes a raw JSON message from Home Assistant.
        Routes to the appropriate handler based on the message type.
        """
        try:
            data = json.loads(raw_msg)
        except ValueError:
            print(f"Non-JSON message: {raw_msg}")
            return

        msg_type = data.get("type")
        msg_id = data.get("id")

        if msg_type == "auth_required":
            print("Server requested auth, sending token.")
            self._send_auth()
            return
        elif msg_type == "auth_ok":
            print("Authenticated successfully.")
            self._post_auth_init()
            return
        elif msg_type == "auth_invalid":
            print(f"Authentication invalid: {data}")
            self.stop()
            return

        if msg_type == "pong":
            # Acknowledge the pong for our last ping
            if msg_id in self._callbacks:
                cb = self._callbacks.pop(msg_id, None)
                if cb:
                    cb(data)
            self._last_ping_id = None
            return

        if msg_type == "result":
            # Possibly handle success/failure for a request
            success = data.get("success", False)
            if success and msg_id in self._callbacks:
                cb = self._callbacks.pop(msg_id)
                cb(data)
            return

        if msg_type == "event":
            self._handle_event(data)
            return

        # If there's a callback for this ID, call it
        if msg_id in self._callbacks:
            cb = self._callbacks.pop(msg_id, None)
            if cb:
                cb(data)
        else:
            print(f"Unhandled message: {data}")

    def _send_auth(self):
        """
        Sends Home Assistant authentication message using the stored token.
        """
        if self._ws:
            self._authenticated = False
            if self.verbose:
                print("Sending auth message with token...")
            msg = {"type": "auth", "access_token": self.token}
            self._ws.send(json.dumps(msg))

    def _post_auth_init(self):
        """
        Subscribes to events and fetches initial data after a successful authentication.
        This method is called both after the first connection and any reconnection.
        """

        self._authenticated = True

        # Subscribe to all events
        self.send_message({"type": "subscribe_events"})

        # Fetch data from each known registry
        for registry_name in REGISTRIES:
            self.refresh_registry(registry_name)

        # Fetch the initial set of entity states
        self._get_states()

        # Get the list of services
        self.send_message({"type": "get_services"}, callback=lambda msg: self.store.update({"services": msg.get("result", {})}))

    def _handle_event(self, data):
        """
        Handles event messages from Home Assistant. This may include
        registry updates and state_changed events.
        """
        event_obj = data.get("event", {})
        event_type = event_obj.get("event_type")

        if event_type and event_type.endswith("_registry_updated"):
            # For example: "entity_registry_updated" => "entity_registry"
            registry_name = event_type.replace("_updated", "")
            if registry_name in REGISTRIES:
                print(f"{registry_name} changed -> refreshing full list.")
                self.refresh_registry(registry_name)
                if registry_name == "entity_registry":
                    # Also refresh states if the entity registry was updated
                    self._get_states()
        elif event_type == "state_changed":
            self._handle_state_changed_event(event_obj)
        elif event_type in (
            "recorder_5min_statistics_generated",
            "recorder_hourly_statistics_generated",
        ):
            # Ignore these events
            return
        else:
            print(f"Unhandled event: {data}")

    def _handle_state_changed_event(self, event_obj):
        """
        Updates our local store of entity states when a state_changed event occurs.
        """
        data = event_obj.get("data", {})
        entity_id = data.get("entity_id")
        if not entity_id:
            return

        new_state = data.get("new_state")
        if new_state is not None:
            self.store["states"][entity_id] = new_state

    def _get_states(self):
        """
        Requests a full list of states from Home Assistant and stores them locally.
        """
        self.send_message({"type": "get_states"}, callback=self._handle_get_states)

    def _handle_get_states(self, msg):
        """
        Called once after requesting get_states. The result should be a list
        of state objects, each typically having an 'entity_id' key.
        """
        result = msg.get("result", [])
        new_states = {}
        for st in result:
            entity_id = st.get("entity_id")
            if entity_id:
                new_states[entity_id] = st
        self.store["states"] = new_states

    def refresh_registry(self, registry_name):
        """
        Sends a request to retrieve the full list of items in a specific registry,
        and updates our local store upon completion.
        """

        def on_list_result(msg):
            if not msg.get("result"):
                return
            items = msg["result"]
            id_field = REGISTRIES[registry_name]
            new_data = {}
            for item in items:
                rec_id = item.get(id_field)
                if rec_id:
                    new_data[rec_id] = item
            self.store[registry_name] = new_data

        self.send_message(
            {"type": f"config/{registry_name}/list"}, callback=on_list_result
        )

    def update_registry(self, registry, callback=None, **kwargs):
        """
        Sends an update request for the specified registry. When the update
        completes, refreshes the registry so local data remains in sync.
        """

        def on_update_result(msg):
            self.refresh_registry(registry)
            if callback:
                callback(msg)

        return self.send_message(
            {"type": f"config/{registry}/update", **kwargs}, callback=on_update_result
        )

    def send_message(self, payload, callback=None):
        """
        Sends a JSON message to the server. Each message is assigned a unique ID.
        An optional callback is triggered once a response is received.
        """
        if not self._ws:
            raise RuntimeError("Not connected, can't send message.")

        if not self._authenticated:
            raise RuntimeError("Not authenticated, can't send message.")

        msg_id = self._current_id
        self._current_id += 1
        payload["id"] = msg_id

        if callback:
            self._callbacks[msg_id] = callback

        if self.verbose:
            print(f"Sending message: {payload}")

        self._ws.send(json.dumps(payload))
        return msg_id

    def stop(self):
        """
        Signals all running threads to stop and closes the WebSocket.
        Used to gracefully shut down the client.
        """
        self._manual_stop = True
        self._stop_event.set()
        self._authenticated = False
        self._close_ws()

        # Join receiver thread
        if (
            self._receiver_thread
            and self._receiver_thread != threading.current_thread()
        ):
            self._receiver_thread.join()
            self._receiver_thread = None

        # Join ping thread
        if self._ping_thread and self._ping_thread != threading.current_thread():
            self._ping_thread.join()
            self._ping_thread = None

    def __enter__(self):
        """
        Provides a context manager interface. This is optional, but convenient.
        """
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    def get_entity(self, entity_id):
        return Entity(entity_id, self)

    def get_device(self, device_id):
        return Device(device_id, self)

    def get_entities(self, include_disabled=False):
        return [Entity(eid, self) for eid in self.store["entity_registry"] if (include_disabled or self.store["entity_registry"][eid].get("disabled_by") is None)]

    @property
    def entities(self):
        return Domains(self)


class Domains(list):

    def __init__(self, client, device_id=None):
        self.client = client
        self.device_id = device_id
        self.extend(
            set(
                [
                    entity_id.split(".")[0]
                    for entity_id in self.client.store["entity_registry"].keys()
                    if (
                        device_id is None
                        or self.client.store["entity_registry"][entity_id].get(
                            "device_id"
                        )
                        == device_id
                    )
                    and (
                        (
                            self.client.store["entity_registry"][entity_id].get(
                                "disabled_by"
                            )
                            is None
                        )
                    )
                ]
            )
        )

    def __getattr__(self, domain):
        return Entities(self.client, domain, device_id=self.device_id)

    def __getitem__(self, domain):
        return self.__getattr__(domain)

    def __dir__(self):
        return [d for d in self]


class Entities(list):

    def __init__(self, client, domain, device_id=None):
        self.client = client
        self.domain = domain
        self.device_id = device_id
        self.extend(
            [
                Entity(eid, self.client)
                for eid in self.client.store["entity_registry"].keys()
                if eid.startswith(f"{domain}.")
                and (
                    device_id is None
                    or self.client.store["entity_registry"][eid].get("device_id")
                    == device_id
                ) and (self.client.store["entity_registry"][eid].get("disabled_by") is None)
            ]
        )

    def __getattr__(self, object_id):
        entities = [e for e in self if e._object_id == object_id]
        if len(entities) == 1:
            return entities[0]
        else:
            raise AttributeError(
                f"No entity with object_id '{object_id}' found in domain '{self.domain}'"
            )

    def __getitem__(self, object_id):
        return self.__getattr__(object_id)
    
    def __dir__(self):
        return [e._object_id for e in self]


class Entity:

    def __init__(self, entity_id, client):
        self._entity_id = entity_id
        self._domain, self._object_id = entity_id.split(".")
        self.client = client

    @property
    def registry(self):
        return attridict(
            copy.deepcopy(self.client.store["entity_registry"].get(self.entity_id))
        )

    @property
    def state(self):
        state = self.client.store["states"].get(self.entity_id)
        if state is None:
            return None
        return attridict(copy.deepcopy(state))

    def get_name(self):
        return self.state.get("attributes", {}).get("friendly_name", "")

    def set_name(self, value):
        self.update_registry(name=value)

    name = property(get_name, set_name)

    def get_names(self):
        return {
            "entity_id": self.entity_id,
            "registry__name": self.registry.get("name", ""),
            "registry__original_name": self.registry.get("original_name", ""),
            "state__attributes__friendly_name": self.state.get("attributes", {}).get("friendly_name", ""),
            "device__name": self.device.name,
        }

    def get_entity_id(self):
        return self._entity_id

    def set_entity_id(self, new_entity_id):
        if "." not in new_entity_id:
            new_entity_id = f"{self._domain}.{new_entity_id}"
        self.update_registry(new_entity_id=new_entity_id)
        self._entity_id = new_entity_id
        self._object_id = self.entity_id.split(".")[1]

    entity_id = property(get_entity_id, set_entity_id)

    def get_slugified_name(self):
        return slugify.slugify(
            self.name.replace("'s", "").replace("'", ""), separator="_"
        )

    def update_registry(self, callback=None, **kwargs):
        self.client.update_registry(
            "entity_registry", entity_id=self.entity_id, callback=callback, **kwargs
        )

    def enable(self):
        self.update_registry(disabled_by=None)

    def disable(self):
        self.update_registry(disabled_by="user")

    enabled = property(
        lambda self: self.registry.get("disabled_by") is None,
        lambda self, value: self.enable() if value else self.disable(),
    )

    def turn_on(self, **kwargs):
        self.call_service("turn_on", **kwargs)

    def turn_off(self, **kwargs):
        self.call_service("turn_off", **kwargs)

    def open_cover(self, **kwargs):
        self.call_service("open_cover", **kwargs)

    def close_cover(self, **kwargs):
        self.call_service("close_cover", **kwargs)

    def call_service(self, service, **kwargs):
        self.client.send_message(
            {
                "type": "call_service",
                "domain": self._domain,
                "service": service,
                "service_data": {"entity_id": self.entity_id, **kwargs},
            }
        )

    def __str__(self):
        parenthetical = self.name if self.enabled else "disabled"
        return f"{self.entity_id} ({parenthetical})"

    def __repr__(self):
        return f"<Entity {str(self)}>"

    @property
    def device(self):
        return Device(self.registry.get("device_id"), self.client)

    @property
    def services(self):
        return Services(self)


class Services:

    def __init__(self, entity):
        self.entity = entity
        self.service_defs = entity.client.store["services"].get(entity._domain)

    def __getattr__(self, service):
        service_def = self.service_defs.get(service)
        code = generate_function_code(self.entity._domain, service, service_def)
        exec(code)
        service_fn = locals()["wrapper_fn"](self.entity)
        return service_fn

    def __dir__(self):
        return [s for s in self.service_defs]


class Device:

    def __init__(self, device_id, client):
        self._device_id = device_id
        self.client = client

    @property
    def device_id(self):
        return self._device_id

    @property
    def registry(self):
        return attridict(
            copy.deepcopy(self.client.store["device_registry"].get(self.device_id))
        )

    def get_name(self):
        return self.registry.get("name_by_user", "") or self.registry.get("name", "")

    def set_name(self, value):
        self.update_registry(name_by_user=value)

    name = property(get_name, set_name)

    def update_registry(self, callback=None, **kwargs):
        self.client.update_registry(
            "device_registry", device_id=self.device_id, callback=callback, **kwargs
        )

    def __str__(self):
        return f"{self.device_id} ({self.name})"

    def __repr__(self):
        return f"<Device {str(self)}>"

    @property
    def entities(self):
        return Domains(self.client, device_id=self.device_id)

    def get_entities(self, include_disabled=False):
        return [
            Entity(eid, self.client)
            for eid, e in self.client.store["entity_registry"].items()
            if e.get("device_id") == self.device_id
            and (include_disabled or e.get("disabled_by") is None)
        ]


if __name__ == "__main__":
    # Example usage:
    client = HassClient(HASS_URL, HASS_TOKEN)
    client.connect()

    # ... Do any desired interactions, e.g.:
    # entity = client.get_entity("light.living_room")
    # entity.turn_on()

    # Stop the client when done
    # client.stop()
