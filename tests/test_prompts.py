from fastmcp import Client
from fixtures import run

from reddit_research_mcp import server

TOOLS = [
    "search_subreddits",
    "get_subreddit_info",
    "search_reddit",
    "get_post",
    "expand_comments",
    "get_posts",
    "get_user_activity",
    "find_other_discussions",
]


def test_prompt_is_listed():
    async def go():
        async with Client(server.mcp) as c:
            return await c.list_prompts()

    result = run(go())
    assert len(result) == 1
    prompt = result[0]
    assert prompt.name == "reddit_research"
    assert len(prompt.arguments) == 1
    assert prompt.arguments[0].name == "question"
    assert prompt.arguments[0].required is True


def test_prompt_text():
    async def go():
        async with Client(server.mcp) as c:
            return await c.get_prompt("reddit_research", {"question": "best budget mechanical keyboard"})

    result = run(go())
    messages = result.messages
    assert len(messages) == 1
    text = messages[0].content.text
    assert text.startswith("Research this question using Reddit:")
    assert "best budget mechanical keyboard" in text
    assert "untrusted" in text
    assert "permalink" in text


def test_prompt_names_only_real_tools():
    async def go():
        async with Client(server.mcp) as c:
            names = {t.name for t in await c.list_tools()}
            result = await c.get_prompt("reddit_research", {"question": "best budget mechanical keyboard"})
            return names, result.messages[0].content.text

    names, text = run(go())
    for name in TOOLS:
        assert name in text
        assert name in names
