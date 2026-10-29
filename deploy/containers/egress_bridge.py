#!/usr/bin/python3 -I
"""Image-local loopback-to-session-socket transport, with no policy authority."""
import os
import runpy
import socket
import sys
import threading


def serve(listener, *, path='/run/acc-egress/socket', relay=None):
    if relay is None:
        relay = runpy.run_path('/opt/acc/runtime/egress.py')['relay']
    capacity = threading.BoundedSemaphore(4)

    def handle(client):
        upstream = socket.socket(socket.AF_UNIX)
        try:
            client.settimeout(5)
            upstream.settimeout(5)
            upstream.connect(path)
            relay(client, upstream)
        except OSError:
            pass
        finally:
            client.close(); upstream.close(); capacity.release()

    while True:
        client, _ = listener.accept()
        if not capacity.acquire(False):
            client.close(); continue
        threading.Thread(target=handle, args=(client,), daemon=True).start()


if __name__ == '__main__':
    if os.getuid() == 0 or len(sys.argv) != 2:
        raise SystemExit('egress bridge requires a non-root session listener')
    with socket.socket(fileno=int(sys.argv[1])) as listener:
        if listener.getsockname() != ('127.0.0.1', 18765):
            raise SystemExit('egress bridge requires fixed loopback listener')
        serve(listener)
