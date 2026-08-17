"""Transport tests. Real sockets, real subprocesses, real header validation."""

from __future__ import annotations

import http.client
import json
import sys
import threading
import time
import unittest

from meridian.protocol import (
    Client,
    ClientCapabilities,
    Implementation,
    Server,
    StdioClientTransport,
    StreamableHttpClient,
    StreamableHttpServer,
    build_request_meta,
    encode,
    text_result,
)
from meridian.protocol import errors
from meridian.protocol.http import (
    decode_header_value,
    encode_header_value,
    header_params_for,
)
from meridian.protocol.meta import PROTOCOL_VERSION

ECHO_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "region": {"type": "string", "x-mcp-header": "Region"},
    },
    "required": ["text"],
}


def build() -> Server:
    server = Server("wire-test", "1.0.0", list_changed=True, subscribe=True)

    @server.tool("echo", "Echo the text back", ECHO_SCHEMA)
    def echo(ctx):
        return text_result(ctx.arguments["text"])

    @server.tool("slow", "Emit progress then finish",
                 {"type": "object", "properties": {"steps": {"type": "integer"}}})
    def slow(ctx):
        steps = int(ctx.arguments.get("steps") or 3)
        for i in range(1, steps + 1):
            ctx.progress(i, steps, f"step {i}")
        return text_result("done")

    @server.resource("wire://doc", "Doc", ttl_ms=1000, cache_scope="public")
    def doc(ctx, uri):
        return "content"

    return server


def meta() -> dict:
    return build_request_meta(ClientCapabilities(),
                              Implementation("wire-test-client", "1.0.0"))


class TestHeaderEncoding(unittest.TestCase):
    def test_plain_ascii_passes_through(self):
        self.assertEqual(encode_header_value("us-west1"), "us-west1")

    def test_non_ascii_is_base64_sentinel_wrapped(self):
        encoded = encode_header_value("Hello, 世界")
        self.assertTrue(encoded.startswith("=?base64?"))
        self.assertEqual(decode_header_value(encoded), "Hello, 世界")

    def test_padding_forces_encoding(self):
        self.assertEqual(decode_header_value(encode_header_value(" padded ")), " padded ")

    def test_newlines_force_encoding(self):
        """Header injection is the whole reason this rule exists."""
        encoded = encode_header_value("line1\nline2")
        self.assertNotIn("\n", encoded)
        self.assertEqual(decode_header_value(encoded), "line1\nline2")

    def test_a_literal_sentinel_is_itself_encoded(self):
        literal = "=?base64?literal?="
        encoded = encode_header_value(literal)
        self.assertNotEqual(encoded, literal)
        self.assertEqual(decode_header_value(encoded), literal)

    def test_booleans_lowercase(self):
        self.assertEqual(encode_header_value(True), "true")
        self.assertEqual(encode_header_value(False), "false")

    def test_integers_are_decimal(self):
        self.assertEqual(encode_header_value(-7), "-7")

    def test_extraction_skips_absent_values(self):
        self.assertEqual(header_params_for(ECHO_SCHEMA, {"text": "hi"}), {})
        self.assertEqual(header_params_for(ECHO_SCHEMA, {"text": "hi", "region": "eu"}),
                         {"Mcp-Param-Region": "eu"})


class TestStreamableHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = build()
        cls.http = StreamableHttpServer(cls.server, port=0).start()
        cls.url = cls.http.url

    @classmethod
    def tearDownClass(cls):
        cls.http.stop()

    def raw_post(self, body: dict, headers: dict) -> tuple[int, dict]:
        conn = http.client.HTTPConnection(self.http.host, self.http.port, timeout=10)
        conn.request("POST", self.http.path, body=encode(body).encode(), headers=headers)
        resp = conn.getresponse()
        payload = resp.read()
        conn.close()
        try:
            return resp.status, json.loads(payload)
        except json.JSONDecodeError:
            return resp.status, {}

    def good_headers(self, method: str, name: str | None = None) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "Mcp-Method": method,
        }
        if name:
            headers["Mcp-Name"] = name
        return headers

    # -- the removed endpoints
    def test_get_is_405(self):
        conn = http.client.HTTPConnection(self.http.host, self.http.port, timeout=10)
        conn.request("GET", self.http.path)
        self.assertEqual(conn.getresponse().status, 405)
        conn.close()

    def test_delete_is_405(self):
        conn = http.client.HTTPConnection(self.http.host, self.http.port, timeout=10)
        conn.request("DELETE", self.http.path)
        self.assertEqual(conn.getresponse().status, 405)
        conn.close()

    # -- required headers
    def test_missing_protocol_version_header(self):
        headers = self.good_headers("tools/list")
        del headers["MCP-Protocol-Version"]
        status, body = self.raw_post(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": meta()}},
            headers)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], errors.HEADER_MISMATCH)

    def test_missing_method_header(self):
        headers = self.good_headers("tools/list")
        del headers["Mcp-Method"]
        status, body = self.raw_post(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": meta()}},
            headers)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], errors.HEADER_MISMATCH)

    def test_method_header_disagreeing_with_body_is_rejected(self):
        """The request-smuggling case. A gateway routing on the header must
        never be able to disagree with the server executing on the body."""
        status, body = self.raw_post(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "echo", "arguments": {"text": "hi"}, "_meta": meta()}},
            self.good_headers("tools/list", "echo"))
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], errors.HEADER_MISMATCH)

    def test_name_header_disagreeing_with_body_is_rejected(self):
        status, body = self.raw_post(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "echo", "arguments": {"text": "hi"}, "_meta": meta()}},
            self.good_headers("tools/call", "some_other_tool"))
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], errors.HEADER_MISMATCH)

    def test_missing_name_header_on_tools_call(self):
        status, body = self.raw_post(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "echo", "arguments": {"text": "hi"}, "_meta": meta()}},
            self.good_headers("tools/call"))
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], errors.HEADER_MISMATCH)

    def test_mirrored_param_header_must_match(self):
        headers = self.good_headers("tools/call", "echo")
        headers["Mcp-Param-Region"] = "us-east"
        status, body = self.raw_post(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "echo",
                        "arguments": {"text": "hi", "region": "eu-west"},
                        "_meta": meta()}},
            headers)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], errors.HEADER_MISMATCH)

    def test_version_header_must_match_body(self):
        headers = self.good_headers("tools/list")
        headers["MCP-Protocol-Version"] = "2025-11-25"
        status, body = self.raw_post(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": meta()}},
            headers)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], errors.HEADER_MISMATCH)

    # -- status codes
    def test_unknown_method_is_404_with_a_jsonrpc_body(self):
        status, body = self.raw_post(
            {"jsonrpc": "2.0", "id": 1, "method": "nope/nope", "params": {"_meta": meta()}},
            self.good_headers("nope/nope"))
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], errors.METHOD_NOT_FOUND)

    def test_notification_gets_202_and_no_body(self):
        conn = http.client.HTTPConnection(self.http.host, self.http.port, timeout=10)
        conn.request("POST", self.http.path,
                     body=encode({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                  "params": {"requestId": 1}}).encode(),
                     headers=self.good_headers("notifications/cancelled"))
        resp = conn.getresponse()
        self.assertEqual(resp.status, 202)
        self.assertEqual(resp.read(), b"")
        conn.close()

    # -- happy path through the real client
    def test_client_round_trip(self):
        client = Client(StreamableHttpClient(self.url), server_label="wire")
        self.assertEqual(
            client.call_tool("echo", {"text": "over the wire"})["content"][0]["text"],
            "over the wire")
        client.close()

    def test_client_sends_mirrored_headers(self):
        client = Client(StreamableHttpClient(self.url), server_label="wire")
        client.list_tools()  # learn the schema so the client can mirror
        result = client.call_tool("echo", {"text": "hi", "region": "eu-west"})
        self.assertEqual(result["content"][0]["text"], "hi")
        client.close()

    def test_connection_is_reused(self):
        transport = StreamableHttpClient(self.url)
        client = Client(transport, server_label="wire")
        for _ in range(5):
            client.call_tool("echo", {"text": "x"})
        self.assertEqual(transport.new_connections, 1)
        self.assertGreaterEqual(transport.reused_connections, 4)
        client.close()

    def test_progress_streams_before_the_response(self):
        seen: list[dict] = []
        client = Client(StreamableHttpClient(self.url), server_label="wire")
        result = client.call("tools/call",
                             {"name": "slow", "arguments": {"steps": 4}},
                             progress_token="p1",
                             on_notification=seen.append,
                             use_cache=False)
        self.assertEqual(result["content"][0]["text"], "done")
        progress = [n for n in seen if n.get("method") == "notifications/progress"]
        self.assertEqual(len(progress), 4)
        self.assertEqual([p["params"]["progress"] for p in progress], [1, 2, 3, 4])
        client.close()

    def test_origin_check(self):
        server = build()
        guarded = StreamableHttpServer(server, port=0,
                                       allowed_origins={"https://app.example"}).start()
        try:
            conn = http.client.HTTPConnection(guarded.host, guarded.port, timeout=10)
            headers = self.good_headers("tools/list")
            headers["Origin"] = "https://evil.example"
            conn.request("POST", guarded.path,
                         body=encode({"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                                      "params": {"_meta": meta()}}).encode(),
                         headers=headers)
            self.assertEqual(conn.getresponse().status, 403)
            conn.close()
        finally:
            guarded.stop()


class TestSubscriptionsOverHttp(unittest.TestCase):
    def test_acknowledged_first_then_only_requested_types(self):
        server = build()
        http_server = StreamableHttpServer(server, port=0).start()
        received: list[dict] = []
        stop = threading.Event()

        def listen():
            client = StreamableHttpClient(http_server.url)
            message = {
                "jsonrpc": "2.0", "id": 42, "method": "subscriptions/listen",
                "params": {"_meta": meta(),
                           "notifications": {"toolsListChanged": True}},
            }
            try:
                client.listen(message, received.append, stop)
            except Exception:
                pass

        thread = threading.Thread(target=listen, daemon=True)
        thread.start()

        deadline = time.time() + 5
        while not received and time.time() < deadline:
            time.sleep(0.02)

        self.assertTrue(received, "no acknowledgement arrived")
        first = received[0]
        self.assertEqual(first["method"], "notifications/subscriptions/acknowledged")
        self.assertEqual(
            first["params"]["_meta"]["io.modelcontextprotocol/subscriptionId"], 42)

        # A prompts change was never requested, so it must not be delivered.
        server.notify_list_changed("prompts")
        server.notify_list_changed("tools")

        deadline = time.time() + 5
        while len(received) < 2 and time.time() < deadline:
            time.sleep(0.02)

        methods = [m["method"] for m in received]
        self.assertIn("notifications/tools/list_changed", methods)
        self.assertNotIn("notifications/prompts/list_changed", methods)

        stop.set()
        http_server.stop()


class TestStdio(unittest.TestCase):
    def test_round_trip_over_a_real_subprocess(self):
        transport = StdioClientTransport(
            [sys.executable, "-m", "meridian.servers.marketdata"])
        try:
            client = Client(transport, server_label="marketdata")
            discovered = client.discover()
            self.assertIn(PROTOCOL_VERSION, discovered["supportedVersions"])

            names = [t["name"] for t in client.list_tools()]
            self.assertIn("get_reference_curve", names)

            result = client.call_tool("get_reference_curve", {"tenors": ["1Y", "5Y"]})
            self.assertEqual(set(result["structuredContent"]["curve"]), {"1Y", "5Y"})
        finally:
            transport.close()

    def test_stdout_carries_only_mcp_messages(self):
        """One stray print in a dependency corrupts the stream for everyone."""
        transport = StdioClientTransport(
            [sys.executable, "-m", "meridian.servers.fraud"])
        try:
            client = Client(transport, server_label="fraud")
            for _ in range(5):
                client.call_tool("screen_account", {"accountId": "ACC-1001"})
            # Every line the reader accepted parsed as JSON, or the calls above
            # would have timed out waiting for a response.
            self.assertGreater(client.stats.requests, 4)
        finally:
            transport.close()

    def test_server_exits_when_stdin_closes(self):
        transport = StdioClientTransport(
            [sys.executable, "-m", "meridian.servers.fraud"])
        client = Client(transport, server_label="fraud")
        client.discover()
        started = time.time()
        transport.close(timeout=6.0)
        self.assertLess(time.time() - started, 6.0,
                        "server had to be killed rather than exiting cleanly")
        self.assertIsNotNone(transport._proc.poll())


class TestConnectionPool(unittest.TestCase):
    """HTTP/1.1 carries one request per connection at a time."""

    def setUp(self):
        self.server = StreamableHttpServer(build(), port=0).start()

    def tearDown(self):
        self.server.stop()

    def test_sequential_calls_reuse_one_connection(self):
        transport = StreamableHttpClient(self.server.url)
        client = Client(transport, server_label="wire")
        for _ in range(5):
            client.call_tool("echo", {"text": "x"})
        self.assertEqual(transport.new_connections, 1)
        self.assertGreaterEqual(transport.reused_connections, 4)
        transport.close()

    def test_parallel_calls_to_one_server_do_not_serialise(self):
        """A single shared connection would make the host's fan-out sequential."""
        transport = StreamableHttpClient(self.server.url, max_connections=4)
        client = Client(transport, server_label="wire")
        client.call_tool("echo", {"text": "warm"})

        barrier = threading.Barrier(3, timeout=10)

        def call():
            barrier.wait()
            client.call_tool("echo", {"text": "x"})

        threads = [threading.Thread(target=call) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)

        # Three requests genuinely in flight together needed more than the one
        # warm connection, so the pool must have opened more.
        self.assertGreater(transport.new_connections, 1)
        transport.close()

    def test_the_pool_is_bounded(self):
        transport = StreamableHttpClient(self.server.url, max_connections=2)
        client = Client(transport, server_label="wire")
        threads = [threading.Thread(
            target=lambda: client.call_tool("echo", {"text": "x"}))
            for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertLessEqual(transport.new_connections, 2)
        transport.close()

    def test_close_empties_the_pool(self):
        transport = StreamableHttpClient(self.server.url)
        client = Client(transport, server_label="wire")
        client.call_tool("echo", {"text": "x"})
        transport.close()
        self.assertEqual(transport._idle, [])



if __name__ == "__main__":
    unittest.main(verbosity=2)
