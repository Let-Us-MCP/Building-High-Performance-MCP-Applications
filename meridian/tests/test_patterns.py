"""Tests for the patterns the later chapters describe.

These exist because a listing in a book that is not backed by running code is a
sketch, and the book claims otherwise. `tools/check_listings.py` enforces the
claim; this file makes it true.
"""

from __future__ import annotations

import unittest

from meridian import ops
from meridian.host.delegation import (
    DEPTH_KEY,
    DOLLARS_KEY,
    MAX_DELEGATION_DEPTH,
    TOKENS_KEY,
    Budget,
    CircuitBreaker,
    inherit_budget,
)
from meridian.protocol import (
    Client,
    ClientCapabilities,
    InProcessTransport,
    McpError,
    build_request_meta,
)
from meridian.protocol.meta import parse_request_context
from meridian.servers import fraud, risk, scoped


def ctx_with(meta_extra: dict):
    meta = build_request_meta(ClientCapabilities())
    meta.update(meta_extra)
    return parse_request_context("tools/call", {"_meta": meta})


class TestBudgetInheritance(unittest.TestCase):
    def test_absent_budget_defaults_small(self):
        """An agent with no budget must assume the smallest, never unlimited."""
        budget = inherit_budget(ctx_with({}))
        self.assertEqual(budget.depth, 1)
        self.assertLess(budget.tokens, 100_000)
        self.assertLess(budget.dollars, 1.0)

    def test_depth_increments(self):
        self.assertEqual(inherit_budget(ctx_with({DEPTH_KEY: 1})).depth, 2)

    def test_depth_is_refused_before_any_work(self):
        with self.assertRaises(McpError):
            inherit_budget(ctx_with({DEPTH_KEY: MAX_DELEGATION_DEPTH}))

    def test_steps_shrink_with_depth(self):
        shallow = inherit_budget(ctx_with({DEPTH_KEY: 0})).steps
        deep = inherit_budget(ctx_with({DEPTH_KEY: 2})).steps
        self.assertLess(deep, shallow)
        self.assertGreaterEqual(deep, 1)

    def test_budget_is_read_from_the_caller(self):
        budget = inherit_budget(ctx_with({TOKENS_KEY: 500, DOLLARS_KEY: 0.02}))
        self.assertEqual(budget.tokens, 500)
        self.assertAlmostEqual(budget.dollars, 0.02)

    def test_spending_decrements(self):
        """Budgets are remaining, not allocated, or fan-out multiplies them."""
        budget = Budget(depth=1, tokens=1000, dollars=1.0, steps=4)
        after = budget.spend(400, 0.4)
        self.assertEqual(after.tokens, 600)
        self.assertAlmostEqual(after.dollars, 0.6)

    def test_spending_never_goes_negative(self):
        after = Budget(depth=1, tokens=10, dollars=0.01, steps=2).spend(999, 9.9)
        self.assertEqual(after.tokens, 0)
        self.assertEqual(after.dollars, 0.0)

    def test_meta_round_trips(self):
        budget = Budget(depth=2, tokens=750, dollars=0.05, steps=4)
        child = inherit_budget(ctx_with(budget.to_meta()))
        self.assertEqual(child.depth, 3)
        self.assertEqual(child.tokens, 750)


class TestCircuitBreaker(unittest.TestCase):
    def test_closed_allows(self):
        self.assertTrue(CircuitBreaker().allows(now=0.0))

    def test_opens_after_threshold(self):
        cb = CircuitBreaker(threshold=3, cooldown=30.0)
        for _ in range(3):
            cb.record(False, now=0.0)
        self.assertFalse(cb.allows(now=1.0))
        self.assertEqual(cb.state, "open")

    def test_success_resets(self):
        cb = CircuitBreaker(threshold=3)
        cb.record(False, now=0.0)
        cb.record(False, now=0.0)
        cb.record(True, now=0.0)
        self.assertEqual(cb.failures, 0)
        self.assertTrue(cb.allows(now=0.0))

    def test_half_open_lets_exactly_one_probe_through(self):
        cb = CircuitBreaker(threshold=2, cooldown=10.0)
        cb.record(False, now=0.0)
        cb.record(False, now=0.0)
        self.assertFalse(cb.allows(now=5.0))         # still open
        self.assertTrue(cb.allows(now=11.0))          # the probe
        cb.record(False, now=11.0)                    # probe failed
        self.assertFalse(cb.allows(now=11.5))         # open again at once


class TestScopedServer(unittest.TestCase):
    def setUp(self):
        self.server = scoped.build_scoped_server()

    def client(self, scopes, accounts=("ACC-1000",)):
        auth = {"sub": "u1", "scopes": list(scopes), "accounts": list(accounts)}
        return Client(InProcessTransport(self.server, auth=auth))

    def test_catalogue_is_filtered_by_scope(self):
        reader = self.client(["risk:read"])
        names = [t["name"] for t in reader.list_tools()]
        self.assertIn("assess_account_risk", names)
        self.assertNotIn("underwrite_loan", names)

    def test_writer_sees_more(self):
        writer = self.client(["risk:read", "risk:write"])
        names = [t["name"] for t in writer.list_tools()]
        self.assertIn("underwrite_loan", names)

    def test_filtering_is_a_menu_and_the_handler_is_the_lock(self):
        """A caller can invoke a tool that was never listed."""
        reader = self.client(["risk:read"])
        with self.assertRaises(McpError):
            reader.call_tool("underwrite_loan",
                             {"accountId": "ACC-1000", "amountUsd": 5000})

    def test_row_level_access_hides_other_accounts(self):
        reader = self.client(["risk:read"], accounts=["ACC-1000"])
        allowed = reader.call_tool("assess_account_risk",
                                   {"accountId": "ACC-1000"})
        self.assertIn("structuredContent", allowed)

        denied = reader.call_tool("assess_account_risk",
                                  {"accountId": "ACC-1001"})
        self.assertTrue(denied["isError"])

    def test_forbidden_and_nonexistent_are_indistinguishable(self):
        """Otherwise the error messages enumerate which accounts are real."""
        reader = self.client(["risk:read"], accounts=["ACC-1000"])
        # ACC-1001 exists but is not this principal's; ACC-9999 does not exist.
        forbidden = reader.call_tool("assess_account_risk",
                                     {"accountId": "ACC-1001"})
        missing = reader.call_tool("assess_account_risk",
                                   {"accountId": "ACC-9999"})
        # Same wording either side of the account id, so the response reveals
        # nothing about which identifiers are real.
        self.assertEqual(forbidden["content"][0]["text"].replace("ACC-1001", "X"),
                         missing["content"][0]["text"].replace("ACC-9999", "X"))
        self.assertTrue(forbidden["isError"] and missing["isError"])

    def test_ordering_survives_filtering(self):
        reader = self.client(["risk:read"])
        first = [t["name"] for t in reader.list_tools()]
        reader.cache.clear()
        second = [t["name"] for t in reader.list_tools()]
        self.assertEqual(first, second)
        self.assertEqual(first, sorted(first))


class TestBasketHandles(unittest.TestCase):
    def setUp(self):
        self.server = scoped.build_basket_server()

    def client(self, sub="alice"):
        return Client(InProcessTransport(self.server, auth={"sub": sub}))

    def test_handle_round_trip(self):
        c = self.client()
        created = c.call_tool("create_basket", {})
        basket_id = created["structuredContent"]["basket_id"]
        added = c.call_tool("add_item", {"basket_id": basket_id, "sku": "X-1"})
        self.assertEqual(added["structuredContent"]["itemCount"], 1)

    def test_handle_is_opaque(self):
        c = self.client()
        basket_id = c.call_tool("create_basket", {})["structuredContent"]["basket_id"]
        self.assertTrue(basket_id.startswith("bsk_"))
        self.assertNotIn("alice", basket_id)

    def test_another_principal_cannot_use_the_handle(self):
        """A handle is a name, not a capability."""
        basket_id = (self.client("alice").call_tool("create_basket", {})
                     ["structuredContent"]["basket_id"])
        stolen = self.client("bob").call_tool(
            "add_item", {"basket_id": basket_id, "sku": "X-1"})
        self.assertTrue(stolen["isError"])

    def test_unknown_handle_error_names_the_recovery(self):
        c = self.client()
        result = c.call_tool("add_item",
                             {"basket_id": "bsk_000000000000", "sku": "X"})
        self.assertTrue(result["isError"])
        self.assertIn("create_basket", result["content"][0]["text"])


class TestPromptContract(unittest.TestCase):
    """Structural prompt tests: the cheap layer that catches most regressions."""

    def setUp(self):
        self.server = risk.build_server()

    def request(self, name, arguments):
        return {"jsonrpc": "2.0", "id": 1, "method": "prompts/get",
                "params": {"name": name, "arguments": arguments,
                           "_meta": build_request_meta(ClientCapabilities())}}

    def test_prompt_rejects_missing_required_argument(self):
        resp = self.server.handle(self.request("credit-review", {}))
        self.assertEqual(resp["error"]["code"], -32602)

    def test_prompt_renders_with_required_argument(self):
        resp = self.server.handle(
            self.request("credit-review", {"accountId": "ACC-1042"}))
        text = resp["result"]["messages"][0]["content"]["text"]
        self.assertIn("ACC-1042", text)

    def test_prompt_names_the_tool_it_expects(self):
        """A blueprint that does not name its tool costs a planning step."""
        resp = self.server.handle(
            self.request("credit-review", {"accountId": "ACC-1042"}))
        text = resp["result"]["messages"][0]["content"]["text"]
        self.assertIn("assess_account_risk", text)


class TestReauth(unittest.TestCase):
    def test_retry_is_bounded(self):
        """An unfixable 401 must not become a denial of service."""
        server = risk.build_server()
        client = Client(InProcessTransport(server))
        attempts = {"n": 0}

        def always_401(name, arguments):
            attempts["n"] += 1
            raise McpError(-32603, "401 Unauthorized")

        client.call_tool = always_401
        client._refresh_hook = lambda: "new-token"
        with self.assertRaises(McpError):
            client.call_with_reauth("assess_account_risk", {})
        self.assertEqual(attempts["n"], 2)

    def test_non_auth_errors_are_not_retried(self):
        client = Client(InProcessTransport(risk.build_server()))
        attempts = {"n": 0}

        def boom(name, arguments):
            attempts["n"] += 1
            raise McpError(-32602, "Unknown tool: nope")

        client.call_tool = boom
        with self.assertRaises(McpError):
            client.call_with_reauth("nope", {})
        self.assertEqual(attempts["n"], 1)


class TestOps(unittest.TestCase):
    def test_healthy_server_passes(self):
        ok, detail = ops.healthy(risk.build_server())
        self.assertTrue(ok)
        self.assertIn("tools", detail["capabilities"])

    def test_health_check_uses_discover_not_a_port(self):
        """A broken router fails health even though the object exists."""
        server = fraud.build_server()
        server._methods.pop("server/discover")
        ok, detail = ops.healthy(server)
        self.assertFalse(ok)
        self.assertIn("reason", detail)

    def test_deep_check_calls_a_real_tool(self):
        ok, detail = ops.deep_check(risk.build_server(),
                                    tool="assess_account_risk",
                                    arguments={"accountId": "ACC-1000"})
        self.assertTrue(ok)
        self.assertGreaterEqual(detail["ms"], 0.0)

    def test_deep_check_notices_a_bad_fixture(self):
        ok, _ = ops.deep_check(risk.build_server(),
                               tool="assess_account_risk",
                               arguments={"accountId": "ACC-9999"})
        self.assertFalse(ok)

    def test_capacity_plans_for_the_burst(self):
        cap = ops.Capacity(tasks_per_second=100, round_trips_per_task=3,
                           fan_out_width=3)
        self.assertEqual(cap.mean_rps, 300)
        self.assertEqual(cap.burst_concurrency, 900)


if __name__ == "__main__":
    unittest.main(verbosity=2)
