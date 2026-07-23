"""System tests for yo.florist — run against the live deployment.

    python3 -m unittest tests/test_system.py -v

Override targets with env vars, e.g.:
    SITE_BASE=http://localhost:8000 API_BASE=http://localhost:8080 python3 -m unittest ...
"""
import json
import os
import unittest
import urllib.error
import urllib.request

SITE = os.environ.get("SITE_BASE", "https://yo.florist")
API = os.environ.get("API_BASE", "https://api.yo.florist")
DOCS = os.environ.get("DOCS_BASE", "https://docs.yo.florist")
TIMEOUT = 20


def get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.status, dict(r.headers), r.read()


def get_json(url):
    status, headers, body = get(url)
    return status, headers, json.loads(body)


def post_json(url, payload):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.status, json.loads(r.read())


def expect_http_error(testcase, code, fn, *args):
    with testcase.assertRaises(urllib.error.HTTPError) as ctx:
        fn(*args)
    testcase.assertEqual(ctx.exception.code, code)
    return ctx.exception


class TestLandingPage(unittest.TestCase):
    def test_homepage_serves(self):
        status, _, body = get(SITE + "/")
        self.assertEqual(status, 200)
        html = body.decode()
        self.assertIn("Flower Aggregator", html)
        self.assertIn('id="hero-search"', html)
        self.assertIn("/checkout", html)

    def test_checkout_page_serves(self):
        status, _, body = get(SITE + "/checkout?iso2=ID&country=Indonesia&florist=Test&fcity=Jakarta")
        self.assertEqual(status, 200)
        html = body.decode()
        self.assertIn("Checkout", html)
        self.assertIn("addr-suggest", html)

    def test_thanks_page_serves(self):
        status, _, body = get(SITE + "/thanks.html?order=YF-TEST")
        self.assertEqual(status, 200)
        self.assertIn("Thank you", body.decode())

    def test_flowers_catalog_page_serves(self):
        status, _, body = get(SITE + "/flowers")
        self.assertEqual(status, 200)
        self.assertIn("Real bouquets", body.decode())

    def test_http_redirects_to_https(self):
        req = urllib.request.Request(SITE.replace("https://", "http://") + "/")

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        opener = urllib.request.build_opener(NoRedirect)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            opener.open(req, timeout=TIMEOUT)
        self.assertIn(ctx.exception.code, (301, 308))
        self.assertTrue(ctx.exception.headers["Location"].startswith("https://"))


class TestDocs(unittest.TestCase):
    def test_swagger_ui(self):
        status, _, body = get(DOCS + "/")
        self.assertEqual(status, 200)
        self.assertIn("Swagger UI", body.decode())

    def test_openapi_schema(self):
        status, _, spec = get_json(DOCS + "/openapi.json")
        self.assertEqual(status, 200)
        for path in ("/v1/health", "/v1/countries", "/v1/florists", "/v1/search", "/v1/orders"):
            self.assertIn(path, spec["paths"], f"{path} missing from OpenAPI spec")


class TestDirectory(unittest.TestCase):
    def test_health(self):
        _, _, data = get_json(API + "/v1/health")
        self.assertEqual(data["status"], "ok")
        self.assertGreaterEqual(data["countries"], 190)

    def test_countries_list(self):
        _, _, data = get_json(API + "/v1/countries")
        self.assertEqual(data["count"], len(data["countries"]))
        isos = {c["iso2"] for c in data["countries"]}
        for expected in ("US", "GB", "ID", "JP", "BR", "NG", "FR"):
            self.assertIn(expected, isos)

    def test_florist_by_iso_code(self):
        _, _, data = get_json(API + "/v1/florists?country=FR")
        self.assertEqual(data["iso2"], "FR")
        self.assertTrue(data["name"])
        self.assertTrue(data["website"].startswith("http"))

    def test_florist_by_country_name_case_insensitive(self):
        _, _, data = get_json(API + "/v1/florists?country=indonesia")
        self.assertEqual(data["iso2"], "ID")

    def test_florist_unknown_country_404(self):
        expect_http_error(self, 404, get_json, API + "/v1/florists?country=Atlantis")

    def test_cors_header_present(self):
        _, headers, _ = get(API + "/v1/health", {"Origin": "https://yo.florist"})
        self.assertEqual(headers.get("access-control-allow-origin"), "*")


class TestSearch(unittest.TestCase):
    def test_search_by_city_and_country(self):
        _, _, data = get_json(API + "/v1/search?q=Jakarta%2C%20Indonesia&date=2026-08-01")
        self.assertEqual(data["date"], "2026-08-01")
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["matches"][0]["iso2"], "ID")

    def test_search_by_city_alone(self):
        _, _, data = get_json(API + "/v1/search?q=Nairobi")
        self.assertTrue(any(m["iso2"] == "KE" for m in data["matches"]))

    def test_search_no_match(self):
        _, _, data = get_json(API + "/v1/search?q=zzzznowhere")
        self.assertEqual(data["count"], 0)

    def test_search_query_too_short_422(self):
        expect_http_error(self, 422, get_json, API + "/v1/search?q=a")


class TestCatalog(unittest.TestCase):
    def test_catalog_endpoint_shape(self):
        _, _, data = get_json(API + "/v1/catalog?country=GB")
        self.assertEqual(data["iso2"], "GB")
        self.assertTrue(data["florist"])
        self.assertIsInstance(data["items"], list)
        self.assertEqual(data["count"], len(data["items"]))
        # every item must honestly attribute its source florist
        for item in data["items"]:
            self.assertIn("source", item)
            self.assertTrue(item["source"]["florist"])
            self.assertTrue(item["source"]["website"])
            self.assertIsNotNone(item.get("price"))

    def test_catalog_unknown_country_404(self):
        expect_http_error(self, 404, get_json, API + "/v1/catalog?country=Atlantis")

    def test_catalog_browse_all(self):
        _, _, data = get_json(API + "/v1/catalog?limit=5")
        self.assertGreaterEqual(data["total"], 1)
        self.assertLessEqual(len(data["items"]), 5)
        for item in data["items"]:
            self.assertIn("iso2", item)
            self.assertIn("source", item)
            self.assertIsNotNone(item.get("price"))

    def test_catalog_browse_pagination(self):
        _, _, p1 = get_json(API + "/v1/catalog?limit=2&offset=0")
        _, _, p2 = get_json(API + "/v1/catalog?limit=2&offset=2")
        if p1["total"] > 4:
            self.assertNotEqual(
                [i["title"] for i in p1["items"]],
                [i["title"] for i in p2["items"]],
            )

    def test_countries_include_city_for_autocomplete(self):
        _, _, data = get_json(API + "/v1/countries")
        self.assertIn("city", data["countries"][0])

    def test_search_matches_carry_items_key(self):
        _, _, data = get_json(API + "/v1/search?q=Indonesia")
        self.assertGreaterEqual(data["count"], 1)
        self.assertIn("items", data["matches"][0])
        self.assertIsInstance(data["matches"][0]["items"], list)


class TestGeo(unittest.TestCase):
    def test_autocomplete_shape(self):
        _, _, data = get_json(API + "/v1/geo/autocomplete?q=Jalan%20Raya%20Canggu&country=id")
        self.assertIn(data["provider"], ("google", "photon", "none"))
        self.assertIsInstance(data["suggestions"], list)
        for s in data["suggestions"]:
            self.assertTrue(s["label"])
            # each suggestion must be resolvable: inline fields or a place_id
            self.assertTrue(s["fields"] is not None or s["place_id"])

    def test_autocomplete_query_too_short_422(self):
        expect_http_error(self, 422, get_json, API + "/v1/geo/autocomplete?q=ab")

    def test_cors_preflight_allows_order_post(self):
        req = urllib.request.Request(
            API + "/v1/orders",
            method="OPTIONS",
            headers={
                "Origin": "https://yo.florist",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            self.assertEqual(r.status, 200)
            allowed = r.headers.get("access-control-allow-methods", "")
            self.assertIn("POST", allowed)


class TestOrders(unittest.TestCase):
    """Purchase-order placeholder — Stripe checkout activates once keys land."""

    ORDER = {
        "country": "ID",
        "customer_name": "System Test",
        "email": "systemtest@yo.florist",
        "address": "Jl. Test No. 1, Jakarta",
        "delivery_date": "2026-08-01",
        "budget_usd": 65,
        "message": "system test order",
    }

    def assert_payment_contract(self, payment):
        """Live mode must hand back a Stripe checkout URL; placeholder must not."""
        self.assertEqual(payment["provider"], "stripe")
        self.assertIn(payment["mode"], ("live", "placeholder"))
        if payment["mode"] == "live":
            self.assertTrue(payment["checkout_url"].startswith("https://checkout.stripe.com/"))
        else:
            self.assertIsNone(payment["checkout_url"])

    def test_create_and_fetch_order(self):
        status, data = post_json(API + "/v1/orders", self.ORDER)
        self.assertEqual(status, 201)
        self.assertTrue(data["order_id"].startswith("YF-"))
        self.assertEqual(data["status"], "pending_payment")
        self.assertEqual(data["routed_to"]["iso2"], "ID")
        self.assert_payment_contract(data["payment"])

        _, _, fetched = get_json(API + f"/v1/orders/{data['order_id']}")
        self.assertEqual(fetched["id"], data["order_id"])
        self.assertEqual(fetched["status"], "pending_payment")
        # status endpoint must not leak PII or Stripe session internals
        for pii in ("email", "address", "customer_name", "message", "stripe_session_id"):
            self.assertNotIn(pii, fetched)

    def test_create_order_with_catalog_item(self):
        payload = dict(
            self.ORDER,
            budget_usd=None,
            item_title="Test Bouquet — Peony Dream",
            item_price=59.5,
            item_currency="USD",
            item_url="https://example.com/products/peony-dream",
            recipient_name="Test Recipient",
            recipient_phone="+62 811 000 000",
            street="Jl. Test No. 1",
            city="Jakarta",
            postal_code="12345",
            delivery_instructions="leave with concierge",
            lat=-6.2,
            lng=106.8,
        )
        status, data = post_json(API + "/v1/orders", payload)
        self.assertEqual(status, 201)
        self.assertEqual(data["item"]["title"], "Test Bouquet — Peony Dream")
        self.assertEqual(data["item"]["price"], 59.5)
        self.assertTrue(data["item"]["fulfilled_by"])
        self.assert_payment_contract(data["payment"])

    def test_order_with_delivery_time_window(self):
        payload = dict(self.ORDER, delivery_time="morning (9am–12pm)")
        status, data = post_json(API + "/v1/orders", payload)
        self.assertEqual(status, 201)
        self.assertEqual(data["delivery"]["time"], "morning (9am–12pm)")
        self.assertFalse(data["delivery"]["confirm_time_with_customer"])

    def test_order_confirm_time_with_customer(self):
        payload = dict(self.ORDER, delivery_time=None, confirm_time_with_customer=True)
        status, data = post_json(API + "/v1/orders", payload)
        self.assertEqual(status, 201)
        self.assertIsNone(data["delivery"]["time"])
        self.assertTrue(data["delivery"]["confirm_time_with_customer"])

    def test_order_unknown_country_404(self):
        bad = dict(self.ORDER, country="Atlantis")
        expect_http_error(self, 404, post_json, API + "/v1/orders", bad)

    def test_order_invalid_email_422(self):
        bad = dict(self.ORDER, email="not-an-email")
        expect_http_error(self, 422, post_json, API + "/v1/orders", bad)

    def test_order_missing_address_422(self):
        bad = {k: v for k, v in self.ORDER.items() if k != "address"}
        expect_http_error(self, 422, post_json, API + "/v1/orders", bad)

    def test_unknown_order_404(self):
        expect_http_error(self, 404, get_json, API + "/v1/orders/YF-DOESNOTEXIST")


if __name__ == "__main__":
    unittest.main(verbosity=2)
