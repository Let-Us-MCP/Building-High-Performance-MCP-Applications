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


class TestCompletions(unittest.TestCase):
    """completion/complete: the method that makes slash commands usable."""

    def setUp(self):
        self.server = risk.build_server()

    def complete(self, argument, value, context=None):
        params = {
            "ref": {"type": "ref/prompt", "name": "credit-review"},
            "argument": {"name": argument, "value": value},
            "_meta": build_request_meta(ClientCapabilities()),
        }
        if context is not None:
            params["context"] = {"arguments": context}
        resp = self.server.handle({"jsonrpc": "2.0", "id": 1,
                                   "method": "completion/complete",
                                   "params": params})
        return resp["result"]["completion"]

    def test_capability_is_declared_when_completers_exist(self):
        caps = self.server.capabilities().to_json()
        self.assertIn("completions", caps)

    def test_prefix_match(self):
        out = self.complete("accountId", "ACC-10")
        self.assertTrue(out["values"])
        self.assertTrue(all(v.startswith("ACC-10") for v in out["values"]))

    def test_matching_is_case_insensitive(self):
        self.assertEqual(self.complete("accountId", "acc-10")["values"],
                         self.complete("accountId", "ACC-10")["values"])

    def test_unknown_argument_is_empty_not_an_error(self):
        """A client asks about everything the user types."""
        out = self.complete("nosuchargument", "x")
        self.assertEqual(out["values"], [])
        self.assertFalse(out["hasMore"])

    def test_values_are_capped_and_total_is_honest(self):
        """A capped list must not look like a complete one."""
        server = self.server
        server._completers[("prompt", "credit-review", "many")] = (
            lambda value, filled: [f"v{i}" for i in range(250)])
        out = self.complete("many", "")
        self.assertEqual(len(out["values"]), server.MAX_COMPLETION_VALUES)
        self.assertEqual(out["total"], 250)
        self.assertTrue(out["hasMore"])

    def test_context_is_passed_to_the_completer(self):
        seen = {}
        self.server._completers[("prompt", "credit-review", "probe")] = (
            lambda value, filled: seen.update(filled) or ["ok"])
        self.complete("probe", "", context={"accountId": "ACC-1000"})
        self.assertEqual(seen, {"accountId": "ACC-1000"})

    def test_bad_ref_type_is_invalid_params(self):
        resp = self.server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "completion/complete",
            "params": {"ref": {"type": "ref/nonsense"},
                       "argument": {"name": "accountId", "value": ""},
                       "_meta": build_request_meta(ClientCapabilities())}})
        self.assertEqual(resp["error"]["code"], -32602)


class TestTaskNotifications(unittest.TestCase):
    """Push instead of poll: notifications/tasks over subscriptions/listen."""

    def setUp(self):
        from meridian.protocol import tasks as tasks_ext
        from meridian.protocol.subscriptions import SubscriptionSink
        self.tasks_ext = tasks_ext
        self.server = risk.build_server()
        tasks_ext.install(self.server)
        self.sent: list[dict] = []
        self.sink = SubscriptionSink(7, {"tasks": True}, self.sent.append)
        self.sink.acknowledge(self.server.capabilities())
        self.server.attach_subscriber(self.sink)

    def pushed(self):
        return [m for m in self.sent if m.get("method") == "notifications/tasks"]

    def test_update_pushes_the_whole_task(self):
        """Carrying the task saves the tasks/get the client would send next."""
        task = self.server.tasks.create(status=self.tasks_ext.WORKING)
        self.server.tasks.update(task.task_id, status_message="scoring")
        pushed = self.pushed()
        self.assertEqual(len(pushed), 1)
        body = pushed[0]["params"]["task"]
        self.assertEqual(body["taskId"], task.task_id)
        self.assertEqual(body["statusMessage"], "scoring")

    def test_terminal_transition_is_pushed(self):
        task = self.server.tasks.create(status=self.tasks_ext.WORKING)
        self.server.tasks.update(task.task_id, status=self.tasks_ext.COMPLETED,
                                 result={"decision": "approve"})
        self.assertEqual(self.pushed()[-1]["params"]["task"]["status"], "completed")

    def test_notifications_are_tagged_with_the_subscription_id(self):
        """One stdio pipe carries every subscription, so the tag is the demux."""
        task = self.server.tasks.create(status=self.tasks_ext.WORKING)
        self.server.tasks.update(task.task_id, status_message="x")
        meta = self.pushed()[0]["params"]["_meta"]
        self.assertEqual(meta["io.modelcontextprotocol/subscriptionId"], 7)

    def test_a_subscriber_that_did_not_ask_gets_nothing(self):
        from meridian.protocol.subscriptions import SubscriptionSink
        other_sent: list[dict] = []
        other = SubscriptionSink(8, {"toolsListChanged": True}, other_sent.append)
        other.acknowledge(self.server.capabilities())
        self.server.attach_subscriber(other)

        task = self.server.tasks.create(status=self.tasks_ext.WORKING)
        self.server.tasks.update(task.task_id, status_message="x")
        self.assertFalse([m for m in other_sent
                          if m.get("method") == "notifications/tasks"])

    def test_a_server_without_the_extension_does_not_grant_task_pushes(self):
        """Granting them would promise updates no code path can send."""
        from meridian.protocol.subscriptions import SubscriptionSink
        plain = risk.build_server()
        sent: list[dict] = []
        sink = SubscriptionSink(9, {"tasks": True}, sent.append)
        sink.acknowledge(plain.capabilities())
        self.assertNotIn("tasks", sent[0]["params"]["notifications"])
        self.assertFalse(sink.wants("tasks"))


class TestIdempotency(unittest.TestCase):
    """Retries are ordinary now, so a mutating tool must survive them."""

    def setUp(self):
        from meridian.protocol.idempotency import IdempotencyStore
        self.Store = IdempotencyStore
        self.server = scoped.build_booking_server()
        self.client = Client(InProcessTransport(self.server, auth={"sub": "u1"}))

    def book(self, key, amount=250_000):
        return self.client.call_tool("book_facility", {
            "accountId": "ACC-1000", "amountUsd": amount,
            "idempotencyKey": key})

    def test_a_retry_returns_the_first_result(self):
        first = self.book("key-aaaaaaaa")
        second = self.book("key-aaaaaaaa")
        self.assertEqual(first["structuredContent"]["reference"],
                         second["structuredContent"]["reference"])

    def test_a_different_key_books_again(self):
        """The key is the intent. Two intents are two bookings."""
        a = self.book("key-aaaaaaaa")["structuredContent"]["reference"]
        b = self.book("key-bbbbbbbb")["structuredContent"]["reference"]
        self.assertNotEqual(a, b)

    def test_reusing_a_key_with_different_arguments_is_refused(self):
        """Returning the first booking's result here would hide a client bug."""
        self.book("key-aaaaaaaa", amount=250_000)
        with self.assertRaises(McpError):
            self.book("key-aaaaaaaa", amount=999_000)

    def test_the_work_runs_once(self):
        calls = {"n": 0}
        store = self.Store()

        def work():
            calls["n"] += 1
            return {"ok": True}

        for _ in range(5):
            store.run("k-12345678", "u1", {"a": 1}, work)
        self.assertEqual(calls["n"], 1)

    def test_a_failure_is_not_remembered(self):
        """Otherwise a transient error is permanent for the life of the key."""
        store = self.Store()
        attempts = {"n": 0}

        def flaky():
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise McpError(-32603, "downstream down")
            return {"ok": True}

        with self.assertRaises(McpError):
            store.run("k-12345678", "u1", {"a": 1}, flaky)
        self.assertEqual(store.run("k-12345678", "u1", {"a": 1}, flaky),
                         {"ok": True})

    def test_keys_are_scoped_to_the_principal(self):
        """One user's key must not collide with another's."""
        store = self.Store()
        alice = store.run("shared-key-1", "alice", {"a": 1}, lambda: "alice-result")
        bob = store.run("shared-key-1", "bob", {"a": 1}, lambda: "bob-result")
        self.assertEqual(alice, "alice-result")
        self.assertEqual(bob, "bob-result")

    def test_a_concurrent_duplicate_waits_rather_than_repeating_the_work(self):
        import threading
        store = self.Store()
        started, release = threading.Event(), threading.Event()
        calls = {"n": 0}

        def slow():
            calls["n"] += 1
            started.set()
            release.wait(5)
            return {"ok": True}

        results = []
        t = threading.Thread(
            target=lambda: results.append(
                store.run("k-12345678", "u1", {"a": 1}, slow)))
        t.start()
        started.wait(5)

        second = threading.Thread(
            target=lambda: results.append(
                store.run("k-12345678", "u1", {"a": 1}, slow)))
        second.start()
        release.set()
        t.join(5)
        second.join(5)

        self.assertEqual(calls["n"], 1)
        self.assertEqual(results, [{"ok": True}, {"ok": True}])

    def test_records_expire(self):
        now = {"t": 1000.0}
        store = self.Store(retention_seconds=60, clock=lambda: now["t"])
        store.run("k-12345678", "u1", {"a": 1}, lambda: "first")
        now["t"] += 61
        self.assertEqual(store.run("k-12345678", "u1", {"a": 1}, lambda: "second"),
                         "second")


class TestBackoff(unittest.TestCase):
    def test_ceiling_doubles(self):
        b = ops.Backoff(base_seconds=1.0)
        self.assertEqual([round(b.next_delay(1.0)) for _ in range(4)], [1, 2, 4, 8])

    def test_ceiling_is_capped(self):
        b = ops.Backoff(base_seconds=1.0, max_seconds=10.0)
        delays = [b.next_delay(1.0) for _ in range(10)]
        self.assertLessEqual(max(delays), 10.0)

    def test_jitter_spreads_the_fleet(self):
        """Full jitter, so two clients that failed together do not retry together."""
        a, c = ops.Backoff(base_seconds=1.0), ops.Backoff(base_seconds=1.0)
        a.next_delay(0.9)
        c.next_delay(0.1)
        self.assertNotEqual(a.next_delay(0.9), c.next_delay(0.1))

    def test_delay_never_exceeds_the_ceiling(self):
        b = ops.Backoff(base_seconds=2.0)
        self.assertLessEqual(b.next_delay(0.999), 2.0)

    def test_reset_returns_to_the_base(self):
        """Or a one-second blip later costs a minute of downtime."""
        b = ops.Backoff(base_seconds=1.0)
        for _ in range(6):
            b.next_delay(1.0)
        b.reset()
        self.assertEqual(b.next_delay(1.0), 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
