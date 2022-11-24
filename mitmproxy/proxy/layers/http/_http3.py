from abc import abstractmethod
import time
from typing import Dict, Union

from aioquic.h3.connection import (
    ErrorCode as H3ErrorCode,
    FrameUnexpected as H3FrameUnexpected,
)
from aioquic.h3.events import DataReceived, HeadersReceived, PushPromiseReceived

from mitmproxy import connection, http, version
from mitmproxy.net.http import status_codes
from mitmproxy.proxy import commands, context, events, layer
from mitmproxy.proxy.layers.quic import (
    QuicConnectionClosed,
    QuicStreamEvent,
    StopQuicStream,
    error_code_to_str,
)
from mitmproxy.proxy.utils import expect

from . import (
    RequestData,
    RequestEndOfMessage,
    RequestHeaders,
    RequestProtocolError,
    RequestTrailers,
    ResponseData,
    ResponseEndOfMessage,
    ResponseHeaders,
    ResponseProtocolError,
    ResponseTrailers,
)
from ._base import (
    HttpConnection,
    HttpEvent,
    ReceiveHttp,
    format_error,
)
from ._http2 import (
    format_h2_request_headers,
    format_h2_response_headers,
    parse_h2_request_headers,
    parse_h2_response_headers,
)
from ._http_h3 import LayeredH3Connection, StreamReset, TrailersReceived


class Http3Connection(HttpConnection):
    h3_conn: LayeredH3Connection

    ReceiveData: type[Union[RequestData, ResponseData]]
    ReceiveEndOfMessage: type[Union[RequestEndOfMessage, ResponseEndOfMessage]]
    ReceiveProtocolError: type[Union[RequestProtocolError, ResponseProtocolError]]
    ReceiveTrailers: type[Union[RequestTrailers, ResponseTrailers]]

    def __init__(self, context: context.Context, conn: connection.Connection):
        super().__init__(context, conn)
        self.h3_conn = LayeredH3Connection(self.conn, is_client=self.conn is self.context.server)

    def _handle_event(self, event: events.Event) -> layer.CommandGenerator[None]:
        if isinstance(event, events.Start):
            yield from self.h3_conn.transmit()

        # send mitmproxy HTTP events over the H3 connection
        elif isinstance(event, HttpEvent):
            try:
                if isinstance(event, (RequestData, ResponseData)):
                    self.h3_conn.send_data(event.stream_id, event.data)
                elif isinstance(event, (RequestHeaders, ResponseHeaders)):
                    headers = yield from (
                        format_h2_request_headers(self.context, event)
                        if isinstance(event, RequestHeaders)
                        else format_h2_response_headers(self.context, event)
                    )
                    self.h3_conn.send_headers(event.stream_id, headers, end_stream=event.end_stream)
                elif isinstance(event, (RequestTrailers, ResponseTrailers)):
                    self.h3_conn.send_trailers(event.stream_id, [*event.trailers.fields])
                elif isinstance(event, (RequestEndOfMessage, ResponseEndOfMessage)):
                    if not self.h3_conn.has_sent_end_stream(event.stream_id):
                        self.h3_conn.send_data(event.stream_id, b"", end_stream=True)
                elif isinstance(event, (RequestProtocolError, ResponseProtocolError)):
                    code = {
                        status_codes.CLIENT_CLOSED_REQUEST: H3ErrorCode.H3_REQUEST_CANCELLED.value,
                    }.get(event.code, H3ErrorCode.H3_INTERNAL_ERROR.value)
                    send_error_message = (
                        isinstance(event, ResponseProtocolError)
                        and not self.h3_conn.has_sent_headers(event.stream_id)
                        and event.code != status_codes.NO_RESPONSE
                    )
                    if send_error_message:
                        self.h3_conn.send_headers(
                            event.stream_id,
                            [
                                (b":status", b"%d" % event.code),
                                (b"server", version.MITMPROXY.encode()),
                                (b"content-type", b"text/html"),
                            ],
                        )
                        self.h3_conn.send_data(
                            event.stream_id,
                            format_error(event.code, event.message),
                            end_stream=True,
                        )
                    elif self.h3_conn.has_sent_end_stream(event.stream_id):
                        yield StopQuicStream(self.conn, event.stream_id, code)
                    else:
                        self.h3_conn.reset_stream(event.stream_id, code)
                else:
                    raise AssertionError(f"Unexpected event: {event!r}")

            except H3FrameUnexpected as e:
                # Http2Connection also ignores HttpEvents that violate the current stream state
                yield commands.Log(f"Received {event!r} unexpectedly: {e}")

            else:
                # transmit buffered data
                yield from self.h3_conn.transmit()

        # forward stream messages from the QUIC layer to the H3 connection
        elif isinstance(event, QuicStreamEvent):
            for h3_event in self.h3_conn.handle_stream_event(event):
                if isinstance(h3_event, StreamReset):
                    if h3_event.push_id is None:
                        err_str = error_code_to_str(h3_event.error_code)
                        err_code = {
                            H3ErrorCode.H3_REQUEST_CANCELLED.value: status_codes.CLIENT_CLOSED_REQUEST,
                        }.get(h3_event.error_code, self.ReceiveProtocolError.code)
                        yield ReceiveHttp(
                            self.ReceiveProtocolError(
                                h3_event.stream_id,
                                f"stream reset by client ({err_str})",
                                code=err_code,
                            )
                        )
                elif isinstance(h3_event, DataReceived):
                    if h3_event.push_id is None:
                        if h3_event.data:
                            yield ReceiveHttp(self.ReceiveData(h3_event.stream_id, h3_event.data))
                        if h3_event.stream_ended:
                            yield ReceiveHttp(self.ReceiveEndOfMessage(h3_event.stream_id))
                elif isinstance(h3_event, HeadersReceived):
                    if h3_event.push_id is None:
                        try:
                            receive_event = self.parse_headers(h3_event)
                        except ValueError as e:
                            self.h3_conn.close_connection(
                                error_code=H3ErrorCode.H3_GENERAL_PROTOCOL_ERROR,
                                reason_phrase=f"Invalid HTTP/3 request headers: {e}",
                            )
                        else:
                            yield ReceiveHttp(receive_event)
                            if h3_event.stream_ended:
                                yield ReceiveHttp(self.ReceiveEndOfMessage(h3_event.stream_id))
                elif isinstance(h3_event, TrailersReceived):
                    if h3_event.push_id is None:
                        yield ReceiveHttp(self.ReceiveTrailers(h3_event.stream_id, http.Headers(h3_event.trailers)))
                        if h3_event.stream_ended:
                            yield ReceiveHttp(self.ReceiveEndOfMessage(h3_event.stream_id))
                elif isinstance(h3_event, PushPromiseReceived):
                    # we don't support push
                    pass
                else:
                    raise AssertionError(f"Unexpected event: {event!r}")
            yield from self.h3_conn.transmit()

        # report a protocol error for all remaining open streams when a connection is closed
        elif isinstance(event, QuicConnectionClosed):
            self._handle_event = self.done  # type: ignore
            msg = event.reason_phrase or error_code_to_str(event.error_code)
            for stream_id in self.h3_conn.get_reserved_stream_ids():
                yield ReceiveHttp(self.ReceiveProtocolError(stream_id, msg))

        else:
            raise AssertionError(f"Unexpected event: {event!r}")

    @expect(HttpEvent, QuicStreamEvent, QuicConnectionClosed)
    def done(self, _) -> layer.CommandGenerator[None]:
        yield from ()

    @abstractmethod
    def parse_headers(
        self, event: HeadersReceived
    ) -> Union[RequestHeaders, ResponseHeaders]:
        pass  # pragma: no cover


class Http3Server(Http3Connection):
    ReceiveData = RequestData
    ReceiveEndOfMessage = RequestEndOfMessage
    ReceiveProtocolError = RequestProtocolError
    ReceiveTrailers = RequestTrailers

    def __init__(self, context: context.Context):
        super().__init__(context, context.client)

    def parse_headers(self, event: HeadersReceived) -> Union[RequestHeaders, ResponseHeaders]:
        # same as HTTP/2
        (
            host,
            port,
            method,
            scheme,
            authority,
            path,
            headers,
        ) = parse_h2_request_headers(event.headers)
        request = http.Request(
            host=host,
            port=port,
            method=method,
            scheme=scheme,
            authority=authority,
            path=path,
            http_version=b"HTTP/3",
            headers=headers,
            content=None,
            trailers=None,
            timestamp_start=time.time(),
            timestamp_end=None,
        )
        return RequestHeaders(event.stream_id, request, end_stream=event.stream_ended)


class Http3Client(Http3Connection):
    ReceiveData = ResponseData
    ReceiveEndOfMessage = ResponseEndOfMessage
    ReceiveProtocolError = ResponseProtocolError
    ReceiveTrailers = ResponseTrailers

    our_stream_id: Dict[int, int]
    their_stream_id: Dict[int, int]

    def __init__(self, context: context.Context):
        super().__init__(context, context.server)
        self.our_stream_id = {}
        self.their_stream_id = {}

    def _handle_event(self, event: events.Event) -> layer.CommandGenerator[None]:
        # QUIC and HTTP/3 would actually allow for direct stream ID mapping, but since we want
        # to support H2<->H3, we need to translate IDs.
        # NOTE: We always create bidirectional streams, as we can't safely infer unidirectionality.
        if isinstance(event, HttpEvent):
            ours = self.our_stream_id.get(event.stream_id, None)
            if ours is None:
                ours = self.h3_conn.get_next_available_stream_id()
                self.our_stream_id[event.stream_id] = ours
                self.their_stream_id[ours] = event.stream_id
            event.stream_id = ours

        for cmd in super()._handle_event(event):
            if isinstance(cmd, ReceiveHttp):
                cmd.event.stream_id = self.their_stream_id[cmd.event.stream_id]
            yield cmd

    def parse_headers(self, event: HeadersReceived) -> Union[RequestHeaders, ResponseHeaders]:
        # same as HTTP/2
        status_code, headers = parse_h2_response_headers(event.headers)
        response = http.Response(
            http_version=b"HTTP/3",
            status_code=status_code,
            reason=b"",
            headers=headers,
            content=None,
            trailers=None,
            timestamp_start=time.time(),
            timestamp_end=None,
        )
        return ResponseHeaders(event.stream_id, response, event.stream_ended)


__all__ = [
    "Http3Client",
    "Http3Server",
]
