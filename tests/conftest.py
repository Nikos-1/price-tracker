"""Τα tests μιλούν στον server όπως το παράθυρο της εφαρμογής: με την κεφαλίδα X-PriceTracker."""
import flask.testing
import pytest


@pytest.fixture(autouse=True)
def _app_header(monkeypatch):
    orig = flask.testing.FlaskClient.__init__

    def init(self, *a, **k):
        orig(self, *a, **k)
        self.environ_base.setdefault("HTTP_X_PRICETRACKER", "1")

    monkeypatch.setattr(flask.testing.FlaskClient, "__init__", init)
