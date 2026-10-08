import logging

import pytest
from fastmcp.exceptions import ToolError
from fixtures import run

from reddit_research_mcp import server
from reddit_research_mcp.reddit import DEFAULT_USER_AGENT, ConfigError, RedditClient, load_config


def test_anonymous_by_default():
    cfg = load_config({})
    assert cfg.mode == "anonymous"
    assert cfg.user_agent == DEFAULT_USER_AGENT
    assert DEFAULT_USER_AGENT.startswith("reddit-research-mcp/")


def test_app_and_user_modes():
    assert load_config({"REDDIT_CLIENT_ID": "id1", "REDDIT_CLIENT_SECRET": "s1"}).mode == "app"
    cfg = load_config({"REDDIT_CLIENT_ID": "id1", "REDDIT_CLIENT_SECRET": "s1", "REDDIT_REFRESH_TOKEN": "r1"})
    assert cfg.mode == "user"
    assert "s1" not in repr(cfg) and "r1" not in repr(cfg)


def test_whitespace_values_count_as_unset():
    assert load_config({"REDDIT_CLIENT_ID": "  ", "REDDIT_CLIENT_SECRET": ""}).mode == "anonymous"


@pytest.mark.parametrize(
    "env, fragment",
    [
        ({"REDDIT_CLIENT_ID": "id1"}, "REDDIT_CLIENT_SECRET is not"),
        ({"REDDIT_CLIENT_SECRET": "s1"}, "REDDIT_CLIENT_ID is not"),
        ({"REDDIT_REFRESH_TOKEN": "r1"}, "without REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET"),
        ({"REDDIT_CLIENT_ID": "id1", "REDDIT_REFRESH_TOKEN": "r1"}, "REDDIT_CLIENT_SECRET is not"),
        ({"REDDIT_USER_AGENT": "two\nlines"}, "single line"),
    ],
)
def test_partial_env_fails_fast(env, fragment):
    with pytest.raises(ConfigError, match=fragment):
        load_config(env)


def test_user_agent_override():
    assert load_config({"REDDIT_USER_AGENT": "research-bot/1.0 (by u/someone)"}).user_agent == (
        "research-bot/1.0 (by u/someone)"
    )


def test_bad_env_surfaces_as_tool_error_not_import_failure(monkeypatch):
    monkeypatch.setenv("REDDIT_CLIENT_ID", "id1")
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    server.set_client(RedditClient())  # constructing the client reads nothing
    with pytest.raises(ToolError) as e:
        run(server.browse_subreddit(subreddit="testsub"))
    assert str(e.value).startswith("Configuration error: REDDIT_CLIENT_ID is set but REDDIT_CLIENT_SECRET is not")


def test_client_creation_is_lazy():
    c = RedditClient()
    assert c._http is None and c._config is None


SENTINELS = {
    "REDDIT_CLIENT_ID": "SENTINEL_CLIENT_ID_VALUE",
    "REDDIT_CLIENT_SECRET": "SENTINEL_SECRET_VALUE",
    "REDDIT_REFRESH_TOKEN": "SENTINEL_REFRESH_TOKEN",
}


def test_repr_and_str_never_show_credentials():
    cfg = load_config({**SENTINELS, "REDDIT_USER_AGENT": "research-bot/1.0"})
    for text in (repr(cfg), str(cfg), f"{cfg}", f"{cfg!r}"):
        assert not any(v in text for v in SENTINELS.values())
    assert "research-bot/1.0" in repr(cfg)


@pytest.mark.parametrize(
    "present, name",
    [
        (("REDDIT_CLIENT_ID",), "REDDIT_CLIENT_SECRET"),
        (("REDDIT_CLIENT_SECRET",), "REDDIT_CLIENT_ID"),
        (("REDDIT_REFRESH_TOKEN",), "REDDIT_CLIENT_ID"),
        (("REDDIT_CLIENT_ID", "REDDIT_REFRESH_TOKEN"), "REDDIT_CLIENT_SECRET"),
        (("REDDIT_CLIENT_SECRET", "REDDIT_REFRESH_TOKEN"), "REDDIT_CLIENT_ID"),
    ],
)
def test_config_errors_name_variables_and_never_values(present, name):
    env = {k: SENTINELS[k] for k in present}
    with pytest.raises(ConfigError) as e:
        load_config(env)
    assert name in str(e.value)
    assert not any(v in str(e.value) for v in SENTINELS.values())


@pytest.mark.parametrize(
    "ua", ["SENTINEL_UA_VALUE" + "x" * 300, "SENTINEL_UA_VALUE\x00nul", "SENTINEL_UA_VALUE caf\u00e9"]
)
def test_bad_user_agent_is_rejected_without_echoing_it(ua):
    with pytest.raises(ConfigError, match="REDDIT_USER_AGENT") as e:
        load_config({"REDDIT_USER_AGENT": ua})
    assert "SENTINEL_UA_VALUE" not in str(e.value)


def test_default_user_agent_has_no_personal_details():
    ua = DEFAULT_USER_AGENT
    assert ua.isascii() and "@" not in ua and "/Users/" not in ua and "/home/" not in ua
    assert load_config({}).user_agent == ua


def test_startup_with_bad_config_logs_names_only_and_still_serves(monkeypatch, capsys):
    for k in ("REDDIT_CLIENT_ID", "REDDIT_REFRESH_TOKEN"):
        monkeypatch.setenv(k, SENTINELS[k])
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("REDDIT_RESEARCH_MCP_LOG_LEVEL", "DEBUG")
    served = []
    monkeypatch.setattr(server.mcp, "run", lambda **kw: served.append(kw))
    log = logging.getLogger("reddit_research_mcp")
    before, level, propagate = list(log.handlers), log.level, log.propagate
    try:
        server.main([])
    finally:
        log.handlers[:] = before
        log.setLevel(level)
        log.propagate = propagate
    err = capsys.readouterr().err
    assert served, "the server must still start"
    assert "configuration error" in err and "REDDIT_CLIENT_SECRET" in err
    assert not any(v in err for v in SENTINELS.values())
    assert "Traceback" not in err


def test_tool_error_for_bad_config_has_no_values(monkeypatch):
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", SENTINELS["REDDIT_CLIENT_SECRET"])
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_REFRESH_TOKEN", raising=False)
    server.set_client(RedditClient())
    with pytest.raises(ToolError) as e:
        run(server.browse_subreddit(subreddit="testsub"))
    assert "REDDIT_CLIENT_ID is set" not in str(e.value) and "REDDIT_CLIENT_ID" in str(e.value)
    assert SENTINELS["REDDIT_CLIENT_SECRET"] not in str(e.value)
