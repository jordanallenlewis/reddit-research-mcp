import pytest

from reddit_research_mcp import server


@pytest.fixture
def captured(monkeypatch):
    calls = []
    monkeypatch.setattr(server.mcp, "run", lambda **kw: calls.append(kw))
    monkeypatch.setattr(server, "load_config", lambda: None)
    return calls


def test_stdio_is_default(captured):
    server.main([])
    assert captured == [{"show_banner": False}]


def test_http_defaults_to_localhost(captured):
    server.main(["--transport", "http"])
    assert captured == [{"transport": "http", "host": "127.0.0.1", "port": 8000, "show_banner": False}]


def test_http_host_and_port(captured):
    server.main(["--transport", "http", "--host", "0.0.0.0", "--port", "9123"])
    assert captured[0]["host"] == "0.0.0.0" and captured[0]["port"] == 9123


def test_public_bind_warns(captured, caplog):
    server.log.addHandler(caplog.handler)
    try:
        server.main(["--transport", "http", "--host", "0.0.0.0"])
        assert "anyone who can reach" in caplog.text
    finally:
        server.log.removeHandler(caplog.handler)


def test_localhost_bind_does_not_warn(captured, caplog):
    server.log.addHandler(caplog.handler)
    try:
        server.main(["--transport", "http"])
        assert "anyone who can reach" not in caplog.text
    finally:
        server.log.removeHandler(caplog.handler)


def test_unknown_transport_exits(captured):
    with pytest.raises(SystemExit):
        server.main(["--transport", "sse"])
    assert captured == []
