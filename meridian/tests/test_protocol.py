"""Contract tests against MCP revision 2026-07-28.

These are the tests Chapter 15 argues every server should have. They assert on
the *wire*, not on the SDK's convenience objects, because the wire is what
another implementation has to interoperate with.
"""

from __future__ import annotations

import json
import unittest

from meridian.protocol import (
    CACHEABLE_METHODS,
    ClientCapabilities,
    Client,
    Implementation,
    InProcessTransport,
    McpError,
    ResultCache,
    Server,
    StateSealer,
    build_request_meta,
    elicit_form,
    encode,
    input_required,
    read_elicit,
    text_result,
    tool_error,
    validate_x_mcp_header,
)
from meridian.protocol import errors, jsonrpc
from meridian.protocol.meta import (
    KEY_CLIENT_CAPABILITIES,
    KEY_PROTOCOL_VERSION,
    KEY_SERVER_INFO,
    PROTOCOL_VERSION,
    is_reserved_meta_key,
)

ADD_SCHEMA = {
    "type": "object",
    "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
    "required": ["a", "b"],
    "additionalProperties": False,
}


def tiny_server(**kw) -> Server:
    server = Server("test-server", "1.0.0", **kw)

    @server.tool("add", "Add two numbers", ADD_SCHEMA)
    def add(ctx):
        return text_result(str(ctx.arguments["a"] + ctx.arguments["b"]))

    @server.resource("test://doc", "Doc", mime_type="text/plain",
                     ttl_ms=5000, cache_scope="public")
    def doc(ctx, uri):
        return "hello"

    @server.prompt("greet", "Greet somebody",
                   arguments=[{"name": "who", "required": True}])
    def greet(ctx, args):
        return [{"role": "user",
                 "content": {"type": "text", "text": f"Say hi to {args['who']}"}}]

    return server


def meta(caps: ClientCapabilities | None = None, version: str = PROTOCOL_VERSION) -> dict:
    return build_request_meta(caps or ClientCapabilities(),
                              Implementation("test-client", "1.0.0"),
                              protocol_version=version)


def request(method: str, params: dict | None = None, req_id=1,
            caps: ClientCapabilities | None = None,
            version: str = PROTOCOL_VERSION) -> dict:
    body = dict(params or {})
    body["_meta"] = meta(caps, version)
    return {"jsonrpc": "2.0", "id": req_id, "method": method, "params": body}


# ---------------------------------------------------------------------------


class TestStatelessEnvelope(unittest.TestCase):
    """Every request stands alone. There is no handshake to lean on."""

    def setUp(self):
        self.server = tiny_server()

    def test_request_without_meta_is_rejected(self):
        resp = self.server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        )
        self.assertEqual(resp["error"]["code"], errors.INVALID_PARAMS)

    def test_request_without_protocol_version_is_rejected(self):
        resp = self.server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/list",
            "params": {"_meta": {KEY_CLIENT_CAPABILITIES: {}}},
        })
        self.assertEqual(resp["error"]["code"], errors.INVALID_PARAMS)
        self.assertIn("protocolVersion", resp["error"]["message"])

    def test_request_without_client_capabilities_is_rejected(self):
        resp = self.server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/list",
            "params": {"_meta": {KEY_PROTOCOL_VERSION: PROTOCOL_VERSION}},
        })
        self.assertEqual(resp["error"]["code"], errors.INVALID_PARAMS)

    def test_unsupported_version_lists_what_is_supported(self):
        resp = self.server.handle(request("tools/list", version="1999-01-01"))
        self.assertEqual(resp["error"]["code"], errors.UNSUPPORTED_PROTOCOL_VERSION)
        self.assertEqual(resp["error"]["data"]["requested"], "1999-01-01")
        self.assertIn(PROTOCOL_VERSION, resp["error"]["data"]["supported"])

    def test_server_identifies_itself_in_every_result(self):
        resp = self.server.handle(request("tools/list"))
        self.assertEqual(resp["result"]["_meta"][KEY_SERVER_INFO]["name"], "test-server")

    def test_there_is_no_initialize_method(self):
        resp = self.server.handle(request("initialize"))
        self.assertEqual(resp["error"]["code"], errors.METHOD_NOT_FOUND)

    def test_two_requests_share_no_state(self):
        """The second request must not benefit from the first having happened."""
        first = self.server.handle(request("tools/list", req_id=1))
        second = self.server.handle(request("tools/list", req_id=2))
        self.assertEqual(first["result"]["tools"], second["result"]["tools"])
        # And a request with no _meta still fails, even after a good one.
        third = self.server.handle(
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}})
        self.assertIn("error", third)


class TestResultType(unittest.TestCase):
    def test_every_result_carries_result_type(self):
        server = tiny_server()
        for method, params in [
            ("server/discover", {}),
            ("tools/list", {}),
            ("tools/call", {"name": "add", "arguments": {"a": 1, "b": 2}}),
            ("resources/list", {}),
            ("resources/read", {"uri": "test://doc"}),
            ("prompts/list", {}),
            ("prompts/get", {"name": "greet", "arguments": {"who": "you"}}),
        ]:
            with self.subTest(method=method):
                resp = server.handle(request(method, params))
                self.assertEqual(resp["result"]["resultType"], "complete")

    def test_absent_result_type_reads_as_complete(self):
        """Servers on 2025-11-25 omit the field. Clients must not care."""
        self.assertEqual(
            jsonrpc.result_type({"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}),
            "complete",
        )


class TestErrorCodes(unittest.TestCase):
    def test_reserved_range_allocations(self):
        self.assertEqual(errors.HEADER_MISMATCH, -32020)
        self.assertEqual(errors.MISSING_REQUIRED_CLIENT_CAPABILITY, -32021)
        self.assertEqual(errors.UNSUPPORTED_PROTOCOL_VERSION, -32022)

    def test_resource_not_found_is_invalid_params_now(self):
        server = tiny_server()
        resp = server.handle(request("resources/read", {"uri": "test://nope"}))
        self.assertEqual(resp["error"]["code"], errors.INVALID_PARAMS)
        self.assertNotEqual(resp["error"]["code"], -32002)

    def test_retired_codes_are_never_emitted(self):
        server = tiny_server()
        seen = set()
        for method, params in [("resources/read", {"uri": "x://y"}),
                               ("tools/call", {"name": "nope"}),
                               ("prompts/get", {"name": "nope"})]:
            resp = server.handle(request(method, params))
            seen.add(resp["error"]["code"])
        self.assertNotIn(-32002, seen)
        self.assertNotIn(-32042, seen)

    def test_unknown_method_is_method_not_found(self):
        resp = tiny_server().handle(request("nonsense/method"))
        self.assertEqual(resp["error"]["code"], errors.METHOD_NOT_FOUND)

    def test_http_status_mapping(self):
        self.assertEqual(errors.HTTP_STATUS[errors.METHOD_NOT_FOUND], 404)
        self.assertEqual(errors.HTTP_STATUS[errors.HEADER_MISMATCH], 400)
        self.assertEqual(errors.HTTP_STATUS[errors.UNSUPPORTED_PROTOCOL_VERSION], 400)


class TestJsonRpcFraming(unittest.TestCase):
    def test_null_id_is_rejected(self):
        resp = tiny_server().handle(
            {"jsonrpc": "2.0", "id": None, "method": "tools/list", "params": {}})
        self.assertEqual(resp["error"]["code"], errors.INVALID_REQUEST)

    def test_boolean_id_is_rejected(self):
        resp = tiny_server().handle(
            {"jsonrpc": "2.0", "id": True, "method": "tools/list", "params": {}})
        self.assertEqual(resp["error"]["code"], errors.INVALID_REQUEST)

    def test_notification_gets_no_response(self):
        self.assertIsNone(tiny_server().handle(
            {"jsonrpc": "2.0", "method": "notifications/cancelled",
             "params": {"requestId": 1}}))

    def test_encoded_messages_contain_no_newlines(self):
        """stdio framing depends on this and nothing enforces it but us."""
        wire = encode({"jsonrpc": "2.0", "id": 1,
                       "result": {"text": "line one\nline two"}})
        self.assertNotIn("\n", wire)

    def test_sse_comments_are_ignored(self):
        raw = [b":\r\n", b"data: {\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{}}\n", b"\n"]
        msgs = list(jsonrpc.iter_sse(iter(raw)))
        self.assertEqual(len(msgs), 1)


class TestCaching(unittest.TestCase):
    def test_all_six_cacheable_methods_carry_hints(self):
        server = tiny_server()
        for method, params in [
            ("server/discover", {}), ("tools/list", {}), ("prompts/list", {}),
            ("resources/list", {}), ("resources/templates/list", {}),
            ("resources/read", {"uri": "test://doc"}),
        ]:
            with self.subTest(method=method):
                result = server.handle(request(method, params))["result"]
                self.assertIn("ttlMs", result, f"{method} is missing ttlMs")
                self.assertIn("cacheScope", result, f"{method} is missing cacheScope")
                self.assertGreaterEqual(result["ttlMs"], 0)
                self.assertIn(result["cacheScope"], ("public", "private"))

    def test_cacheable_method_set_matches_the_spec(self):
        self.assertEqual(CACHEABLE_METHODS, {
            "server/discover", "tools/list", "prompts/list",
            "resources/list", "resources/templates/list", "resources/read",
        })

    def test_hit_then_expiry(self):
        clock = [1000.0]
        cache = ResultCache(clock=lambda: clock[0])
        result = {"resultType": "complete", "tools": [], "ttlMs": 5000,
                  "cacheScope": "public"}
        cache.put("s", "tools/list", {}, result)
        self.assertIsNotNone(cache.get("s", "tools/list", {}))
        clock[0] += 4.9
        self.assertIsNotNone(cache.get("s", "tools/list", {}))
        clock[0] += 0.2
        self.assertIsNone(cache.get("s", "tools/list", {}))

    def test_zero_ttl_is_never_stored(self):
        cache = ResultCache()
        stored = cache.put("s", "tools/list", {},
                           {"resultType": "complete", "ttlMs": 0, "cacheScope": "public"})
        self.assertFalse(stored)

    def test_absent_ttl_is_never_stored(self):
        cache = ResultCache()
        self.assertFalse(cache.put("s", "tools/list", {},
                                   {"resultType": "complete", "cacheScope": "public"}))

    def test_private_entries_are_keyed_by_auth_context(self):
        cache = ResultCache()
        result = {"resultType": "complete", "contents": [{"uri": "u", "text": "alice"}],
                  "ttlMs": 60000, "cacheScope": "private"}
        cache.put("s", "resources/read", {"uri": "u"}, result, auth_context="alice")
        self.assertIsNotNone(cache.get("s", "resources/read", {"uri": "u"}, "alice"))
        self.assertIsNone(cache.get("s", "resources/read", {"uri": "u"}, "bob"))

    def test_mrtr_retries_are_not_cacheable(self):
        cache = ResultCache()
        params = {"uri": "u", "inputResponses": {"k": {"action": "accept"}}}
        self.assertFalse(cache.put("s", "resources/read", params,
                                   {"resultType": "complete", "ttlMs": 9999,
                                    "cacheScope": "private"}))
        self.assertIsNone(cache.get("s", "resources/read", params))

    def test_interim_results_are_not_cacheable(self):
        cache = ResultCache()
        self.assertFalse(cache.put("s", "tools/list", {},
                                   {"resultType": "input_required", "ttlMs": 9999}))

    def test_notification_invalidates_a_fresh_entry(self):
        cache = ResultCache()
        cache.put("s", "tools/list", {},
                  {"resultType": "complete", "tools": [], "ttlMs": 999_999,
                   "cacheScope": "public"})
        self.assertIsNotNone(cache.get("s", "tools/list", {}))
        removed = cache.invalidate_method("s", "tools/list")
        self.assertEqual(removed, 1)
        self.assertIsNone(cache.get("s", "tools/list", {}))

    def test_different_cursors_are_different_entries(self):
        cache = ResultCache()
        for cursor in ("", "50"):
            params = {"cursor": cursor} if cursor else {}
            cache.put("s", "tools/list", params,
                      {"resultType": "complete", "tools": [cursor], "ttlMs": 60000,
                       "cacheScope": "public"})
        self.assertEqual(cache.get("s", "tools/list", {})["tools"], [""])
        self.assertEqual(cache.get("s", "tools/list", {"cursor": "50"})["tools"], ["50"])


class TestDeterministicOrdering(unittest.TestCase):
    def test_tools_list_order_is_stable(self):
        """Unstable ordering silently destroys the provider's prompt cache."""
        server = tiny_server()
        for i in range(6):
            server.add_tool(_noop_tool(f"tool_{i}"))
        orders = [
            [t["name"] for t in server.handle(request("tools/list", req_id=i))["result"]["tools"]]
            for i in range(5)
        ]
        self.assertEqual(len(set(map(tuple, orders))), 1)


def _noop_tool(name: str):
    from meridian.protocol import Tool
    return Tool(name=name, description="noop",
                input_schema={"type": "object", "additionalProperties": False},
                handler=lambda ctx: text_result("ok"))


class TestPagination(unittest.TestCase):
    def test_pages_cover_everything_exactly_once(self):
        server = tiny_server(page_size=7)
        for i in range(30):
            server.add_tool(_noop_tool(f"t{i:02d}"))

        seen, cursor, pages = [], None, 0
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = server.handle(request("tools/list", params))["result"]
            seen.extend(t["name"] for t in result["tools"])
            pages += 1
            cursor = result.get("nextCursor")
            if not cursor:
                break
            self.assertLess(pages, 20, "pagination did not terminate")

        self.assertEqual(len(seen), 31)              # 30 plus the built-in `add`
        self.assertEqual(len(seen), len(set(seen)))  # no duplicates

    def test_bad_cursor_is_invalid_params(self):
        resp = tiny_server().handle(request("tools/list", {"cursor": "banana"}))
        self.assertEqual(resp["error"]["code"], errors.INVALID_PARAMS)


class TestSchemaValidation(unittest.TestCase):
    """Argument validation failures are tool execution errors (SEP-1303).

    A protocol error is caught by the client and never reaches the model, so a
    model that sent the wrong type could never learn that it had. These have to
    come back as `isError` results with a message worth reading.
    """

    def call(self, arguments):
        return tiny_server().handle(
            request("tools/call", {"name": "add", "arguments": arguments}))

    def test_missing_required_argument(self):
        resp = self.call({"a": 1})
        self.assertNotIn("error", resp)
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("b", resp["result"]["content"][0]["text"])

    def test_wrong_type(self):
        resp = self.call({"a": "x", "b": 2})
        self.assertNotIn("error", resp)
        self.assertTrue(resp["result"]["isError"])

    def test_additional_properties_false_is_enforced(self):
        resp = self.call({"a": 1, "b": 2, "c": 3})
        self.assertNotIn("error", resp)
        self.assertTrue(resp["result"]["isError"])

    def test_unknown_tool_is_still_a_protocol_error(self):
        """The model cannot fix this by changing arguments, so it stays -32602."""
        resp = tiny_server().handle(request("tools/call", {"name": "nope"}))
        self.assertEqual(resp["error"]["code"], errors.INVALID_PARAMS)

    def test_malformed_request_is_still_a_protocol_error(self):
        resp = tiny_server().handle(request("tools/call", {"name": 42}))
        self.assertEqual(resp["error"]["code"], errors.INVALID_PARAMS)

    def test_remote_ref_is_not_dereferenced(self):
        from meridian.protocol import validate_against_schema
        with self.assertRaises(McpError):
            validate_against_schema({}, {"$ref": "https://evil.example/schema.json"})

    def test_boolean_is_not_an_integer(self):
        from meridian.protocol import validate_against_schema
        with self.assertRaises(McpError):
            validate_against_schema(True, {"type": "integer"})


class TestMrtr(unittest.TestCase):
    def test_input_required_needs_one_of_the_two_fields(self):
        with self.assertRaises(ValueError):
            input_required()

    def test_shape_of_an_input_required_result(self):
        result = input_required(
            input_requests={"k": elicit_form("why?", {"type": "object"})},
            request_state="opaque",
        )
        self.assertEqual(result["resultType"], "input_required")
        self.assertEqual(result["inputRequests"]["k"]["method"], "elicitation/create")
        self.assertEqual(result["requestState"], "opaque")

    def test_retry_uses_a_different_id(self):
        """The retry is an independent request, so it must not reuse the id."""
        server = Server("mrtr", "1.0.0")

        @server.tool("ask", "Asks once", {"type": "object"})
        def ask(ctx):
            answer = read_elicit(ctx.input_responses, "who")
            if answer is None:
                return input_required(
                    input_requests={"who": elicit_form("Who?", {"type": "object"})},
                    request_state="s1")
            return text_result(f"hello {answer.content.get('name')}")

        caps = ClientCapabilities(elicitation={"form": {}})
        from meridian.protocol import ScriptedInput
        client = Client(InProcessTransport(server), capabilities=caps,
                        input_provider=ScriptedInput(
                            {"who": {"action": "accept", "content": {"name": "ada"}}}))
        ids = []
        original_send = client.transport.send

        def spy(message, **kw):
            ids.append(message["id"])
            return original_send(message, **kw)

        client.transport.send = spy
        result = client.call_tool("ask", {})
        self.assertEqual(result["content"][0]["text"], "hello ada")
        self.assertEqual(len(ids), 2)
        self.assertNotEqual(ids[0], ids[1])

    def test_server_must_not_ask_for_undeclared_capability(self):
        from meridian.servers.risk import build_server
        server = build_server()
        # No elicitation capability declared.
        resp = server.handle(request(
            "tools/call",
            {"name": "assess_account_risk",
             "arguments": {"accountId": "ACC-1000", "exposureUsd": 9_000_000}},
            caps=ClientCapabilities(),
        ))
        result = resp["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertTrue(result["isError"])
        self.assertIn("cannot collect an approval", result["content"][0]["text"])


class TestStateSealer(unittest.TestCase):
    def setUp(self):
        self.sealer = StateSealer(secret=b"k" * 32, ttl_seconds=60)

    def test_round_trip(self):
        token = self.sealer.seal({"a": 1})
        self.assertEqual(self.sealer.open(token), {"a": 1})

    def test_tampering_is_detected(self):
        token = self.sealer.seal({"amount": 100})
        head, payload, mac = token.split(".")
        forged = f"{head}.{payload[:-2]}XY.{mac}"
        with self.assertRaises(McpError):
            self.sealer.open(forged)

    def test_expiry(self):
        token = self.sealer.seal({"a": 1}, ttl_seconds=10, now=1000.0)
        self.assertEqual(self.sealer.open(token, now=1009.0), {"a": 1})
        with self.assertRaises(McpError):
            self.sealer.open(token, now=1011.0)

    def test_principal_binding_blocks_cross_user_replay(self):
        token = self.sealer.seal({"txn": "T1"}, principal="alice")
        self.assertEqual(self.sealer.open(token, principal="alice"), {"txn": "T1"})
        with self.assertRaises(McpError):
            self.sealer.open(token, principal="bob")

    def test_request_binding_blocks_cross_request_replay(self):
        params = {"name": "approve_refund", "arguments": {"amount": 10}}
        token = self.sealer.seal({"ok": True}, method="tools/call", params=params)
        self.assertEqual(
            self.sealer.open(token, method="tools/call", params=params), {"ok": True})
        other = {"name": "transfer_funds", "arguments": {"amount": 10}}
        with self.assertRaises(McpError):
            self.sealer.open(token, method="tools/call", params=other)

    def test_a_different_secret_cannot_open_it(self):
        token = self.sealer.seal({"a": 1})
        with self.assertRaises(McpError):
            StateSealer(secret=b"j" * 32).open(token)


class TestMetaKeys(unittest.TestCase):
    def test_reserved_prefixes(self):
        for key in ["io.modelcontextprotocol/x", "dev.mcp/x",
                    "org.modelcontextprotocol.api/x", "com.mcp.tools/x"]:
            self.assertTrue(is_reserved_meta_key(key), key)

    def test_second_label_is_what_counts(self):
        self.assertFalse(is_reserved_meta_key("com.example.mcp/x"))
        self.assertFalse(is_reserved_meta_key("com.example/x"))

    def test_trace_context_keys_are_reserved_by_exception(self):
        for key in ("traceparent", "tracestate", "baggage"):
            self.assertTrue(is_reserved_meta_key(key))


class TestXMcpHeader(unittest.TestCase):
    def test_valid_annotation_passes(self):
        self.assertEqual(validate_x_mcp_header({
            "type": "object",
            "properties": {"region": {"type": "string", "x-mcp-header": "Region"}},
        }), [])

    def test_number_type_is_rejected(self):
        problems = validate_x_mcp_header({
            "type": "object",
            "properties": {"n": {"type": "number", "x-mcp-header": "N"}},
        })
        self.assertEqual(len(problems), 1)

    def test_duplicate_names_are_rejected(self):
        problems = validate_x_mcp_header({
            "type": "object",
            "properties": {
                "a": {"type": "string", "x-mcp-header": "Tenant"},
                "b": {"type": "string", "x-mcp-header": "tenant"},
            },
        })
        self.assertTrue(any("duplicates" in p for p in problems))

    def test_inside_an_array_is_not_statically_reachable(self):
        problems = validate_x_mcp_header({
            "type": "object",
            "properties": {
                "items": {"type": "array",
                          "items": {"type": "object",
                                    "properties": {
                                        "r": {"type": "string",
                                              "x-mcp-header": "R"}}}},
            },
        })
        self.assertTrue(any("statically reachable" in p for p in problems))

    def test_nested_objects_are_reachable(self):
        self.assertEqual(validate_x_mcp_header({
            "type": "object",
            "properties": {
                "routing": {"type": "object",
                            "properties": {"region": {"type": "string",
                                                      "x-mcp-header": "Region"}}},
            },
        }), [])


class TestToolErrorsVsProtocolErrors(unittest.TestCase):
    """The distinction that decides whether a model can recover."""

    def test_unknown_tool_is_a_protocol_error(self):
        resp = tiny_server().handle(request("tools/call", {"name": "nope"}))
        self.assertIn("error", resp)

    def test_bad_business_input_is_a_tool_execution_error(self):
        from meridian.servers.risk import build_server
        resp = build_server().handle(request(
            "tools/call",
            {"name": "assess_account_risk", "arguments": {"accountId": "ACC-9999"}}))
        self.assertNotIn("error", resp)
        self.assertTrue(resp["result"]["isError"])
        # And it must be actionable, not just "invalid".
        self.assertIn("ACC-1000", resp["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
