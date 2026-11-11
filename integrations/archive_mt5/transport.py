"""Outbound fixed-host JSON only. Server replies are never executable code."""
import http.client
import json
import ssl

from .common import Refusal, canonical, unique_json


class Transport:
    def __init__(self, config, factory=http.client.HTTPSConnection):
        self.config, self.factory = config, factory

    def post(self, action, payload):
        if action not in ("poll", "result"):
            raise Refusal("invalid_endpoint", True)
        body = canonical(payload).encode("utf-8")
        if len(body) > 1024 * 1024:
            raise Refusal("payload_too_large", True)
        connection = None
        try:
            # No proxy variables, redirects or SSLKEYLOGFILE auto-configuration.
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.load_default_certs()
            connection = self.factory("hollowheir.com", timeout=10, context=context)
            connection.request("POST", "/_archive/mt5/trading/" + action, body=body, headers={
                "Authorization": "Bearer " + self.config.token, "Content-Type": "application/json",
                "Accept": "application/json", "Content-Length": str(len(body)),
                "User-Agent": "TradingAgents-Archive-ManualMT5/1", "Cache-Control": "no-store"})
            response = connection.getresponse()
            if 300 <= response.status < 400 or response.status in (401, 403, 404, 409):
                raise Refusal("device_authorization_or_state_rejected", True)
            if response.status not in (200, 201, 202):
                raise Refusal("transport_unavailable")
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise Refusal("invalid_response", True)
            result = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_json)
            if not isinstance(result, dict):
                raise Refusal("invalid_response", True)
            return result
        except Refusal:
            raise
        except Exception:
            raise Refusal("transport_unavailable") from None
        finally:
            if connection:
                connection.close()
