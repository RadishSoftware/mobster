"""A borrower whose connection is busy serves a concurrent request on a second pooled one. Offline."""

import threading
import unittest

from mobile_agent.http_pool import ConnectionPool, PooledHTTP


class Client:
    def __init__(self):
        self.key, self.gate, self.lock = "", threading.Event(), threading.Lock()
        self._inflight = self.lock
        self.connection = type("C", (), {"sock": object()})()

    def request(self, method, path, body=None, timeout=20):
        if not self.lock.acquire(blocking=False):
            raise AssertionError("one connection carried two requests")
        try:
            if path == "/slow":
                self.gate.wait(2)
            return {"path": path}
        finally:
            self.lock.release()

    def close(self):
        pass


class Pool:
    def __init__(self):
        self.made = []

    def acquire(self, base_url, key=""):
        client = Client()
        self.made.append(client)
        return client

    def release(self, base_url, key, client):
        return True


class ConcurrencyTests(unittest.TestCase):
    def test_a_second_request_gets_its_own_connection(self):
        pool = Pool()
        http = PooledHTTP("https://example.test", pool=pool)
        slow = threading.Thread(target=http.request, args=("POST", "/slow"))
        slow.start()
        for _ in range(100):
            if pool.made and pool.made[0].lock.locked():
                break
            threading.Event().wait(.01)
        self.assertEqual(http.request("POST", "/fast"), {"path": "/fast"})
        pool.made[0].gate.set()
        slow.join()
        self.assertEqual(len(pool.made), 2)


if __name__ == "__main__":
    unittest.main()
