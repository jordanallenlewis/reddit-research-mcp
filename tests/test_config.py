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
