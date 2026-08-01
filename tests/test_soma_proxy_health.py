import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "serving" / "soma_proxy.py"
SPEC = importlib.util.spec_from_file_location("soma_proxy_under_test", MODULE_PATH)
soma_proxy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = soma_proxy
SPEC.loader.exec_module(soma_proxy)


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeHTTP:
    def __init__(self, *, get_response=None, post_response=None, get_exc=None, post_exc=None):
        self.get_response = get_response
        self.post_response = post_response
        self.get_exc = get_exc
        self.post_exc = post_exc
        self.get_calls = []
        self.post_calls = []

    async def get(self, path, **kwargs):
        self.get_calls.append((path, kwargs))
        if self.get_exc:
            raise self.get_exc
        return self.get_response

    async def post(self, path, **kwargs):
        self.post_calls.append((path, kwargs))
        if self.post_exc:
            raise self.post_exc
        return self.post_response


class FakeRedis:
    def get(self, key):
        if key == "taey:soma:vprop":
            return "{}"
        return None


class SomaProxyHealthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._original_liveness_required = soma_proxy.TAEY_LIVENESS_REQUIRED
        self._original_probe_timeout = soma_proxy.VLLM_HEALTH_PROBE_TIMEOUT_SECS
        self._original_cache_secs = soma_proxy.VLLM_HEALTH_CACHE_SECS
        soma_proxy.TAEY_LIVENESS_REQUIRED = False
        soma_proxy.VLLM_HEALTH_PROBE_TIMEOUT_SECS = 10
        soma_proxy.VLLM_HEALTH_CACHE_SECS = 30
        soma_proxy._health_generation_cache = {"expires_at": 0.0, "result": None}
        soma_proxy._health_generation_lock = None
        soma_proxy._redis = None
        soma_proxy._last_liveness_error = ""
        soma_proxy._last_liveness_error_at = 0.0
        soma_proxy._last_liveness_success_at = 0.0

    async def asyncTearDown(self):
        soma_proxy.TAEY_LIVENESS_REQUIRED = self._original_liveness_required
        soma_proxy.VLLM_HEALTH_PROBE_TIMEOUT_SECS = self._original_probe_timeout
        soma_proxy.VLLM_HEALTH_CACHE_SECS = self._original_cache_secs
        soma_proxy._health_generation_cache = {"expires_at": 0.0, "result": None}
        soma_proxy._health_generation_lock = None
        soma_proxy._http = None
        soma_proxy._redis = None

    async def test_catalogue_healthy_but_generation_dead_returns_503(self):
        fake_http = FakeHTTP(
            get_response=FakeResponse(200, {"data": [{"id": "ep3"}]}),
            post_exc=soma_proxy.httpx.TimeoutException("decode queue did not answer"),
        )
        soma_proxy._http = fake_http

        response = await soma_proxy.health()
        payload = json.loads(response.body)

        self.assertEqual(503, response.status_code)
        self.assertEqual("unhealthy", payload["status"])
        self.assertEqual("unhealthy", payload["vllm"]["status"])
        self.assertTrue(payload["vllm"]["catalogue_ok"])
        self.assertFalse(payload["vllm"]["generation_ok"])
        self.assertEqual("/v1/models", fake_http.get_calls[0][0])
        self.assertEqual("/v1/chat/completions", fake_http.post_calls[0][0])
        self.assertEqual(10, fake_http.post_calls[0][1]["timeout"])

    async def test_degraded_returns_200_with_health_status_header(self):
        fake_http = FakeHTTP(
            get_exc=soma_proxy.httpx.ConnectError("catalogue unavailable"),
            post_response=FakeResponse(
                200,
                {"choices": [{"message": {"content": "ok"}, "finish_reason": "length"}]},
            ),
        )
        soma_proxy._http = fake_http

        response = await soma_proxy.health()
        payload = json.loads(response.body)

        self.assertEqual(200, response.status_code)
        self.assertEqual("degraded", payload["status"])
        self.assertEqual("degraded", response.headers["X-Health-Status"])
        self.assertEqual("degraded", payload["vllm"]["status"])
        self.assertFalse(payload["vllm"]["catalogue_ok"])
        self.assertTrue(payload["vllm"]["generation_ok"])

    async def test_generation_probe_is_cached(self):
        fake_http = FakeHTTP(
            get_response=FakeResponse(200, {"data": [{"id": "ep3"}]}),
            post_response=FakeResponse(
                200,
                {"choices": [{"message": {"content": "ok"}, "finish_reason": "length"}]},
            ),
        )
        soma_proxy._http = fake_http
        soma_proxy._redis = FakeRedis()

        with patch.object(soma_proxy, "_reconcile_registered_liveness", return_value=(0, 0, ["taey"])):
            first = await soma_proxy.health()
            second = await soma_proxy.health()

        first_payload = json.loads(first.body)
        second_payload = json.loads(second.body)

        self.assertEqual(200, first.status_code)
        self.assertEqual(200, second.status_code)
        self.assertEqual("healthy", first_payload["status"])
        self.assertEqual("healthy", second_payload["status"])
        self.assertFalse(first_payload["vllm"]["generation"]["cached"])
        self.assertTrue(second_payload["vllm"]["generation"]["cached"])
        self.assertEqual(2, len(fake_http.get_calls))
        self.assertEqual(1, len(fake_http.post_calls))


if __name__ == "__main__":
    unittest.main()
