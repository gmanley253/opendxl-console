from __future__ import absolute_import
import pkg_resources

import json
import logging
import tornado
import tornado.httputil
import traceback

from dxlclient.message import Request, Message
from dxlbootstrap.util import MessageUtils
from dxlconsole.handlers import BaseRequestHandler
from dxlconsole.module import Module

# Configure local logger
from dxlconsole.modules.monitor.module import MonitorModule

logger = logging.getLogger(__name__)


class BrokerModule(Module):
    """
    Module that provides information about the OpenDXL broker that is being managed
    """

    # Request topic for Broker registry queries
    BROKER_REGISTRY_QUERY_TOPIC = '/mcafee/service/dxl/brokerregistry/query'
    # Request topic for Broker health info
    BROKER_HEALTH_TOPIC = '/mcafee/service/dxl/broker/health'

    def __init__(self, app):
        """
        Constructor parameters:

        :param app: The application that the module is a part of
        """
        super(BrokerModule, self).__init__(
            app, "broker", "Broker Details", "/public/images/broker.png", "broker_layout")

    @property
    def content(self):
        """
        The content of the module (JS code)

        :return: The content of the module (JS code)
        """
        return pkg_resources.resource_string(__name__, "content.html")

    @property
    def enabled(self):
        """
        Returns whether the module is enabled

        :return: Whether the module is enabled
        """
        return self.app.bootstrap_app.local_broker

    @property
    def handlers(self):
        """
        Web (Tornado) handlers for the module

        :return: The web (Tornado) handlers for the module
        """
        return [
            (r'/broker_info', BrokerInfoHandler, dict(module=self))
        ]


class BrokerInfoHandler(BaseRequestHandler):
    """
    Handles post requests to get broker information
    """

    def __init__(self, application, request, module):
        """
        Constructor parameters:

        :param application: The application associated with the request handler
        :param request: The request
        :param module: The module this request handler is associated with
        """
        super(BrokerInfoHandler, self).__init__(application, request)
        self._module = module
        self._bootstrap_app = application.bootstrap_app

    def data_received(self, chunk):
        pass

    @tornado.web.authenticated
    def get(self):
        """
        Sends requests to get broker information (health and the broker registry topic)
        """
        try:
            response_wrapper = MonitorModule.create_smartclient_response_wrapper()
            # build the the response data
            response = response_wrapper["response"]
            # the dxlclient for retrieving broker info
            dxlclient = self._bootstrap_app.client

            req = Request(BrokerModule.BROKER_REGISTRY_QUERY_TOPIC)
            # targeting the connected broker
            MessageUtils.dict_to_json_payload(req, {})

            # Send the broker registry request
            dxl_response = dxlclient.sync_request(req, 5)
            if dxl_response.message_type != Message.MESSAGE_TYPE_ERROR:
                dxl_response_dict = MessageUtils.json_payload_to_dict(dxl_response)
            else:
                err_msg = "Error invoking service with topic '{0}': {1} ({2})".format(
                    BrokerModule.BROKER_REGISTRY_QUERY_TOPIC, dxl_response.error_message, dxl_response.error_code)
                raise Exception(err_msg)

            brokerinfo = list(dxl_response_dict['brokers'].values())[0]
            # Send the broker health request
            req = Request(BrokerModule.BROKER_HEALTH_TOPIC)
            # targeting the connected broker
            MessageUtils.dict_to_json_payload(req, {})
            dxl_response = dxlclient.sync_request(req, 15)

            if dxl_response.message_type != Message.MESSAGE_TYPE_ERROR:
                dxl_response_dict = MessageUtils.json_payload_to_dict(dxl_response)
            else:
                err_msg = "Error invoking service with topic '{0}': {1} ({2})".format(
                    BrokerModule.BROKER_HEALTH_TOPIC, dxl_response.error_message, dxl_response.error_code)
                raise Exception(err_msg)

            entry = {
                "version": brokerinfo['version'],
                "guid": brokerinfo['guid'],
                "connectedClients": dxl_response_dict['connectedClients'],
                "localServiceCounter": dxl_response_dict['localServiceCounter'],
                "incomingMessages": dxl_response_dict['incomingMessages'],
                "outgoingMessages": dxl_response_dict['outgoingMessages'],
                "startTime": dxl_response_dict['startTime']
            }
            response["data"].append(entry)
            response['totalRows'] += 1

            self.write(json.dumps(response_wrapper))

        except Exception as e:
            logger.error("Exception while processing broker info request." + str(e))
            logger.error(traceback.format_exc())
            self.set_status(500)
            self.write(u"""{response:{status:0,startRow:0,endRow:0,totalRows:0,data:[]}}""")