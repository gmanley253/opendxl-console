from __future__ import absolute_import
import logging
import threading

import time
import datetime
import pkg_resources

from dxlclient.client import DxlClient
from dxlclient.client_config import DxlClientConfig
from dxlclient.callbacks import EventCallback
from dxlclient.message import Request, Message
from dxlbootstrap.util import MessageUtils
from tornado.ioloop import IOLoop

from .services_handler import ServiceUpdateHandler
from .subscriptions_handler import SubscriptionsHandler
from .messages_handler import MessagesHandler
from .send_message_handler import SendMessageHandler
from .websocket_handler import ConsoleWebSocketHandler

from dxlconsole.module import Module

# Configure local logger
logger = logging.getLogger(__name__)


class MonitorModule(Module):
    # Request topic for service registry queries
    SERVICE_REGISTRY_QUERY_TOPIC = '/mcafee/service/dxl/svcregistry/query'

    # Event topics for service registry changes
    SERVICE_REGISTRY_REGISTER_EVENT_TOPIC = '/mcafee/event/dxl/svcregistry/register'
    SERVICE_REGISTRY_UNREGISTER_EVENT_TOPIC = '/mcafee/event/dxl/svcregistry/unregister'

    # How often(in seconds) to refresh the service list
    SERVICE_UPDATE_INTERVAL = 60

    # How long to retain clients without any keep alive before evicting them
    CLIENT_RETENTION_MINUTES = 30

    # A default SmartClient JSON response to show no results
    NO_RESULT_JSON = u"""{response:{status:0,startRow:0,endRow:0,totalRows:0,data:[]}}"""

    # Locks for the different dictionaries shared between Monitor Handlers
    _service_dict_lock = threading.Lock()
    _client_dict_lock = threading.Lock()
    _web_socket_dict_lock = threading.Lock()
    _pending_messages_lock = threading.Lock()

    def __init__(self, app):
        super(MonitorModule, self).__init__(
            app, "monitor", "Fabric Monitor", "/public/images/monitor.png", "monitor_layout")

        # dictionary to store DXL Client instances unique to each "session"
        self._client_dict = {}

        # dictionary to store web sockets for each "session"
        self._web_socket_dict = {}

        # dictionary to store incoming messages for each "session"
        self._pending_messages = {}

        # dictionary to cache service state
        self._services = {}

        self._message_id_topics = {}

        self._client_config = DxlClientConfig.create_dxl_config_from_file(self.app.bootstrap_app.client_config_path)

        # DXL Client to perform operations that are the same for all users(svc registry queries)
        self._dxl_service_client = DxlClient(self._client_config)
        self._dxl_service_client.connect()

        self._dxl_service_client.add_event_callback(
            MonitorModule.SERVICE_REGISTRY_REGISTER_EVENT_TOPIC, _ServiceEventCallback(self))
        self._dxl_service_client.add_event_callback(
            MonitorModule.SERVICE_REGISTRY_UNREGISTER_EVENT_TOPIC, _ServiceEventCallback(self))

        self._refresh_all_services()

        self._service_updater_thread = threading.Thread(target=self._service_updater)
        self._service_updater_thread.daemon = True
        self._service_updater_thread.start()

        self._dxl_client_cleanup_thread = threading.Thread(target=self._cleanup_dxl_clients)
        self._dxl_client_cleanup_thread.daemon = True
        self._dxl_client_cleanup_thread.start()

    @property
    def handlers(self):
        return [
            (r'/update_services', ServiceUpdateHandler, dict(module=self)),
            (r'/subscriptions', SubscriptionsHandler, dict(module=self)),
            (r'/messages', MessagesHandler, dict(module=self)),
            (r'/send_message', SendMessageHandler, dict(module=self)),
            (r'/websocket', ConsoleWebSocketHandler, dict(module=self))
        ]

    @property
    def content(self):
        content = pkg_resources.resource_string(__name__, "content.html")
        return content.replace("@PORT@", str(self.app.bootstrap_app.port))

    @property
    def services(self):
        with self._service_dict_lock:
            return self._services.copy()

    @property
    def message_id_topics(self):
        return self._message_id_topics

    @property
    def client_config(self):
        return DxlClientConfig.create_dxl_config_from_file(self.app.bootstrap_app.client_config_path)

    def get_dxl_client(self, client_id):
        """
        Retrieves the DxlClient for the given request. If there is not one associated with
        the incoming request it creates a new one and saves the generated client_id as a cookie

        :param client_id: The client identifier
        :return: the DxlClient specific to this "session"
        """
        if not self._client_exists_for_connection(client_id):
            self._create_client_for_connection(client_id)

        with self._client_dict_lock:
            client = self._client_dict[client_id][0]

        if not client.connected:
            client.connect()

        logger.debug("Returning DXL client for id: " + str(client_id))
        return client

    def _create_client_for_connection(self, client_id):
        """
        Creates a DxlClient and stores it for the give client_id

        :param client_id: the client_id for the DxlClient
        """
        client = DxlClient(self.client_config)
        client.connect()
        logger.info("Initializing new dxl client for client_id: " + str(client_id))
        with self._client_dict_lock:
            self._client_dict[client_id] = (client, datetime.datetime.now())

    def _client_exists_for_connection(self, client_id):
        """
        Checks if there is already an existing DxlClient for the given client_id.

        :param client_id: the ID of the DxlClient to check for
        :return: whether there is an existing DxlClient for this ID
        """
        with self._client_dict_lock:
            return client_id in self._client_dict

    @staticmethod
    def create_smartclient_response_wrapper():
        """
        Creates a wrapper object containing the standard fields required by SmartClient responses

        :return: an initial SmartClient response wrapper
        """
        response_wrapper = {"response": {}}
        response = response_wrapper["response"]
        response["status"] = 0
        response["startRow"] = 0
        response["endRow"] = 0
        response["totalRows"] = 0
        response["data"] = []
        return response_wrapper

    def create_smartclient_error_response(self, error_message):
        """
        Creates an error response for the SmartClient UI with the given message

        :param error_message: The error message
        :return: The SmartClient response in dict form
        """
        response_wrapper = self.create_smartclient_response_wrapper()
        response = response_wrapper["response"]
        response["status"] = -1
        response["data"] = error_message
        return response

    def queue_message(self, message, client_id):
        """
        Adds the given message to the pending messages queue for the give client.

        :param message: the message to enqueue
        :param client_id: the client the message is intended for
        """
        with self._pending_messages_lock:
            if client_id not in self._pending_messages:
                self._pending_messages[client_id] = []

            self._pending_messages[client_id].append(message)

    def get_messages(self, client_id):
        """
        Retrieves the messages pending for the given client. This does not clear the queue after retrieving.

        :param client_id: the client to retrieve messages for
        :return: a List of messages for the client
        """
        with self._pending_messages_lock:
            if client_id in self._pending_messages:
                return self._pending_messages[client_id]
        return None

    def clear_messages(self, client_id):
        """
        Clears the pending messages for the given client.

        :param client_id: the client to clear messages for
        """
        with self._pending_messages_lock:
            self._pending_messages[client_id] = []

    def _service_updater(self):
        """
        A thread target that will run forever and do a complete refresh of the service list on an interval
        or if the DXL client reconnects
        """
        while True:
            with self._dxl_service_client._connected_lock:
                self._dxl_service_client._connected_wait_condition.wait(self.SERVICE_UPDATE_INTERVAL)

            if self._dxl_service_client.connected:
                logger.debug("Refreshing service list.")
                self._refresh_all_services()

    def _cleanup_dxl_clients(self):
        """
        A thread target that will run forever and evict DXL clients if their clients have not sent a keep-alive
        """
        logger.debug("DXL client cleanup thread initialized.")
        while True:
            with self._client_dict_lock:
                for key in list(self._client_dict):
                    if self._client_dict[key][1] < \
                            (datetime.datetime.now() - datetime.timedelta(minutes=self.CLIENT_RETENTION_MINUTES)):
                        logger.debug("Evicting DXL client for client_id: " + key)
                        del self._client_dict[key]

            time.sleep(5)

    def _refresh_all_services(self):
        """
        Queries the broker for the service list and replaces the currently stored one with the new
        results. Notifies all connected web sockets that new services are available.
        """
        req = Request(MonitorModule.SERVICE_REGISTRY_QUERY_TOPIC)

        req.payload = "{}"
        # Send the request
        dxl_response = self._dxl_service_client.sync_request(req, 5)
        dxl_response_dict = MessageUtils.json_payload_to_dict(dxl_response)
        logger.info("Service registry response: " + str(dxl_response_dict))

        with self._service_dict_lock:
            self._services = {}
            for service_guid in dxl_response_dict["services"]:
                self._services[service_guid] = dxl_response_dict["services"][service_guid]

        self.notify_web_sockets()

    def update_service(self, service_event):
        """
        Replaces a stored service data withe the one from the provided DXL service event

        :param service_event: the  DXL service event containing the service
        """
        with self._service_dict_lock:
            self._services[service_event['serviceGuid']] = service_event

    def remove_service(self, service_event):
        """
        Removes a stored service using the provided DXL service event

        :param service_event: the DXL service event containing the service to be removed
        """
        with self._service_dict_lock:
            if service_event['serviceGuid'] in self._services:
                del self._services[service_event['serviceGuid']]

    def client_keep_alive(self, message, client_id):
        logger.debug("Client keep-alive received for client id: " + client_id)
        if self._client_exists_for_connection(client_id):
            with self._client_dict_lock:
                self._client_dict[client_id] = (self._client_dict[client_id][0], datetime.datetime.now())

    @property
    def io_loop(self):
        """
        Returns the Tornado IOLoop that the web console uses

        :return: The Tornado IOLoop instance
        """
        return self.app.io_loop

    def add_web_socket(self, client_id, web_socket):
        """
        Stores a web socket associated with the given client id

        :param client_id: the client id key the web socket to
        :param web_socket:  the web socket to store
        """
        logger.debug("Adding web socket for client: " + client_id)
        with self._web_socket_dict_lock:
            self._web_socket_dict[client_id] = web_socket

    def remove_web_socket(self, client_id):
        """
        Removes any web socket associated with the given client_id

        :param client_id: The client ID
        """
        logger.debug("Removing web socket for client: " + client_id)
        with self._web_socket_dict_lock:
            self._web_socket_dict.pop(client_id, None)

    def notify_web_sockets(self):
        """
        Notifies all web sockets that there are pending service updates
        """
        with self._web_socket_dict_lock:
            for key in self._web_socket_dict:
                try:
                    self.io_loop.add_callback(
                        self._web_socket_dict[key].write_message,
                        u"serviceUpdates")
                except:
                    pass

    def get_message_topic(self, message):
        """
        Determines the topic for the provided message. Replaces the response channel in
        responses with the topic of the original request

        :param message: The DXL message
        :return:  The topic to use
        """
        if (message.message_type == Message.MESSAGE_TYPE_RESPONSE
            or message.message_type == Message.MESSAGE_TYPE_ERROR) and \
                message.request_message_id in self.message_id_topics:
            topic = self.message_id_topics[message.request_message_id]
            del self.message_id_topics[message.request_message_id]
        else:
            topic = message.destination_topic
        return topic


class _ServiceEventCallback(EventCallback):
    """
    A DXL event callback to handle service change events(register and unregister)
    """

    def __init__(self, module):
        super(_ServiceEventCallback, self).__init__()
        self._module = module

    def on_event(self, event):
        """
        Notifies all clients that there are changes to the service registry

        :param event: the incoming event
        """
        service_event = MessageUtils.json_payload_to_dict(event)
        logger.info("Received service registry event: " + str(service_event))
        if event.destination_topic == MonitorModule.SERVICE_REGISTRY_REGISTER_EVENT_TOPIC:
            self._module.update_service(service_event)
        elif event.destination_topic == MonitorModule.SERVICE_REGISTRY_UNREGISTER_EVENT_TOPIC:
            self._module.remove_service(service_event)

        self._module.notify_web_sockets()
