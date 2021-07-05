#!/usr/bin/env python3

import asyncio
import json
import os
import time
from datetime import datetime, timedelta

from typing import Dict

import hbmqtt
import websockets
from hbmqtt.broker import Broker

from hbmqtt.client import MQTTClient, ConnectException
from hbmqtt.mqtt.constants import QOS_0, QOS_1, QOS_2
import pkg_resources
import time
import bumper
from bumper.util import get_logger
import json
from datetime import datetime, timedelta
from passlib.apps import custom_app_context as pwd_context
import ssl
import tempfile

from urllib.parse import urlparse, urlunparse
from hbmqtt.mqtt.protocol.client_handler import ClientProtocolHandler
from hbmqtt.adapters import StreamReaderAdapter, StreamWriterAdapter, WebSocketsReader, WebSocketsWriter
from websockets.uri import InvalidURI
from websockets.exceptions import InvalidHandshake
from hbmqtt.mqtt.protocol.handler import ProtocolHandlerException
from hbmqtt.mqtt.connack import CONNECTION_ACCEPTED

mqttserverlog = get_logger("mqttserver")
helperbotlog = get_logger("helperbot")
boterrorlog = get_logger("boterror")
proxymodelog = get_logger("proxymode")


class MQTTHelperBot:
    Client = None
    wait_resp_timeout_seconds = 60

    def __init__(self, address):
        self.address = address
        self.client_id = "helperbot@bumper/helperbot"
        self.command_responses = []

    async def start_helper_bot(self):
        try:
            if self.Client is None:
                self.Client = MQTTClient(
                    client_id=self.client_id, config={"check_hostname": False, "reconnect_retries": 20}
                )

            await self.Client.connect(
                "mqtts://{}:{}/".format(self.address[0], self.address[1]),
                cafile=bumper.ca_cert,
            )
            await self.Client.subscribe(
                [
                    ("iot/#", QOS_0),
                ]
            )
        except Exception as e:
            helperbotlog.exception("{}".format(e))

    async def wait_for_resp(self, requestid):
        try:
            t_end = (
                    datetime.now() + timedelta(seconds=self.wait_resp_timeout_seconds)
            ).timestamp()

            while time.time() < t_end:
                await asyncio.sleep(0.1)
                if len(self.command_responses) > 0:
                    for msg in self.command_responses:
                        topic = str(msg["topic"]).split("/")
                        if topic[6] == "helperbot" and topic[10] == requestid:
                            if topic[11] == "j":
                                resppayload = json.loads(msg["payload"])
                            else:
                                resppayload = str(msg["payload"])
                            resp = {"id": requestid, "ret": "ok", "resp": resppayload}
                            self.command_responses.remove(msg)
                            return resp
        except asyncio.CancelledError:
            helperbotlog.debug("wait_for_resp cancelled by asyncio")
        except Exception as e:
            helperbotlog.exception("{}".format(e))

        return {
            "id": requestid,
            "errno": 500,
            "ret": "fail",
            "debug": "wait for response timed out",
        }

    async def send_command(self, cmdjson, requestid):
        if not self.Client._handler.writer is None:
            try:
                ttopic = "iot/p2p/{}/helperbot/bumper/helperbot/{}/{}/{}/q/{}/{}".format(
                    cmdjson["cmdName"],
                    cmdjson["toId"],
                    cmdjson["toType"],
                    cmdjson["toRes"],
                    requestid,
                    cmdjson["payloadType"],
                )
                if cmdjson["payloadType"] == "x":
                    await self.Client.publish(
                        ttopic, str(cmdjson["payload"]).encode(), QOS_0
                    )
                elif cmdjson["payloadType"] == "j":
                    await self.Client.publish(
                        ttopic, json.dumps(cmdjson["payload"]).encode(), QOS_0
                    )

                resp = await self.wait_for_resp(requestid)
                return resp
            except Exception as e:
                helperbotlog.exception("{}".format(e))
                return {
                    "id": requestid,
                    "errno": 500,
                    "ret": "fail",
                    "debug": "exception occurred please check bumper logs",
                }


class MQTTServer:
    default_config = None
    broker = None

    async def broker_coro(self):

        mqttserverlog.info(
            "Starting MQTT Server at {}:{}".format(self.address[0], self.address[1])
        )

        try:
            await self.broker.start()

        except hbmqtt.broker.BrokerException as e:
            mqttserverlog.exception(e)
            # asyncio.create_task(bumper.shutdown())
            pass

        except Exception as e:
            mqttserverlog.exception("{}".format(e))
            # asyncio.create_task(bumper.shutdown())
            pass

    def __init__(self, address, **kwargs):
        try:
            self.address = address

            # Default config opts
            passwd_file = os.path.join(
                os.path.join(bumper.data_dir, "passwd")
            )  # For file auth, set user:hash in passwd file see (https://hbmqtt.readthedocs.io/en/latest/references/hbmqtt.html#configuration-example)

            allow_anon = False

            for key, value in kwargs.items():
                if key == "password_file":
                    passwd_file = kwargs["password_file"]

                elif key == "allow_anonymous":
                    allow_anon = kwargs["allow_anonymous"]  # Set to True to allow anonymous authentication

            # The below adds a plugin to the hbmqtt.broker.plugins without having to futz with setup.py
            distribution = pkg_resources.Distribution("hbmqtt.broker.plugins")
            bumper_plugin = pkg_resources.EntryPoint.parse(
                "bumper = bumper.mqttserver:BumperMQTTServer_Plugin", dist=distribution
            )
            distribution._ep_map = {"hbmqtt.broker.plugins": {"bumper": bumper_plugin}}
            pkg_resources.working_set.add(distribution)

            # Initialize bot server
            self.default_config = {
                "listeners": {
                    "default": {"type": "tcp"},
                    "tls1": {
                        "bind": "{}:{}".format(address[0], address[1]),
                        "ssl": "on",
                        "certfile": bumper.server_cert,
                        "keyfile": bumper.server_key,
                    },
                },
                "sys_interval": 0,
                "auth": {
                    "allow-anonymous": allow_anon,
                    "password-file": passwd_file,
                    "plugins": ["bumper"],  # Bumper plugin provides auth and handling of bots/clients connecting
                },
                "topic-check": {"enabled": False},
            }

            self.broker = hbmqtt.broker.Broker(config=self.default_config)

        except Exception as e:
            mqttserverlog.exception("{}".format(e))

class BumperProxyModeMQTTClient(MQTTClient):

    eco_helper_names: Dict[str, str] = {}

    async def _connect_coro(self): #Override default to ignore ssl verification
        kwargs = dict()

        # Decode URI attributes
        uri_attributes = urlparse(self.session.broker_uri)
        scheme = uri_attributes.scheme
        secure = True if scheme in ('mqtts', 'wss') else False
        self.session.username = self.session.username if self.session.username else uri_attributes.username
        self.session.password = self.session.password if self.session.password else uri_attributes.password
        self.session.remote_address = uri_attributes.hostname
        self.session.remote_port = uri_attributes.port
        if scheme in ('mqtt', 'mqtts') and not self.session.remote_port:
            self.session.remote_port = 8883 if scheme == 'mqtts' else 1883
        if scheme in ('ws', 'wss') and not self.session.remote_port:
            self.session.remote_port = 443 if scheme == 'wss' else 80
        if scheme in ('ws', 'wss'):
            # Rewrite URI to conform to https://tools.ietf.org/html/rfc6455#section-3
            uri = (scheme, self.session.remote_address + ":" + str(self.session.remote_port), uri_attributes[2],
                uri_attributes[3], uri_attributes[4], uri_attributes[5])
            self.session.broker_uri = urlunparse(uri)
        # Init protocol handler
        #if not self._handler:
        self._handler = ClientProtocolHandler(self.plugins_manager, loop=self._loop)

        if secure:
            sc = ssl.create_default_context(
                ssl.Purpose.SERVER_AUTH,
                cafile=self.session.cafile,
                capath=self.session.capath,
                cadata=self.session.cadata)
            if 'certfile' in self.config and 'keyfile' in self.config:
                sc.load_cert_chain(self.config['certfile'], self.config['keyfile'])
            if 'check_hostname' in self.config and isinstance(self.config['check_hostname'], bool):
                sc.check_hostname = self.config['check_hostname']

            sc.verify_mode = ssl.CERT_NONE #Ignore verify of cert
            kwargs['ssl'] = sc

        try:
            reader = None
            writer = None
            self._connected_state.clear()
            # Open connection
            if scheme in ('mqtt', 'mqtts'):
                conn_reader, conn_writer = \
                    await  asyncio.open_connection(
                        self.session.remote_address,
                        self.session.remote_port, loop=self._loop, **kwargs)
                reader = StreamReaderAdapter(conn_reader)
                writer = StreamWriterAdapter(conn_writer)
            elif scheme in ('ws', 'wss'):
                websocket = await websockets.connect(
                    self.session.broker_uri,
                    subprotocols=['mqtt'],
                    loop=self._loop,
                    extra_headers=self.extra_headers,
                    **kwargs)
                reader = WebSocketsReader(websocket)
                writer = WebSocketsWriter(websocket)
            # Start MQTT protocol
            self._handler.attach(self.session, reader, writer)
            return_code = await  self._handler.mqtt_connect()
            if return_code is not CONNECTION_ACCEPTED:
                self.session.transitions.disconnect()
                self.logger.warning("Connection rejected with code '%s'" % return_code)
                exc = ConnectException("Connection rejected by broker")
                exc.return_code = return_code
                raise exc
            else:
                # Handle MQTT protocol
                await  self._handler.start()
                self.session.transitions.connect()
                self._connected_state.set()
                self.logger.debug("connected to %s:%s" % (self.session.remote_address, self.session.remote_port))
            return return_code
        except InvalidURI as iuri:
            self.logger.warning("connection failed: invalid URI '%s'" % self.session.broker_uri)
            self.session.transitions.disconnect()
            raise ConnectException("connection failed: invalid URI '%s'" % self.session.broker_uri, iuri)
        except InvalidHandshake as ihs:
            self.logger.warning("connection failed: invalid websocket handshake")
            self.session.transitions.disconnect()
            raise ConnectException("connection failed: invalid websocket handshake", ihs)
        except (ProtocolHandlerException, ConnectionError, OSError) as e:
            self.logger.warning("MQTT connection failed: %r" % e)
            self.session.transitions.disconnect()
            raise ConnectException(e)

    async def get_msg(self):
        try:
            while self._connected_state._value:
                message = await self.deliver_message()
                msgdata = str(message.data.decode("utf-8"))

                proxymodelog.info(f"MQTT Proxy Client - Message Received From Ecovacs - Topic: {message.topic} - Message: {msgdata}")
                topic = message.topic
                ttopic = topic.split("/")
                if ttopic[1] == "p2p":
                    self.eco_helper_names[ttopic[10]] = ttopic[3]
                    ttopic[3] = "proxyhelper"
                    topic = "/".join(ttopic)
                    proxymodelog.info(f"MQTT Proxy Client - Converted Topic From {message.topic} TO {topic}")

                proxymodelog.info(
                    f"MQTT Proxy Client - Proxy Forward Message to Robot - Topic: {topic} - Message: {msgdata.encode()}")
                await bumper.mqtt_helperbot.Client.publish(
                    topic, msgdata.encode(), QOS_0
                )

        except Exception as e:
            proxymodelog.error(f"MQTT Proxy Client - get_msg Exception - {e}")

class BumperMQTTServer_Plugin:
    proxyclients: Dict[str, BumperProxyModeMQTTClient] = {}
    def __init__(self, context):
        self.context = context
        try:
            self.auth_config = self.context.config["auth"]
            self._users = dict()
            self._read_password_file()

        except KeyError:
            self.context.logger.warning(
                "'bumper' section not found in context configuration"
            )
        except Exception as e:
            mqttserverlog.exception("{}".format(e))



    async def authenticate(self, *args, **kwargs):
        authenticated = False

        try:
            session = kwargs.get("session", None)
            username = session.username
            password = session.password
            client_id = session.client_id

            if "@" in client_id:
                didsplit = str(client_id).split("@")
                if not (  # if ecouser or bumper aren't in details it is a bot
                        "ecouser" in didsplit[1] or "bumper" in didsplit[1]
                ):
                    tmpbotdetail = str(didsplit[1]).split("/")
                    bumper.bot_add(
                        username,
                        didsplit[0],
                        tmpbotdetail[0],
                        tmpbotdetail[1],
                        "eco-ng",
                    )
                    mqttserverlog.info(f"Bumper Authentication Success - Bot - SN: {username} - DID: {didsplit[0]}"
                                       f" - Class: {tmpbotdetail[0]}")
                    authenticated = True

                    if authenticated and bumper.bumper_proxy_mode:
                        mqtt_server = bumper.config_proxyMode_getServerIP("mqtt_server","")
                        if mqtt_server:
                            proxymodelog.info(f"MQTT Proxy Mode - Using server {mqtt_server}")
                        else:
                            proxymodelog.error(f"MQTT Proxy Mode - No server found! Load defaults or set mqtt_server in config_proxymode table!")
                            proxymodelog.exception(f"MQTT Proxy Mode - Exiting due to no MQTT Server configured!")
                            exit(1)

                        proxymodelog.info(f"MQTT Proxy Mode - Proxy Bot to MQTT - Client_id: {client_id} - Username: {username}")

                        self.proxyclients[client_id] = BumperProxyModeMQTTClient(
                            client_id=client_id, config={"check_hostname": False}
                        )

                        try:
                            await self.proxyclients[client_id].connect(
                                f"mqtts://{username}:{password}@{mqtt_server}:443",
                            )
                        except Exception as e:
                            mqttserverlog.error(f"MQTT Proxy Mode - Exception connecting with proxy to ecovacs - {e}")
                            pass
                        proxymodelog.info(f"MQTT Proxy Mode - Proxy Bot Connected - Client_id: {client_id}")
                        asyncio.create_task(self.proxyclients[client_id].get_msg())


                else:
                    tmpclientdetail = str(didsplit[1]).split("/")
                    userid = didsplit[0]
                    realm = tmpclientdetail[0]
                    resource = tmpclientdetail[1]

                    if userid == "helperbot":
                        mqttserverlog.info(f"Bumper Authentication Success - Helperbot: {client_id}")
                        authenticated = True
                    elif bumper.check_authcode(didsplit[0], password) or not bumper.use_auth:
                        bumper.client_add(userid, realm, resource)
                        mqttserverlog.info(f"Bumper Authentication Success - Client - Username: {username} - "
                                           f"ClientID: {client_id}")
                        authenticated = True

            # Check for File Auth            
            if username and not authenticated:  # If there is a username and it isn't already authenticated
                hash = self._users.get(username, None)
                if hash:  # If there is a matching entry in passwd, check hash
                    authenticated = pwd_context.verify(password, hash)
                    if authenticated:
                        mqttserverlog.info(
                            f"File Authentication Success - Username: {username} - ClientID: {client_id}")
                    else:
                        mqttserverlog.info(f"File Authentication Failed - Username: {username} - ClientID: {client_id}")
                else:
                    mqttserverlog.info(
                        f"File Authentication Failed - No Entry for Username: {username} - ClientID: {client_id}")

        except Exception as e:
            mqttserverlog.exception(
                "Session: {} - {}".format((kwargs.get("session", None)), e)
            )
            authenticated = False

        # Check for allow anonymous
        allow_anonymous = self.auth_config.get("allow-anonymous", True)
        if allow_anonymous and not authenticated:  # If anonymous auth is allowed and it isn't already authenticated
            authenticated = True
            self.context.logger.debug(
                f"Anonymous Authentication Success: config allows anonymous - Username: {username}")
            mqttserverlog.info(f"Anonymous Authentication Success: config allows anonymous - Username: {username}")

        return authenticated

    def _read_password_file(self):
        password_file = self.auth_config.get('password-file', None)
        if password_file:
            try:
                with open(password_file) as f:
                    self.context.logger.debug(f"Reading user database from {password_file}")
                    for l in f:
                        line = l.strip()
                        if not line.startswith('#'):  # Allow comments in files
                            (username, pwd_hash) = line.split(sep=":", maxsplit=3)
                            if username:
                                self._users[username] = pwd_hash
                                self.context.logger.debug(f"user: {username} - hash: {pwd_hash}")
                self.context.logger.debug(f"{(len(self._users))} user(s) read from file {password_file}")
            except FileNotFoundError:
                self.context.logger.warning(f"Password file {password_file} not found")

    async def on_broker_client_subscribed(self, client_id, topic, qos):
        if bumper.bumper_proxy_mode: #if proxy mode, also subscribe on ecovacs server
            if client_id in self.proxyclients:
                await self.proxyclients[client_id].subscribe(
                    [
                        (topic, qos)
                    ]
                )
            else:
                proxymodelog.info(f"MQTT Proxy Mode - New MQTT Topic Subscription - Client: {client_id} - Topic: {topic}")

        #return
        #pass

    async def on_broker_client_connected(self, client_id):
        self._set_client_connected(client_id, True)

    def _set_client_connected(self, client_id, connected: bool):
        didsplit = str(client_id).split("@")

        bot = bumper.bot_get(didsplit[0])
        if bot:
            bumper.bot_set_mqtt(bot["did"], connected)
            return

        clientresource = didsplit[1].split("/")[1]
        client = bumper.client_get(clientresource)
        if client:
            bumper.client_set_mqtt(client["resource"], connected)

    async def on_broker_message_received(self, client_id, message):
        topic = message.topic
        topic_split = str(topic).split("/")
        data_decoded = str(message.data.decode("utf-8"))

        if bumper.bumper_proxy_mode:
            if client_id in self.proxyclients:
                if not str(message.topic).split("/")[
                           3] == "proxyhelper":  # if from proxyhelper, don't send back to ecovacs...yet
                    if str(message.topic).split("/")[6] == "proxyhelper":
                        ttopic = message.topic.split("/")
                        ttopic[6] = self.proxyclients[client_id].eco_helper_names.pop(ttopic[10], "")
                        ttopic_join = "/".join(ttopic)
                        proxymodelog.info(
                            f"MQTT Proxy Client - Bot Message Converted Topic From {message.topic} TO {ttopic_join} with message: {msgdata}")
                    else:
                        ttopic_join = message.topic
                        proxymodelog.info(f"MQTT Proxy Client - Bot Message From {ttopic_join} with message: {msgdata}")

                    try:
                        # Send back to ecovacs
                        proxymodelog.info(
                            f"MQTT Proxy Client - Proxy Forward Message to Ecovacs - Topic: {ttopic_join} - Message: {msgdata.encode()}")
                        await self.proxyclients[client_id].publish(
                            ttopic_join, msgdata.encode(), message.qos
                        )
                    except Exception as e:
                        proxymodelog.error(f"MQTT Proxy Client - Forwarding to Ecovacs Exception - {e}")

        if topic_split[6] == "helperbot":
            # Response to command
            helperbotlog.debug(f"Received Response - Topic: {topic} - Message: {data_decoded}")
            bumper.mqtt_helperbot.command_responses.append(
                {
                    "time": time.time(),
                    "topic": topic,
                    "payload": data_decoded,
                }
            )
        elif topic_split[3] == "helperbot":
            # Helperbot sending command
            helperbotlog.debug(f"Send Command - Topic: {topic} - Message: {data_decoded}")
        elif topic_split[1] == "atr":
            # Broadcast message received on atr
            if topic_split[2] == "errors":
                boterrorlog.error(f"Received Error - Topic: {topic} - Message: {data_decoded}")
            else:
                helperbotlog.debug(f"Received Broadcast - Topic: {topic} - Message: {data_decoded}")
        else:
            helperbotlog.debug(f"Received Message - Topic: {topic} - Message: {data_decoded}")

        # Cleanup "expired messages" > 60 seconds from time
        for msg in bumper.mqtt_helperbot.command_responses:
            expire_time = (
                    datetime.fromtimestamp(msg["time"]) + timedelta(seconds=MQTTHelperBot.wait_resp_timeout_seconds)
            ).timestamp()
            if time.time() > expire_time:
                helperbotlog.debug(f"Pruning Message Due To Expiration - Message Topic: {msg['topic']}")
                bumper.mqtt_helperbot.command_responses.remove(msg)

    async def on_broker_client_disconnected(self, client_id):
        if bumper.bumper_proxy_mode:
            if client_id in self.proxyclients:
                await self.proxyclients[client_id].disconnect()
        self._set_client_connected(client_id, False)
