import importlib

import pytest


def test_create_app_requires_an_application_object():
    from app.web import create_app

    with pytest.raises(TypeError, match=r"create_app\(application\)"):
        create_app(None)


def test_create_app_registers_routes_and_keeps_the_exact_application():
    web = importlib.import_module("app.web")
    from app.web import core

    application = object()
    flask_app = web.create_app(application)

    assert flask_app.extensions["application"] is application
    assert core.app is flask_app
    assert web.app is flask_app
    assert web.application is core.application
    assert web.state is core.state
    assert web.health.state is core.state
    assert {"healthz", "index", "api_dashboard", "metrics"} <= set(flask_app.view_functions)


def test_create_app_resolves_state_for_the_current_flask_application():
    from app.web import create_app

    class FakeNodes:
        def __init__(self, data_plane_running):
            self._data_plane_running = data_plane_running

        def data_plane_running(self):
            return self._data_plane_running

        def ai_node_running(self):
            return False

    class FakeDnsFailover:
        @staticmethod
        def dns_failover_status():
            return {"enabled": False}

    class FakeApplication:
        def __init__(self, data_plane_running):
            self.nodes = FakeNodes(data_plane_running)
            self.dns_failover = FakeDnsFailover()

    first_application = FakeApplication(True)
    first_flask_app = create_app(first_application)
    second_application = FakeApplication(False)
    second_flask_app = create_app(second_application)

    with first_flask_app.test_client() as client:
        assert client.get("/healthz").json["data_plane_running"] is True
    with second_flask_app.test_client() as client:
        assert client.get("/healthz").json["data_plane_running"] is False


def test_metrics_cache_is_scoped_to_the_current_flask_application():
    from app import web
    from app.web import metrics

    class FakeApplication:
        def __init__(self, data_plane_running):
            self._data_plane_running = data_plane_running

        def data_plane_running(self):
            return self._data_plane_running

    first_flask_app = web.create_app(FakeApplication(True))
    second_flask_app = web.create_app(FakeApplication(False))

    with first_flask_app.app_context():
        assert metrics._data_plane_running_cached() == 1
    with second_flask_app.app_context():
        assert metrics._data_plane_running_cached() == 0


def test_create_app_does_not_construct_panel_state(monkeypatch):
    panel_module = importlib.import_module("app.state.panel")

    def fail_panel_state_constructor(*_args, **_kwargs):
        raise AssertionError("Web factory must not construct PanelState")

    monkeypatch.setattr(panel_module, "PanelState", fail_panel_state_constructor)
    from app.web import create_app

    create_app(object())
