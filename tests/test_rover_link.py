"""The EP link under the failure modes seen on the real robot (2026-10-10): replies without ';', a reply that comes
late, a link that stalls, and many callers at once. Real TCP sockets, a scripted EP behind them."""
import socket
import threading
import time

import pytest

from rover import robot_server as rs


class FlakyEP:
    """An EP SDK port whose replies can be slow, missing their ';', or never come."""

    def __init__(self, semicolon=True):
        self.ep, self.semicolon = rs.FakeEP(), semicolon
        self.delays = []            # seconds to hold back each next reply (consumed in order)
        self.accepts = 0
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            self.accepts += 1
            threading.Thread(target=self._talk, args=(conn,), daemon=True).start()

    def _talk(self, conn):
        buf = b""
        try:
            while (data := conn.recv(1024)):
                buf += data
                while b";" in buf:
                    command, buf = buf.split(b";", 1)
                    command = command.decode().strip()
                    if command == "quit":
                        return
                    delay = self.delays.pop(0) if self.delays else 0.0
                    reply = self.ep.cmd(command)
                    if delay:
                        threading.Timer(delay, self._send, args=(conn, reply)).start()
                    else:
                        self._send(conn, reply)
        except OSError:
            pass

    def _send(self, conn, reply):
        try:
            conn.sendall(reply.encode() + (b";" if self.semicolon else b""))
        except OSError:
            pass

    def close(self):
        self.srv.close()


def link_to(ep, timeout=0.3):
    link = rs.SdkLink("127.0.0.1", port=ep.port, timeout=timeout)
    link.RETRY_S, link.RETRY_MAX_S = 0.05, 0.1
    assert link.open(wait_s=3)
    return link


def test_replies_without_a_semicolon_are_fast():
    ep = FlakyEP(semicolon=False)
    link = link_to(ep)
    try:
        t0 = time.monotonic()
        for _ in range(10):
            assert link.cmd("robot battery ?") == "87"
        assert time.monotonic() - t0 < 0.5           # was >= 0.2 s per command waiting for a ';'
    finally:
        link.close(); ep.close()


def test_one_late_reply_keeps_the_link_and_does_not_shift_the_replies():
    ep = FlakyEP()
    link = link_to(ep)
    try:
        ep.delays = [0.45]                           # the battery reply arrives after the 0.3 s timeout
        with pytest.raises(TimeoutError):
            link.cmd("robot battery ?")
        assert link.connected and ep.accepts == 1    # no redial
        reply = link.cmd("chassis position ?")
        assert len(reply.split()) >= 3                # its own answer, not the late "87"
        assert link.stats["timeouts"] == 1 and link.stats["drops"] == 0
    finally:
        link.close(); ep.close()


def test_three_timeouts_in_a_row_drop_and_the_link_comes_back():
    ep = FlakyEP()
    link = link_to(ep, timeout=0.15)
    try:
        ep.delays = [1.0, 1.0, 1.0]
        for _ in range(3):
            with pytest.raises(OSError):
                link.cmd("robot battery ?")
        for _ in range(100):
            if link.connected and ep.accepts >= 2:
                break
            time.sleep(0.02)
        time.sleep(1.1)                              # the delayed replies went to the old, closed socket
        assert link.cmd("robot battery ?") == "87" and link.stats["connects"] == 2
    finally:
        link.close(); ep.close()


def test_a_caller_that_cannot_get_the_link_gets_busy_quickly():
    ep = FlakyEP()
    link = link_to(ep, timeout=2.0)
    link.LOCK_WAIT_S = 0.1
    try:
        ep.delays = [0.6]
        threading.Thread(target=link.cmd, args=("robot battery ?",), daemon=True).start()
        time.sleep(0.05)
        t0 = time.monotonic()
        with pytest.raises(rs.LinkBusy):
            link.cmd("chassis position ?")
        assert time.monotonic() - t0 < 0.3 and link.connected
    finally:
        link.close(); ep.close()


def test_pose_answers_from_the_poller_while_the_link_is_slow():
    ep = FlakyEP()
    link = link_to(ep, timeout=2.0)
    rover = rs.Rover(link)
    stop = threading.Event()
    threading.Thread(target=rover.poll, args=(stop, 0.02), daemon=True).start()
    try:
        time.sleep(0.2)
        ep.delays = [0.8]                            # the next command (a poll) is stuck for 0.8 s
        time.sleep(0.1)
        t0 = time.monotonic()
        p = rover.pose()
        assert p["ok"] and time.monotonic() - t0 < 0.05 and p["age_s"] <= rover.POSE_MAX_AGE_S
    finally:
        stop.set(); link.close(); ep.close()
