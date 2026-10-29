"""Acquire the inherited PTY in a fresh session, then exec the terminal client.

Runs in a separate interpreter, never as a preexec_fn in the threaded broker.
"""
import fcntl
import os
import sys
import termios

if __name__ == "__main__":
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    os.execvpe(sys.argv[1], sys.argv[1:], os.environ)
