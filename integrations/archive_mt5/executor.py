"""Durable, explicitly confirmed manual-order dispatcher.

There is deliberately ONE order_send call site. The journal commits attempted
state first. Recovery and reconciliation contain no order submission path.
"""
from datetime import datetime, timezone
import json
import math
import time

from .common import UUID, Refusal, decimal, digest, exact, number, outcome, ticket


class Executor:
    def __init__(self, config, broker, journal, transport, monotonic=time.monotonic):
        self.config, self.broker, self.journal, self.transport = config, broker, journal, transport
        self.clock = broker.clock
        self.monotonic = monotonic
        self.binding = digest({"connection": config.connection_id, "login": config.expected_login,
                               "server": config.expected_server, "terminal": config.terminal_path,
                               "dataPath": config.terminal_data_path})
        journal.bind(self.binding)

    def envelope(self, command):
        return {"version": 1, "connectionId": self.config.connection_id, "login": self.config.expected_login,
                "server": self.config.expected_server, "id": command["id"], "claimId": command["claimId"]}

    def flush(self):
        for payload in self.journal.pending():
            response = self.transport.post("result", payload)
            if response.get("accepted") is not True or response.get("id") != payload["id"] or response.get("status") != payload["status"]:
                raise Refusal("acknowledgement_invalid", True)
            discarded = response.get("discarded")
            if discarded is not None:
                row = self.journal.get(payload["id"])
                if discarded not in ("cancelled", "expired") or payload["status"] not in ("ready", "rejected") or row and (row["execute_claim"] is not None or row["state"] not in ("ready", "rejected")):
                    raise Refusal("acknowledgement_invalid", True)
            self.journal.acknowledge(payload, discarded)

    def command(self, command, server_time=None):
        if not isinstance(command, dict) or command.get("action") not in ("catalog", "prepare", "execute"):
            raise Refusal("invalid_command", True)
        fields = ["id", "claimId", "action", "expiresAt"]
        if command["action"] == "prepare":
            fields.append("input")
        elif command["action"] == "execute":
            fields.append("reviewHash")
        exact(command, fields)
        if any(not isinstance(command[key], str) or not UUID.fullmatch(command[key]) for key in ("id", "claimId")) or type(command["expiresAt"]) is not int:
            raise Refusal("invalid_command", True)
        response = self.envelope(command)
        if command["action"] == "execute":
            return self.execute(command, server_time)
        try:
            if self.clock() >= command["expiresAt"]:
                raise Refusal("command_expired")
            if command["action"] == "catalog":
                instruments = self.broker.catalog()
                if self.clock() >= command["expiresAt"]:
                    raise Refusal("command_expired")
                self.journal.result({**response, "status": "ready", "instruments": instruments})
            else:
                if self.journal.get(command["id"]):
                    raise Refusal("duplicate_prepare", True)
                prepared = self.broker.prepare(command["id"], command["input"])
                if self.clock() >= command["expiresAt"]:
                    raise Refusal("command_expired")
                self.journal.prepare(command["id"], self.binding, prepared,
                                     {**response, "status": "ready", "review": prepared["review"], "reviewHash": prepared["reviewHash"]})
        except Refusal as error:
            if error.fatal:
                raise
            self.journal.result({**response, "status": "rejected", "result": outcome(error.code, self.clock())})
        except Exception:
            # Unexpected local persistence failure must stop, not create a
            # contradictory replacement result or attempt execution.
            raise Refusal("preparation_failed", True) from None

    def execute(self, command, server_time=None):
        row = self.journal.get(command["id"])
        if not row or row["binding"] != self.binding or row["state"] != "ready":
            # A duplicate execute is never another opportunity to call MT5.
            if row and row["execute_claim"] == command["claimId"]:
                return
            raise Refusal("local_review_unavailable", True)
        prepared = json.loads(row["prepared"])
        response = self.envelope(command)
        volume = prepared["review"]["volume"]
        effective_now = max(self.clock(), server_time or 0)
        deadline = self.monotonic() + max(0, min(command["expiresAt"], prepared["review"]["expiresAt"]) - effective_now) / 1000
        def deadline_check():
            if self.monotonic() >= deadline:
                raise Refusal("execution_expired")
        try:
            if command["reviewHash"] != prepared["reviewHash"] or digest(prepared["review"]) != command["reviewHash"]:
                raise Refusal("review_hash_mismatch")
            if command["expiresAt"] > prepared["review"]["expiresAt"] or command["expiresAt"] > self.clock() + 12000:
                raise Refusal("invalid_execution_expiry")
            deadline_check()
            self.broker.check_execution(prepared, command["expiresAt"])
            deadline_check()
        except Refusal as error:
            self.journal.result({**response, "status": "rejected", "result": outcome(error.code, self.clock(), volume)}, "rejected")
            if error.fatal:
                raise
            return
        except Exception:
            self.journal.result({**response, "status": "rejected", "result": outcome("preflight_unavailable", self.clock(), volume)}, "rejected")
            return
        # Any journal error propagates. Never send if durable reservation failed.
        self.journal.reserve(command["id"], self.binding, command["claimId"], self.clock())
        try:
            # Recheck after fsync: expiration, account or permissions may change.
            self.broker.validate_execution(prepared, command["expiresAt"])
            deadline_check()
        except Exception:
            self.journal.result({**response, "status": "rejected", "result": outcome("execution_expired_or_blocked", self.clock(), volume)}, "rejected")
            return
        # No exception after this point may be represented as safe to resend.
        interruption = None
        try:
            broker_result = self.broker.api.order_send(prepared["request"])
            status, result = self.normalize_result(broker_result, volume)
            self.broker.identity()
        except BaseException as error:
            status, result = "unknown", outcome("unknown", self.clock(), volume)
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                interruption = error
        self.journal.result({**response, "status": status, "result": result}, status)
        if interruption is not None:
            raise interruption

    def normalize_result(self, value, volume):
        unknown = ("unknown", outcome("unknown", self.clock(), volume))
        if value is None or type(value.retcode) is not int:
            return unknown
        code = value.retcode
        fields = {"retcode": code, "orderTicket": ticket(value.order), "dealTicket": ticket(value.deal)}
        if code in (10009, 10010):
            try:
                filled = number(value.volume, positive=True)
                price = number(value.price, positive=True)
                if decimal(filled) > decimal(volume) or not (fields["orderTicket"] or fields["dealTicket"]):
                    return unknown
                if code == 10009 and decimal(filled) != decimal(volume) or code == 10010 and decimal(filled) >= decimal(volume):
                    return unknown
                status = "filled" if code == 10009 else "partial"
                return status, outcome(status, self.clock(), volume, **fields, filledVolume=filled, fillPrice=price)
            except (Refusal, AttributeError):
                return unknown
        if code == 10008 and fields["orderTicket"]:
            return "accepted", outcome("accepted", self.clock(), volume, **fields)
        # Explicit broker rejections. Processing errors, timeout, connection
        # loss, locks and unrecognized return codes remain ambiguous.
        rejected = {10004, 10006, 10007, 10013, 10014, 10015, 10016, 10017, 10018, 10019,
                    10020, 10021, 10022, 10024, 10026, 10027, 10030, 10032, 10033, 10034,
                    10035, 10040, 10042, 10043, 10044, 10045, 10046}
        if code in rejected and not fields["orderTicket"] and not fields["dealTicket"]:
            try:
                if decimal(number(value.volume, nonnegative=True)) != 0:
                    return unknown
            except (Refusal, AttributeError):
                return unknown
            return "rejected", outcome("rejected", self.clock(), volume, **fields)
        return unknown

    def recover(self):
        for row in self.journal.unsettled():
            if row["state"] != "attempted":
                continue
            prepared = json.loads(row["prepared"])
            response = self.envelope({"id": row["id"], "claimId": row["execute_claim"]})
            self.journal.result({**response, "status": "unknown", "result": outcome("unknown", self.clock(), prepared["review"]["volume"])}, "unknown")

    def reconcile(self):
        """Read-only positive-evidence reconciliation; absence never means failed."""
        for row in self.journal.unsettled():
            if row["state"] not in ("unknown", "accepted"):
                continue
            prepared = json.loads(row["prepared"])
            try:
                match = self.reconcile_one(row, prepared)
            except Exception:
                continue
            if match:
                status, result = match
                response = self.envelope({"id": row["id"], "claimId": row["execute_claim"]})
                self.journal.result({**response, "status": status, "result": result}, status)

    def reconcile_one(self, row, prepared):
        self.broker.identity()
        start = datetime.fromtimestamp((row["attempted_at"] - 60000) / 1000, timezone.utc)
        end = datetime.fromtimestamp((self.clock() + 5000) / 1000, timezone.utc)
        orders = self.broker.api.history_orders_get(start, end)
        deals = self.broker.api.history_deals_get(start, end)
        active = self.broker.api.orders_get(symbol=prepared["review"]["symbol"])
        if orders is None or deals is None or active is None or len(orders) > 10000 or len(deals) > 20000:
            return None
        self.broker.identity()
        request = prepared["request"]
        known = self.journal.latest_result(row["id"], row["execute_claim"])
        known_ticket = known.get("orderTicket") if known else None
        matches = [o for o in orders if o.magic == request["magic"] and o.symbol == request["symbol"]
                   and o.type == request["type"] and number(o.volume_initial) == number(request["volume"])
                   and (known_ticket is None or str(o.ticket) == known_ticket)
                   and row["attempted_at"] - 5000 <= o.time_setup_msc <= row["attempted_at"] + 120000]
        if len(matches) != 1:
            return None
        order = matches[0]
        if any(o.ticket == order.ticket for o in active):
            return None
        # Absence from active orders alone is not proof of completion. A
        # terminal history state and its exact executed quantity must agree
        # with the returned deals; history can arrive in multiple fragments.
        final_states = (self.broker.api.ORDER_STATE_FILLED, self.broker.api.ORDER_STATE_CANCELED,
                        self.broker.api.ORDER_STATE_EXPIRED, self.broker.api.ORDER_STATE_REJECTED)
        if order.state not in final_states:
            return None
        initial = decimal(number(order.volume_initial, positive=True))
        remaining = decimal(number(order.volume_current, nonnegative=True))
        if remaining > initial or order.state == self.broker.api.ORDER_STATE_FILLED and remaining != 0:
            return None
        executed = initial - remaining
        found = [d for d in deals if d.order == order.ticket and d.magic == request["magic"]
                 and d.symbol == request["symbol"] and d.type == request["type"]]
        if not found or len({d.ticket for d in found}) != len(found):
            return None
        volume = sum((decimal(number(d.volume, positive=True)) for d in found), start=decimal("0"))
        requested = decimal(prepared["review"]["volume"])
        if volume <= 0 or volume > requested or volume != executed:
            return None
        status = "filled" if volume == requested else "partial"
        # Report the actual last fill price, not an invented rounded average.
        last = max(found, key=lambda d: d.time_msc)
        if not ticket(order.ticket) or not ticket(last.ticket):
            return None
        result = outcome(status, self.clock(), prepared["review"]["volume"],
                         orderTicket=ticket(order.ticket), dealTicket=ticket(last.ticket),
                         filledVolume=number(volume), fillPrice=number(last.price, positive=True))
        result["code"] = "reconciled_" + status
        return status, result

    def step(self):
        self.recover()
        self.flush()
        self.reconcile()
        self.flush()
        account, permissions = self.broker.identity()
        poll_started = self.monotonic()
        response = self.transport.post("poll", {"version": 1, "connectionId": self.config.connection_id,
            "login": self.config.expected_login, "server": self.config.expected_server, "mode": "real",
            "currency": account.currency, "capturedAt": self.clock(), "permissions": permissions})
        exact(response, ["command", "serverTime"])
        if type(response["serverTime"]) is not int or abs(response["serverTime"] - self.clock()) > 10000:
            raise Refusal("clock_out_of_sync", True)
        if response["command"] is not None:
            # Include the whole round trip conservatively. A delayed reply or
            # wall-clock rollback may shorten validity, never extend it.
            server_bound = response["serverTime"] + math.ceil((self.monotonic() - poll_started) * 1000)
            self.command(response["command"], server_bound)
            self.flush()
