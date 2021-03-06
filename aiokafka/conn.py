import asyncio
import struct
import logging

import kafka.common as Errors
from kafka.protocol.api import RequestHeader
from kafka.protocol.commit import (
    GroupCoordinatorResponse_v0 as GroupCoordinatorResponse)

from aiokafka import ensure_future

__all__ = ['AIOKafkaConnection', 'create_conn']


READER_LIMIT = 2 ** 16


@asyncio.coroutine
def create_conn(host, port, *, loop=None, client_id='aiokafka',
                request_timeout_ms=40000, api_version=(0, 8, 2),
                ssl_context=None, security_protocol="PLAINTEXT"):
    if loop is None:
        loop = asyncio.get_event_loop()
    conn = AIOKafkaConnection(
        host, port, loop=loop, client_id=client_id,
        request_timeout_ms=request_timeout_ms,
        api_version=api_version,
        ssl_context=ssl_context, security_protocol=security_protocol)
    yield from conn.connect()
    return conn


class AIOKafkaProtocol(asyncio.StreamReaderProtocol):

    def __init__(self, closed_fut, *args, loop, **kw):
        self._closed_fut = closed_fut
        super().__init__(*args, loop=loop, **kw)

    def connection_lost(self, exc):
        super().connection_lost(exc)
        if not self._closed_fut.cancelled():
            self._closed_fut.set_result(None)


class AIOKafkaConnection:
    """Class for manage connection to Kafka node"""

    HEADER = struct.Struct('>i')

    log = logging.getLogger(__name__)

    def __init__(self, host, port, *, loop, client_id='aiokafka',
                 request_timeout_ms=40000, api_version=(0, 8, 2),
                 ssl_context=None, security_protocol="PLAINTEXT"):
        self._loop = loop
        self._host = host
        self._port = port
        self._request_timeout = request_timeout_ms / 1000
        self._api_version = api_version
        self._client_id = client_id
        self._ssl_context = ssl_context
        self._secutity_protocol = security_protocol

        self._reader = self._writer = self._protocol = None
        self._requests = []
        self._read_task = None
        self._correlation_id = 0
        self._closed_fut = None

    @asyncio.coroutine
    def connect(self):
        loop = self._loop
        self._closed_fut = asyncio.Future(loop=loop)
        if self._secutity_protocol == "PLAINTEXT":
            ssl = None
        else:
            assert self._secutity_protocol == "SSL"
            assert self._ssl_context is not None
            ssl = self._ssl_context
        # Create streams same as `open_connection`, but using custom protocol
        reader = asyncio.StreamReader(limit=READER_LIMIT, loop=loop)
        protocol = AIOKafkaProtocol(self._closed_fut, reader, loop=loop)
        transport, _ = yield from asyncio.wait_for(
            loop.create_connection(
                lambda: protocol, self.host, self.port, ssl=ssl),
            loop=loop, timeout=self._request_timeout)
        writer = asyncio.StreamWriter(transport, protocol, reader, loop)
        self._reader, self._writer, self._protocol = reader, writer, protocol
        # Start reader task.
        self._read_task = ensure_future(self._read(), loop=loop)
        return reader, writer

    def __repr__(self):
        return "<AIOKafkaConnection host={0.host} port={0.port}>".format(self)

    @property
    def host(self):
        return self._host

    @property
    def port(self):
        return self._port

    def send(self, request, expect_response=True):
        if self._writer is None:
            raise Errors.ConnectionError(
                "No connection to broker at {0}:{1}"
                .format(self._host, self._port))

        correlation_id = self._next_correlation_id()
        header = RequestHeader(request,
                               correlation_id=correlation_id,
                               client_id=self._client_id)
        message = header.encode() + request.encode()
        size = self.HEADER.pack(len(message))
        try:
            self._writer.write(size + message)
        except OSError as err:
            self.close()
            raise Errors.ConnectionError(
                "Connection at {0}:{1} broken: {2}".format(
                    self._host, self._port, err))

        if not expect_response:
            return self._writer.drain()
        fut = asyncio.Future(loop=self._loop)
        self._requests.append((correlation_id, request.RESPONSE_TYPE, fut))
        return asyncio.wait_for(fut, self._request_timeout, loop=self._loop)

    def connected(self):
        return bool(self._reader is not None and not self._reader.at_eof())

    def close(self):
        if self._reader is not None:
            self._writer.close()
            self._writer = self._reader = None
            self._read_task.cancel()
            self._read_task = None
            error = Errors.ConnectionError(
                "Connection at {0}:{1} closed".format(
                    self._host, self._port))
            for _, _, fut in self._requests:
                if not fut.done():
                    fut.set_exception(error)
            self._requests = []
        # transport.close() will close socket, but not right ahead. Return
        # a future in case we need to wait on it.
        return self._closed_fut

    @asyncio.coroutine
    def _read(self):
        try:
            while True:
                resp = yield from self._reader.readexactly(4)
                size, = self.HEADER.unpack(resp)

                resp = yield from self._reader.readexactly(size)

                recv_correlation_id, = self.HEADER.unpack(resp[:4])

                correlation_id, resp_type, fut = self._requests.pop(0)
                if (self._api_version == (0, 8, 2) and
                        resp_type is GroupCoordinatorResponse and
                        correlation_id != 0 and recv_correlation_id == 0):
                    self.log.warning(
                        'Kafka 0.8.2 quirk -- GroupCoordinatorResponse'
                        ' coorelation id does not match request. This'
                        ' should go away once at least one topic has been'
                        ' initialized on the broker')

                elif correlation_id != recv_correlation_id:
                    error = Errors.CorrelationIdError(
                        'Correlation ids do not match: sent {}, recv {}'
                        .format(correlation_id, recv_correlation_id))
                    if not fut.done():
                        fut.set_exception(error)
                    self.close()
                    break

                if not fut.done():
                    response = resp_type.decode(resp[4:])
                    self.log.debug('%s Response %d: %s',
                                   self, correlation_id, response)
                    fut.set_result(response)
        except (OSError, EOFError, ConnectionError) as exc:
            conn_exc = Errors.ConnectionError(
                "Connection at {0}:{1} broken".format(self._host, self._port))
            conn_exc.__cause__ = exc
            conn_exc.__context__ = exc
            for _, _, fut in self._requests:
                fut.set_exception(conn_exc)
            self.close()
        except asyncio.CancelledError:
            pass

    def _next_correlation_id(self):
        self._correlation_id = (self._correlation_id + 1) % 2**31
        return self._correlation_id
