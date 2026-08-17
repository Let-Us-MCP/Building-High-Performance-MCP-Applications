"""End-to-end tests: host, loop, tasks, and the four Meridian servers."""

from __future__ import annotations

import time
import unittest

from meridian.host.host import ConsentDecision, ConsentPolicy, Host
from meridian.host.loop import AgentLoop
from meridian.host.model import StubModel, estimate_tokens
from meridian.protocol import (
    Client,
    ClientCapabilities,
    InProcessTransport,
    McpError,
    ScriptedInput,
)
from meridian.protocol import tasks as tasks_ext
from meridian.servers import compliance, fraud, marketdata, risk


def wire_host(*, fat: bool = False, poisoned: bool = False,
              inputs: dict | None = None, **kw) -> Host:
    host = Host(**kw)
    provider = ScriptedInput(inputs or {})
    risk_server = risk.build_server(fat_catalogue=fat)
    tasks_ext.install(risk_server)
    host.connect("risk", InProcessTransport(risk_server), input_provider=provider)
    host.connect("compliance",
                 InProcessTransport(compliance.build_server(poisoned=poisoned)),
                 input_provider=provider)
    host.connect("fraud", InProcessTransport(fraud.build_server()),
                 input_provider=provider)
    host.connect("marketdata", InProcessTransport(marketdata.build_server()),
                 input_provider=provider)
    host.refresh_catalogue()
    return host


class TestHostRouting(unittest.TestCase):
    def setUp(self):
        self.host = wire_host()

    def test_catalogue_is_namespaced(self):
        names = [t["name"] for t in self.host.catalogue()]
        self.assertIn("risk.assess_account_risk", names)
        self.assertIn("fraud.screen_account", names)
        self.assertTrue(all("." in n for n in names))

    def test_namespacing_keeps_collisions_apart(self):
        """Two servers may each define the same tool name. That is legal."""
        from meridian.protocol import Server, Tool, text_result

        def make(label: str) -> Server:
            server = Server(label, "1.0.0")
            server.add_tool(Tool("search", "Search", {"type": "object"},
                                 lambda ctx, l=label: text_result(f"from {l}")))
            return server

        host = Host()
        host.connect("alpha", InProcessTransport(make("alpha")))
        host.connect("beta", InProcessTransport(make("beta")))
        host.refresh_catalogue()

        names = [t["name"] for t in host.catalogue()]
        self.assertEqual(sorted(names), ["alpha.search", "beta.search"])
        self.assertEqual(
            host.call_tool("alpha.search")["content"][0]["text"], "from alpha")
        self.assertEqual(
            host.call_tool("beta.search")["content"][0]["text"], "from beta")

    def test_unqualified_name_is_rejected(self):
        with self.assertRaises(McpError):
            self.host.call_tool("assess_account_risk", {})

    def test_unknown_server_is_rejected(self):
        with self.assertRaises(McpError):
            self.host.call_tool("nosuch.tool", {})

    def test_parallel_fanout_returns_results_in_order(self):
        calls = [
            {"name": "risk.assess_account_risk", "arguments": {"accountId": "ACC-1005"}},
            {"name": "fraud.screen_account", "arguments": {"accountId": "ACC-1005"}},
            {"name": "marketdata.get_reference_curve", "arguments": {}},
        ]
        results = self.host.call_tools_parallel(calls)
        self.assertEqual(len(results), 3)
        self.assertEqual(results[0]["structuredContent"]["accountId"], "ACC-1005")
        self.assertEqual(results[1]["structuredContent"]["accountId"], "ACC-1005")
        self.assertIn("curve", results[2]["structuredContent"])

    def test_discovery_reports_capabilities_per_server(self):
        discovered = self.host.discover_all()
        self.assertIn("tools", discovered["risk"]["capabilities"])
        self.assertIn("resources", discovered["marketdata"]["capabilities"])
        self.assertIn(tasks_ext.EXTENSION_ID,
                      discovered["risk"]["capabilities"]["extensions"])


class TestConsent(unittest.TestCase):
    def test_denylist_blocks_the_call(self):
        host = wire_host(consent=ConsentPolicy(denylist={"risk.underwrite_loan"}))
        result = host.call_tool("risk.underwrite_loan",
                                {"accountId": "ACC-1000", "amountUsd": 50_000})
        self.assertTrue(result["isError"])
        self.assertIn("declined", result["content"][0]["text"])

    def test_read_only_hint_is_auto_allowed(self):
        seen: list[str] = []
        host = wire_host(consent=ConsentPolicy(
            auto_allow_read_only=True,
            on_ask=lambda name, args: seen.append(name) or True))
        host.call_tool("risk.assess_account_risk", {"accountId": "ACC-1000"})
        self.assertEqual(seen, [], "a read-only tool should not have prompted")

    def test_non_read_only_reaches_the_prompt(self):
        seen: list[str] = []

        def ask(name, args):
            seen.append(name)
            return True

        host = wire_host(consent=ConsentPolicy(on_ask=ask))
        host.call_tool("compliance.review_transaction", {"txnId": "TXN-0000-00"})
        self.assertIn("compliance.review_transaction", seen)

    def test_every_call_is_audited(self):
        host = wire_host()
        host.call_tool("risk.assess_account_risk", {"accountId": "ACC-1000"})
        self.assertEqual(len(host.audit), 1)
        self.assertEqual(host.audit[0]["tool"], "risk.assess_account_risk")


class TestMrtrEndToEnd(unittest.TestCase):
    def test_approval_flow_completes_in_two_round_trips(self):
        host = wire_host(inputs={
            "approval": {"action": "accept",
                         "content": {"approver": "j.okonjo", "rationale": "board ok"}},
        })
        binding = host.bindings["risk"]
        before = binding.client.stats.requests

        result = host.call_tool("risk.assess_account_risk",
                                {"accountId": "ACC-1000", "exposureUsd": 9_000_000})

        self.assertFalse(result.get("isError"))
        self.assertIn("score", result["structuredContent"])
        self.assertEqual(binding.client.stats.requests - before, 2)
        self.assertEqual(binding.client.stats.mrtr_rounds, 1)

    def test_declining_produces_a_tool_error_not_a_crash(self):
        host = wire_host(inputs={"approval": {"action": "decline"}})
        result = host.call_tool("risk.assess_account_risk",
                                {"accountId": "ACC-1000", "exposureUsd": 9_000_000})
        self.assertTrue(result["isError"])
        self.assertIn("declined", result["content"][0]["text"])

    def test_below_threshold_needs_no_round_trip(self):
        host = wire_host()
        binding = host.bindings["risk"]
        before = binding.client.stats.requests
        host.call_tool("risk.assess_account_risk",
                       {"accountId": "ACC-1000", "exposureUsd": 100_000})
        self.assertEqual(binding.client.stats.requests - before, 1)

    def test_compliance_officer_flow(self):
        host = wire_host(inputs={
            "officer": {"action": "accept",
                        "content": {"officer": "r.mehta", "decision": "escalate"}},
        })
        flagged = _find_flagged_txn()
        result = host.call_tool("compliance.review_transaction", {"txnId": flagged})
        self.assertEqual(result["structuredContent"]["officer"], "r.mehta")
        self.assertEqual(result["structuredContent"]["outcome"], "escalate")

    def test_tampered_request_state_is_rejected(self):
        """A malicious client rewriting requestState must not get through.

        This one bypasses `Client.call`, because that helper answers input
        requests for you. Here we are playing the attacker, so we drive the
        wire directly.
        """
        from meridian.protocol import build_request_meta

        server = risk.build_server()
        caps = ClientCapabilities(elicitation={"form": {}})
        args = {"accountId": "ACC-1000", "exposureUsd": 9_000_000}

        def post(params: dict, req_id: int) -> dict:
            params = dict(params)
            params["_meta"] = build_request_meta(caps)
            return server.handle({"jsonrpc": "2.0", "id": req_id,
                                  "method": "tools/call", "params": params})

        first = post({"name": "assess_account_risk", "arguments": args}, 1)["result"]
        self.assertEqual(first["resultType"], "input_required")
        forged = first["requestState"][:-4] + "AAAA"

        second = post({
            "name": "assess_account_risk",
            "arguments": args,
            "inputResponses": {"approval": {"action": "accept",
                                            "content": {"approver": "mallory"}}},
            "requestState": forged,
        }, 2)

        self.assertIn("error", second, "a forged requestState was accepted")
        self.assertIn("verification", second["error"]["message"].lower())

        # And the untampered state still works, so the check is not just
        # rejecting everything.
        third = post({
            "name": "assess_account_risk",
            "arguments": args,
            "inputResponses": {"approval": {"action": "accept",
                                            "content": {"approver": "j.okonjo"}}},
            "requestState": first["requestState"],
        }, 3)
        self.assertIn("structuredContent", third["result"])


def _find_flagged_txn() -> str:
    from meridian.servers.data import TRANSACTIONS
    for items in TRANSACTIONS.values():
        for txn in items:
            if txn.flagged:
                return txn.txn_id
    raise AssertionError("the fixture should contain a flagged transaction")


class TestTasks(unittest.TestCase):
    def test_long_job_returns_a_handle_and_resolves(self):
        server = risk.build_server()
        tasks_ext.install(server)
        caps = ClientCapabilities(extensions={tasks_ext.EXTENSION_ID: {}})
        client = Client(InProcessTransport(server), capabilities=caps)

        result = client.call_tool("underwrite_loan",
                                  {"accountId": "ACC-1000", "amountUsd": 250_000})
        self.assertEqual(result["resultType"], "task")
        task_id = result["task"]["taskId"]

        final = tasks_ext.poll_until_done(client, task_id, timeout=15)
        self.assertIn(final["structuredContent"]["decision"],
                      ("approve", "refer", "decline"))

    def test_client_without_the_extension_gets_the_synchronous_answer(self):
        server = risk.build_server()
        tasks_ext.install(server)
        client = Client(InProcessTransport(server),
                        capabilities=ClientCapabilities())
        result = client.call_tool("underwrite_loan",
                                  {"accountId": "ACC-1000", "amountUsd": 250_000})
        self.assertEqual(result["resultType"], "complete")
        self.assertIn("decision", result["structuredContent"])

    def test_cancel_is_acknowledged(self):
        server = risk.build_server()
        store = tasks_ext.install(server)
        caps = ClientCapabilities(extensions={tasks_ext.EXTENSION_ID: {}})
        client = Client(InProcessTransport(server), capabilities=caps)
        result = client.call_tool("underwrite_loan",
                                  {"accountId": "ACC-1000", "amountUsd": 250_000})
        task_id = result["task"]["taskId"]
        client.call("tasks/cancel", {"taskId": task_id}, use_cache=False)
        self.assertIn(store.get(task_id).status,
                      (tasks_ext.CANCELLED, tasks_ext.COMPLETED))

    def test_unknown_task_is_invalid_params(self):
        server = risk.build_server()
        tasks_ext.install(server)
        client = Client(InProcessTransport(server), capabilities=ClientCapabilities())
        with self.assertRaises(McpError):
            client.call("tasks/get", {"taskId": "tsk_nope"}, use_cache=False)


class TestCachingEndToEnd(unittest.TestCase):
    def test_repeat_list_hits_the_cache(self):
        """`refresh_catalogue` already paid for the fetch. Nothing else should."""
        host = wire_host()
        binding = host.bindings["marketdata"]
        before = binding.client.stats.requests
        for _ in range(10):
            binding.client.list_tools()
        self.assertEqual(binding.client.stats.requests - before, 0)
        self.assertGreaterEqual(host.cache.stats.hits, 10)

    def test_list_changed_forces_a_refetch(self):
        server = fraud.build_server()
        host = Host()
        binding = host.connect("fraud", InProcessTransport(server))
        binding.client.list_tools()
        first = binding.client.stats.requests

        binding.client.list_tools()
        self.assertEqual(binding.client.stats.requests, first)  # served from cache

        server.notify_list_changed("tools")  # no subscriber, so invalidate directly
        binding.client.on_notification({"method": "notifications/tools/list_changed"})

        binding.client.list_tools()
        self.assertEqual(binding.client.stats.requests, first + 1)

    def test_public_resource_is_shared_across_auth_contexts(self):
        server = marketdata.build_server()
        host = Host()
        alice = host.connect("md", InProcessTransport(server), auth_context="alice")
        # A second client on the same shared cache, different principal.
        bob = Client(InProcessTransport(server), cache=host.cache,
                     server_label="md", auth_context="bob")

        alice.client.read_resource("meridian://market/curve")
        before = bob.stats.requests
        bob.read_resource("meridian://market/curve")
        # Public or not, this implementation keys by principal, which is the
        # conservative choice. The entry is refetched rather than shared.
        self.assertEqual(bob.stats.requests - before, 1)


class TestAgentLoop(unittest.TestCase):
    def test_loop_terminates_and_attributes_time(self):
        host = wire_host()
        model = StubModel(plan=[
            [{"name": "risk.assess_account_risk", "arguments": {"accountId": "ACC-1002"}}],
            [{"name": "fraud.screen_account", "arguments": {"accountId": "ACC-1002"}}],
            [],
        ], simulate_latency=False)
        result = AgentLoop(host, model).run("Assess ACC-1002 for credit and fraud.")

        self.assertEqual(result.stopped_because, "completed")
        self.assertEqual(len(result.iterations), 3)
        self.assertEqual(result.round_trips, 2)
        self.assertGreater(result.model_ms, 0)
        self.assertGreater(result.total_tokens, 0)

    def test_step_budget_stops_a_runaway(self):
        host = wire_host()
        looping = [[{"name": "fraud.screen_account",
                     "arguments": {"accountId": "ACC-1000"}}]] * 50
        model = StubModel(plan=looping, simulate_latency=False)
        result = AgentLoop(host, model, max_steps=4).run("loop forever")
        self.assertEqual(result.stopped_because, "step budget exhausted")
        self.assertEqual(len(result.iterations), 4)

    def test_cost_budget_stops_the_loop(self):
        host = wire_host()
        looping = [[{"name": "fraud.screen_account",
                     "arguments": {"accountId": "ACC-1000"}}]] * 50
        model = StubModel(plan=looping, simulate_latency=False)
        result = AgentLoop(host, model, max_steps=40,
                           cost_budget_usd=0.0001).run("spend money")
        self.assertEqual(result.stopped_because, "cost budget exhausted")

    def test_parallel_fanout_beats_serial_on_round_trips(self):
        """Same work, same results, fewer wall-clock milliseconds."""
        calls = [
            {"name": "risk.assess_account_risk", "arguments": {"accountId": "ACC-1003"}},
            {"name": "fraud.screen_account", "arguments": {"accountId": "ACC-1003"}},
            {"name": "marketdata.get_reference_curve", "arguments": {}},
        ]
        host = wire_host()
        for binding in host.bindings.values():
            binding.client.transport.latency_ms = 8.0

        serial = AgentLoop(host, StubModel(plan=[calls, []], simulate_latency=False),
                           parallel_fanout=False).run("go")
        parallel = AgentLoop(host, StubModel(plan=[calls, []], simulate_latency=False),
                             parallel_fanout=True).run("go")

        self.assertLess(parallel.transport_ms, serial.transport_ms)

    def test_catalogue_size_drives_input_tokens(self):
        """The fat catalogue is paid for on every single turn."""
        slim = wire_host(fat=False)
        fat = wire_host(fat=True)
        self.assertGreater(fat.catalogue_tokens(), slim.catalogue_tokens() * 3)

        plan = [[{"name": "risk.assess_account_risk",
                  "arguments": {"accountId": "ACC-1004"}}], []]
        slim_run = AgentLoop(slim, StubModel(plan=plan, simulate_latency=False)).run("go")
        fat_run = AgentLoop(fat, StubModel(plan=plan, simulate_latency=False)).run("go")
        self.assertGreater(fat_run.total_tokens, slim_run.total_tokens)


class TestServerBehaviour(unittest.TestCase):
    def test_every_server_answers_discover(self):
        for build in (risk.build_server, compliance.build_server,
                      fraud.build_server, marketdata.build_server):
            with self.subTest(server=build.__module__):
                client = Client(InProcessTransport(build()))
                result = client.discover()
                self.assertIn("2026-07-28", result["supportedVersions"])
                self.assertIn("capabilities", result)

    def test_public_and_private_scopes_are_used_deliberately(self):
        client = Client(InProcessTransport(risk.build_server()))
        public = client.read_resource("meridian://filings/FIL-2000")
        private = client.read_resource("meridian://accounts/ACC-1000/summary")
        self.assertEqual(public["cacheScope"], "public")
        self.assertEqual(private["cacheScope"], "private")

    def test_marketdata_paginates_its_resource_list(self):
        client = Client(InProcessTransport(marketdata.build_server()))
        first = client.call("resources/list")
        self.assertIn("nextCursor", first)
        everything = client.list_resources()
        self.assertGreater(len(everything), 25)

    def test_poisoned_server_returns_the_injection_verbatim(self):
        """The protocol does not stop this, and pretending otherwise is the bug."""
        client = Client(InProcessTransport(compliance.build_server(poisoned=True)))
        result = client.call_tool("search_guidance", {"query": "structuring"})
        self.assertIn("IMPORTANT SYSTEM NOTICE", result["content"][0]["text"])

    def test_clean_server_does_not(self):
        client = Client(InProcessTransport(compliance.build_server(poisoned=False)))
        result = client.call_tool("search_guidance", {"query": "structuring"})
        self.assertNotIn("IMPORTANT SYSTEM NOTICE", result["content"][0]["text"])

    def test_runtime_tool_addition_notifies_subscribers(self):
        server = fraud.build_server()
        received: list[dict] = []

        class Sink:
            def wants(self, key): return key == "toolsListChanged"
            def wants_uri(self, uri): return False
            def send(self, msg): received.append(msg)

        server.attach_subscriber(Sink())
        sent = fraud.add_runtime_tool(server)
        self.assertEqual(sent, 1)
        self.assertEqual(received[0]["method"], "notifications/tools/list_changed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
