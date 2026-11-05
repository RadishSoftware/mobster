import fcntl
import os
import socket
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mobile_agent.decision_provider import JevDecisions
from mobile_agent.http_pool import ConnectionPool, PooledHTTP


class FakeConnection:
    def __init__(self):
        self.sock = None
        self.peer = None
        self.timeout = None

    def connect(self):
        self.sock, self.peer = socket.socketpair()


class FakeHTTP:
    made = []

    def __init__(self, base_url, key=""):
        self.base_url, self.key = base_url, key
        self.connection = FakeConnection()
        self._inflight = threading.Lock()
        self.closed = 0
        self.keys_seen = []
        self.hold = None
        FakeHTTP.made.append(self)

    def request(self, method, path, body=None, timeout=20):
        with self._inflight:
            if self.connection.sock is None:
                self.connection.connect()
            self.keys_seen.append(self.key)
            if self.hold is not None:
                self.hold.wait(2)
            return {"ok": True}

    def close(self):
        self.closed += 1
        self.release_sockets()

    def release_sockets(self):
        if self.connection.sock is not None:
            self.connection.sock.close()
            self.connection.peer.close()
        self.connection.sock = None


def close_fakes():
    for client in FakeHTTP.made:
        client.release_sockets()
    FakeHTTP.made = []


class PoolTests(unittest.TestCase):
    def setUp(self):
        FakeHTTP.made = []
        self.clock = [0.0]
        self.pool = ConnectionPool(max_idle=60, per_key=2, factory=FakeHTTP, clock=lambda: self.clock[0])

    def tearDown(self):
        close_fakes()

    def pooled(self, key="k1", url="https://api.example"):
        return PooledHTTP(url, key, pool=self.pool)

    def hold_first_request(self, client):
        client.request("POST", "/x")
        hold = threading.Event()
        FakeHTTP.made[0].hold = hold
        thread = threading.Thread(target=client.request, args=("POST", "/x"))
        thread.start()
        while not FakeHTTP.made[0]._inflight.locked():
            pass
        return hold, thread

    def test_next_run_reuses_the_warm_connection(self):
        first = self.pooled()
        first.request("POST", "/x")
        first.close()
        second = self.pooled()
        second.request("POST", "/x")
        self.assertEqual(len(FakeHTTP.made), 1)
        self.assertEqual(self.pool.stats["reused"], 1)
        self.assertEqual(FakeHTTP.made[0].closed, 0)

    def test_credentials_and_hosts_never_share_connections(self):
        for key, url in [("k1", "https://a.example"), ("k2", "https://a.example"), ("k1", "https://b.example")]:
            client = self.pooled(key, url)
            client.request("POST", "/x")
            client.close()
        self.assertEqual(len(FakeHTTP.made), 3)

    def test_dropped_or_stale_connections_are_discarded(self):
        client = self.pooled()
        client.request("POST", "/x")
        client.close()
        FakeHTTP.made[0].connection.peer.close()
        again = self.pooled()
        again.request("POST", "/x")
        self.assertEqual(len(FakeHTTP.made), 2)
        self.assertEqual(self.pool.stats["discarded"], 1)
        self.assertEqual(FakeHTTP.made[0].closed, 1)
        again.close()
        self.clock[0] += 61
        late = self.pooled()
        late.request("POST", "/x")
        self.assertEqual(len(FakeHTTP.made), 3)

    def test_unsolicited_bytes_make_a_connection_unusable(self):
        client = self.pooled()
        client.request("POST", "/x")
        client.close()
        FakeHTTP.made[0].connection.peer.sendall(b"HTTP/1.1 408 Request Timeout\r\n\r\n")
        self.pooled().request("POST", "/x")
        self.assertEqual(len(FakeHTTP.made), 2)

    def test_tls_buffered_bytes_make_a_connection_unusable(self):
        client = self.pooled()
        client.request("POST", "/x")
        client.close()
        connection = FakeHTTP.made[0].connection
        sock = connection.sock
        connection.sock = SimpleNamespace(fileno=sock.fileno, close=sock.close, pending=lambda: 1)
        self.pooled().request("POST", "/x")
        self.assertEqual(len(FakeHTTP.made), 2)
        self.assertEqual(self.pool.stats["discarded"], 1)

    def test_idle_socket_above_select_descriptor_limit_is_reused(self):
        client = self.pooled()
        client.request("POST", "/x")
        client.close()
        connection = FakeHTTP.made[0].connection
        try:
            descriptor = fcntl.fcntl(connection.sock, fcntl.F_DUPFD, 1024)
        except OSError as error:
            self.skipTest(f"cannot allocate a high descriptor: {error}")
        high = socket.socket(fileno=descriptor)
        connection.sock.close()
        connection.sock = high
        self.pooled().request("POST", "/x")
        self.assertEqual(len(FakeHTTP.made), 1)
        self.assertEqual(self.pool.stats["reused"], 1)

    def test_close_during_a_request_aborts_it_and_never_pools_it(self):
        client = self.pooled()
        hold, thread = self.hold_first_request(client)
        client.close()
        hold.set()
        thread.join()
        self.assertEqual(FakeHTTP.made[0].closed, 1)
        self.assertEqual(self.pool.stats["released"], 0)

    def test_a_second_request_on_a_busy_borrower_gets_its_own_connection(self):
        client = self.pooled()
        hold, thread = self.hold_first_request(client)
        self.assertEqual(client.request("POST", "/x"), {"ok": True})
        hold.set()
        thread.join()
        self.assertEqual(len(FakeHTTP.made), 2)

    def test_failed_request_connection_is_not_pooled(self):
        client = self.pooled()
        client.request("POST", "/x")
        FakeHTTP.made[0].close()
        client.close()
        self.assertEqual(self.pool.stats["released"], 0)

    def test_close_is_idempotent_and_request_after_close_borrows_again(self):
        client = self.pooled()
        client.request("POST", "/x")
        client.close()
        client.close()
        client.request("POST", "/x")
        self.assertEqual(len(FakeHTTP.made), 1)
        self.assertEqual((self.pool.stats["released"], self.pool.stats["reused"]), (1, 1))

    def test_idle_connections_are_capped_per_key(self):
        clients = [self.pooled() for _ in range(3)]
        for client in clients:
            client.request("POST", "/x")
        for client in clients:
            client.close()
        self.assertEqual([client.closed for client in FakeHTTP.made], [1, 0, 0])

    def test_borrowed_clients_are_exclusive_across_threads(self):
        clients = [self.pooled() for _ in range(4)]
        barrier = threading.Barrier(4)

        def run(client):
            barrier.wait()
            client.request("POST", "/x")
        threads = [threading.Thread(target=run, args=(c,)) for c in clients]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(FakeHTTP.made), 4)

    def test_temporary_key_is_cleared_before_pooling(self):
        client = PooledHTTP("https://vertex.example", "", pool=self.pool)
        client.key = "short-lived-token"
        client.request("POST", "/x")
        client.close()
        self.assertEqual(FakeHTTP.made[0].keys_seen, ["short-lived-token"])
        self.assertEqual(FakeHTTP.made[0].key, "")

    def test_prewarm_opens_a_connection_off_thread(self):
        self.pool.prewarm("https://api.example", "k1").join(2)
        self.pooled().request("POST", "/x")
        self.assertEqual(len(FakeHTTP.made), 1)
        self.assertEqual(self.pool.stats["reused"], 1)

    def test_jev_decisions_client_is_pooled_and_reused_after_close(self):
        with patch("mobile_agent.http_pool.POOL", self.pool), patch.dict(os.environ, {"TYPESAFE_API_KEY": "test"}):
            first = JevDecisions()
            first.http.request("POST", "/systemone")
            first.http.close()
            JevDecisions().http.request("POST", "/systemone")
        self.assertEqual(len(FakeHTTP.made), 1)


if __name__ == "__main__":
    unittest.main()
