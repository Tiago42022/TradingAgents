"""Fixed HTTPS signal endpoint; redirects never receive the device bearer."""
import http.client
import json
import ssl

from .common import Refusal, canonical, unique_json

LIMIT = 128 * 1024


class Transport:
    def __init__(self, config, factory=http.client.HTTPSConnection):
        self.config, self.factory = config, factory

    def get(self):
        return self.request("GET")

    def post(self, payload):
        return self.request("POST", payload)

    def request(self, method, payload=None):
        body = None if payload is None else canonical(payload).encode("utf-8")
        if method not in ("GET", "POST") or (body is not None and len(body) > LIMIT):
            raise Refusal("invalid_request", True)
        connection = None
        try:
            # Explicit trust context avoids SSLKEYLOGFILE/proxy environment hooks.
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.load_default_certs()
            connection = self.factory("hollowheir.com", timeout=10, context=context)
            headers = {"Authorization": "Bearer " + self.config.token,
                       "Accept": "application/json", "Cache-Control": "no-store",
                       "User-Agent": "TradingAgents-Archive-ReadOnlySignals/1"}
            if body is not None:
                headers.update({"Content-Type": "application/json", "Content-Length": str(len(body))})
            connection.request(method, "/_archive/mt5/signals", body=body, headers=headers)
            response = connection.getresponse()
            if 300 <= response.status < 400 or response.status in (401, 403, 404):
                raise Refusal("signal_device_rejected", True)
            if response.status == 409:
                # An owner toggle/symbol change can race an in-flight snapshot.
                # Back off and GET fresh settings; never replay the rejected POST.
                raise Refusal("settings_or_snapshot_changed")
            if response.status not in (200, 201, 202):
                raise Refusal("transport_unavailable")
            raw = response.read(LIMIT + 1)
            if len(raw) > LIMIT:
                raise Refusal("invalid_response", True)
            result = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_json)
            if type(result) is not dict:
                raise Refusal("invalid_response", True)
            return result
        except Refusal:
            raise
        except Exception:
            raise Refusal("transport_unavailable") from None
        finally:
            if connection:
                connection.close()
