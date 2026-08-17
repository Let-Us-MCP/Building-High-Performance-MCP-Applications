"""Multi Round-Trip Requests: how a stateless server asks a question.

The old design let a server send its own JSON-RPC request back down the wire
mid-call. That required the connection to mean something, which is exactly what
2026-07-28 removed. MRTR inverts it:

    client  -> tools/call (id 1)
    server  <- result { resultType: "input_required",
                        inputRequests: {...}, requestState: "..." }
              ... the original request is now finished and forgotten ...
    client  -> tools/call (id 2, same arguments + inputResponses + requestState)
    server  <- result { resultType: "complete", ... }

The server holds nothing between the two. Whatever it needs to remember, it
seals into `requestState` and hands to the client to carry back.

Which means `requestState` is attacker-controlled input. It goes out through a
client you do not own and comes back through a client that may not be the one
you sent it to. `StateSealer` below treats it accordingly: authenticated
encryption, a bound principal, a bound request shape, and an expiry.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

from .errors import InvalidParams
from .jsonrpc import RESULT_INPUT_REQUIRED

# --- Building input requests ----------------------------------------------


def elicit_form(message: str, schema: dict) -> dict:
    """An `elicitation/create` in form mode.

    Form mode is for ordinary structured input. It must never be used for
    secrets: the values pass through the client, get rendered in a UI, and end
    up in logs. Credentials go through URL mode instead.
    """
    return {
        "method": "elicitation/create",
        "params": {"mode": "form", "message": message, "requestedSchema": schema},
    }


def elicit_url(message: str, url: str) -> dict:
    """An `elicitation/create` in URL mode, for anything sensitive.

    The client shows the URL, gets consent, and opens it somewhere it cannot
    read. It learns only that the user agreed to look, never what happened
    next. The server finds out by other means and reports on the retry.
    """
    return {
        "method": "elicitation/create",
        "params": {"mode": "url", "message": message, "url": url},
    }


def sample(messages: list[dict], *, max_tokens: int = 512,
           system_prompt: str | None = None,
           model_preferences: dict | None = None) -> dict:
    """A `sampling/createMessage` input request.

    Deprecated as of 2026-07-28. Included because you will meet servers that
    still send it, and because Chapter 8 argues about why it lost.
    """
    params: dict[str, Any] = {"messages": messages, "maxTokens": max_tokens}
    if system_prompt:
        params["systemPrompt"] = system_prompt
    if model_preferences:
        params["modelPreferences"] = model_preferences
    return {"method": "sampling/createMessage", "params": params}


def input_required(
    input_requests: dict[str, dict] | None = None,
    request_state: str | None = None,
) -> dict:
    """Build an `InputRequiredResult`.

    At least one of the two fields must be present. A result with neither says
    "I cannot continue and I will not tell you why", which is not a protocol
    state, it is a bug.
    """
    if not input_requests and request_state is None:
        raise ValueError(
            "InputRequiredResult needs inputRequests, requestState, or both"
        )
    out: dict[str, Any] = {"resultType": RESULT_INPUT_REQUIRED}
    if input_requests:
        out["inputRequests"] = input_requests
    if request_state is not None:
        out["requestState"] = request_state
    return out


# --- Reading input responses ----------------------------------------------


@dataclass
class ElicitResponse:
    action: str                       # accept | decline | cancel
    content: dict = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return self.action == "accept"

    @property
    def declined(self) -> bool:
        return self.action == "decline"

    @property
    def cancelled(self) -> bool:
        return self.action == "cancel"


def read_elicit(responses: dict, key: str) -> ElicitResponse | None:
    """Pull one elicitation answer out of `inputResponses`.

    Returns None when the client did not answer this key at all, which is
    allowed: the specification says a server that still needs the value should
    ask again with a fresh `InputRequiredResult` rather than erroring out.
    """
    raw = responses.get(key)
    if not isinstance(raw, dict):
        return None
    action = raw.get("action")
    if action not in ("accept", "decline", "cancel"):
        raise InvalidParams(f"inputResponses[{key!r}] has an invalid action")
    content = raw.get("content")
    return ElicitResponse(action, content if isinstance(content, dict) else {})


# --- Sealing state --------------------------------------------------------


class StateExpired(InvalidParams):
    def __init__(self) -> None:
        super().__init__("requestState has expired; retry the operation")


class StateTampered(InvalidParams):
    def __init__(self, detail: str = "requestState failed verification") -> None:
        super().__init__(detail)


class StateSealer:
    """Authenticated, expiring, principal-bound `requestState`.

    The format is `v1.<payload-b64>.<mac-b64>`, with the MAC over the payload
    bytes. HMAC-SHA256 gives integrity, which is what the specification
    requires. It does not give confidentiality: anyone holding the blob can
    read it. So put correlation data in here, never secrets.

    Three bindings, each closing a specific attack:

    `principal`  stops Bob replaying Alice's state to inherit her half-finished
                 privileged operation.
    `request`    stops a state minted for `approve_refund` being replayed
                 against `transfer_funds`.
    `expiry`     bounds the replay window when the other two are not enough.

    None of this makes the blob single-use. If an operation must happen at most
    once, the server has to track that itself. Chapter 8 says so at more length.
    """

    VERSION = "v1"

    def __init__(self, secret: bytes | None = None, *, ttl_seconds: int = 600):
        self._secret = secret or os.urandom(32)
        self.ttl_seconds = ttl_seconds

    # -- helpers
    @staticmethod
    def _b64e(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _b64d(text: str) -> bytes:
        pad = "=" * (-len(text) % 4)
        return base64.urlsafe_b64decode(text + pad)

    def _mac(self, payload: bytes) -> bytes:
        return hmac.new(self._secret, payload, hashlib.sha256).digest()

    @staticmethod
    def request_digest(method: str, params: dict) -> str:
        """A stable fingerprint of the request this state belongs to.

        Deliberately covers only the fields that identify *which* operation is
        in flight. Including `inputResponses` would change the digest between
        the mint and the redeem, which defeats the purpose.
        """
        salient = {
            "method": method,
            "name": params.get("name") or params.get("uri"),
            "arguments": params.get("arguments"),
        }
        blob = json.dumps(salient, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]

    # -- API
    def seal(self, data: dict, *, principal: str | None = None,
             method: str | None = None, params: dict | None = None,
             ttl_seconds: int | None = None, now: float | None = None) -> str:
        now = time.time() if now is None else now
        envelope = {
            "d": data,
            "exp": now + (ttl_seconds if ttl_seconds is not None else self.ttl_seconds),
            "sub": principal,
            "req": self.request_digest(method, params or {}) if method else None,
        }
        payload = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
        return f"{self.VERSION}.{self._b64e(payload)}.{self._b64e(self._mac(payload))}"

    def open(self, token: str, *, principal: str | None = None,
             method: str | None = None, params: dict | None = None,
             now: float | None = None) -> dict:
        now = time.time() if now is None else now
        if not isinstance(token, str):
            raise StateTampered("requestState must be a string")
        parts = token.split(".")
        if len(parts) != 3 or parts[0] != self.VERSION:
            raise StateTampered("requestState has an unrecognised format")

        try:
            payload = self._b64d(parts[1])
            mac = self._b64d(parts[2])
        except Exception as exc:
            raise StateTampered("requestState is not valid base64") from exc

        # Constant time, so a timing oracle cannot walk the MAC out of us.
        if not hmac.compare_digest(mac, self._mac(payload)):
            raise StateTampered()

        try:
            envelope = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise StateTampered("requestState payload is not JSON") from exc

        if envelope.get("exp", 0) < now:
            raise StateExpired()

        bound_sub = envelope.get("sub")
        if bound_sub is not None and bound_sub != principal:
            raise StateTampered("requestState was issued to a different principal")

        bound_req = envelope.get("req")
        if bound_req is not None:
            if method is None:
                raise StateTampered("requestState is request-bound but no request given")
            if not hmac.compare_digest(bound_req,
                                       self.request_digest(method, params or {})):
                raise StateTampered("requestState was issued for a different request")

        data = envelope.get("d")
        return data if isinstance(data, dict) else {}
