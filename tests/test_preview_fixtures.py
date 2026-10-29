"""Concurrent fixture stacks must keep their own preview sockets, even for the same candidate."""
import multiprocessing
import socket
import sys

import pytest
from helpers import preview_listener


def serve_marker(pipe, marker):
    with preview_listener(run_id=19147) as listener:
        listener.settimeout(15)
        pipe.send(listener.getsockname()[1])
        with listener.accept()[0] as client:
            client.sendall(marker)
        assert pipe.recv() == 'stop'  # retain the reservation until both runs have been checked


def test_preview_run_identifier_uses_an_exclusively_held_socket():
    with preview_listener(run_id=19147) as listener:
        port = listener.getsockname()[1]
        assert 8000 <= port < 8100
        with pytest.raises(RuntimeError, match='No free preview test port'):
            with preview_listener(ports=[port], run_id=19148):
                pytest.fail('another run acquired the held preview socket')


def test_two_concurrent_preview_runs_cannot_reach_each_others_listener():
    context = multiprocessing.get_context('spawn')
    children, parents, ports = [], [], []
    try:
        for marker in (b'run-one', b'run-two'):
            parent, child = context.Pipe()
            process = context.Process(target=serve_marker, args=(child, marker))
            process.start()
            child.close()
            children.append(process)
            parents.append(parent)
            assert parent.poll(15), 'preview fixture did not start'
            ports.append(parent.recv())
        assert ports[0] != ports[1]
        for port, marker in zip(ports, (b'run-one', b'run-two'), strict=True):
            with socket.create_connection(('127.0.0.1', port), timeout=5) as client:
                assert client.recv(32) == marker
        for parent in parents:
            parent.send('stop')
        for process in children:
            process.join(15)
            assert process.exitcode == 0
    finally:
        for process in children:
            if process.is_alive():
                process.terminate()  # only this test's recorded child
            process.join(5)
            process.close()
        for parent in parents:
            parent.close()


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux SO_REUSEADDR bind/listen race')
def test_listener_retries_if_another_run_listens_between_bind_and_listen(monkeypatch):
    original = socket.socket.listen
    raced = False
    with socket.socket() as competing:
        competing.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        def listen(listener, *args):
            nonlocal raced
            if not raced:
                raced = True
                # Both reuse-address sockets can bind before either listens. This run loses the listen race.
                competing.bind(listener.getsockname())
                original(competing)
            return original(listener, *args)

        monkeypatch.setattr(socket.socket, 'listen', listen)
        with preview_listener(run_id=19147) as listener:
            assert listener.getsockname()[1] != competing.getsockname()[1]
