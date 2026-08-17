"""Dual-era tests: one server, two protocol generations, no confusion between them."""

from __future__ import annotations

import unittest

from meridian.protocol import (
    Client,
    ClientCapabilities,
    InProcessTransport,
    build_request_meta,
    text_result,
)
from meridian.protocol import errors
from meridian.protocol.legacy import DEFAULT_LEGACY_VERSION, LegacyBridge
from meridian.protocol.meta import PROTOCOL_VERSION
from meridian.servers import risk


def bridged():
    return LegacyBridge(risk.build_server())


def legacy(method: str, params: dict | None = None, req_id: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}


HANDSHAKE = legacy("initialize", {
    "protocolVersion": "2025-06-18",
    "capabilities": {"elicitation": {}},
    "clientInfo": {"name": "legacy-client", "version": "0.9"},
})


class TestEraSelection(unittest.TestCase):
    def test_initialize_is_answered(self):
        resp = bridged().handle(HANDSHAKE)
        self.assertEqual(resp["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(resp["result"]["serverInfo"]["name"], "meridian-risk")
        self.assertIn("tools", resp["result"]["capabilities"])

    def test_unknown_legacy_version_falls_back_to_the_default(self):
        resp = bridged().handle(legacy("initialize", {"protocolVersion": "1999-01-01"}))
        self.assertEqual(resp["result"]["protocolVersion"], DEFAULT_LEGACY_VERSION)

    def test_after_handshake_bare_requests_work(self):
        bridge = bridged()
        bridge.handle(HANDSHAKE)
        resp = bridge.handle(legacy("tools/list", req_id=2))
        names = [t["name"] for t in resp["result"]["tools"]]
        self.assertIn("assess_account_risk", names)

    def test_before_handshake_bare_requests_get_the_modern_error(self):
        """A modern client that forgot `_meta` must be told to fix it, not
        silently served under legacy rules."""
        resp = bridged().handle(legacy("tools/list"))
        self.assertEqual(resp["error"]["code"], errors.INVALID_PARAMS)

    def test_modern_requests_bypass_the_bridge_entirely(self):
        bridge = bridged()
        params = {"_meta": build_request_meta(ClientCapabilities())}
        resp = bridge.handle(legacy("tools/list", params, req_id=3))
        self.assertEqual(resp["result"]["resultType"], "complete")
        self.assertEqual(bridge.era, "modern")

    def test_modern_still_works_even_after_a_handshake(self):
        bridge = bridged()
        bridge.handle(HANDSHAKE)
        params = {"_meta": build_request_meta(ClientCapabilities())}
        resp = bridge.handle(legacy("tools/list", params, req_id=4))
        self.assertEqual(resp["result"]["resultType"], "complete")


class TestLegacyTranslation(unittest.TestCase):
    def setUp(self):
        self.bridge = bridged()
        self.bridge.handle(HANDSHAKE)

    def test_result_type_is_stripped_for_legacy_clients(self):
        resp = self.bridge.handle(legacy("tools/list", req_id=2))
        self.assertNotIn("resultType", resp["result"])

    def test_caching_hints_are_kept_because_they_are_additive(self):
        resp = self.bridge.handle(legacy("tools/list", req_id=2))
        self.assertIn("ttlMs", resp["result"])

    def test_ping_is_answered_even_though_the_revision_removed_it(self):
        resp = self.bridge.handle(legacy("ping", req_id=5))
        self.assertEqual(resp["result"], {})

    def test_removed_rpcs_are_acknowledged_not_errored(self):
        for method in ("resources/subscribe", "resources/unsubscribe",
                       "logging/setLevel"):
            with self.subTest(method=method):
                resp = self.bridge.handle(legacy(method, {"uri": "x"}, req_id=6))
                self.assertNotIn("error", resp)

    def test_initialized_notification_produces_no_response(self):
        self.assertIsNone(self.bridge.handle(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_a_tool_call_round_trips(self):
        resp = self.bridge.handle(legacy(
            "tools/call",
            {"name": "assess_account_risk", "arguments": {"accountId": "ACC-1042"}},
            req_id=7))
        self.assertEqual(resp["result"]["structuredContent"]["accountId"], "ACC-1042")

    def test_mrtr_is_reported_as_needing_a_newer_client(self):
        """MRTR has no legacy equivalent, so say so rather than send a shape
        the client cannot parse."""
        resp = self.bridge.handle(legacy(
            "tools/call",
            {"name": "assess_account_risk",
             "arguments": {"accountId": "ACC-1042", "exposureUsd": 9_000_000}},
            req_id=8))
        self.assertIn("error", resp)
        self.assertIn("2026-07-28", resp["error"]["message"])

    def test_client_capabilities_survive_the_handshake(self):
        self.assertIn("elicitation", self.bridge.client_capabilities)


class TestBridgeIsTransparent(unittest.TestCase):
    def test_a_modern_client_sees_no_difference(self):
        direct = Client(InProcessTransport(risk.build_server()), server_label="a")
        through = Client(InProcessTransport(bridged()), server_label="b")
        self.assertEqual(
            [t["name"] for t in direct.list_tools()],
            [t["name"] for t in through.list_tools()],
        )
        self.assertEqual(
            direct.call_tool("assess_account_risk", {"accountId": "ACC-1007"}
                             )["structuredContent"],
            through.call_tool("assess_account_risk", {"accountId": "ACC-1007"}
                              )["structuredContent"],
        )

    def test_supported_versions_advertise_both_eras(self):
        versions = bridged().supported_versions
        self.assertIn(PROTOCOL_VERSION, versions)
        self.assertIn("2025-06-18", versions)


if __name__ == "__main__":
    unittest.main(verbosity=2)
