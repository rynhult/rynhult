"""A test listener must survive the TCP churn caused by concurrent preview suites."""
import socket
from contextlib import ExitStack

from helpers import preview_listener


def test_listener_rebinds_after_server_closes_connection():
    # The server closes first, leaving its address in TIME_WAIT after the test.
    with preview_listener(ports=[0]) as listener:
        port = listener.getsockname()[1]
        with socket.create_connection(('127.0.0.1', port)) as client:
            connection, _ = listener.accept()
            connection.close()
            assert client.recv(1) == b''
    with preview_listener(ports=[port]) as listener:
        with socket.create_connection(listener.getsockname()) as client:
            connection, _ = listener.accept()
            with connection:
                connection.sendall(b'reused')
                assert client.recv(6) == b'reused'


def test_live_listeners_keep_distinct_ports():
    with ExitStack() as stack:
        listeners = [stack.enter_context(preview_listener()) for _ in range(3)]
        assert len({listener.getsockname()[1] for listener in listeners}) == 3
