"""Posting to Slack as a bot, behind the slack_post shared credential.

This is not the caller's authorization. The tools write with one bot token
the agent holds, so whoever can call the agent can post to channels they
are not in themselves - as long as the bot is. The fence moves with the
destination's nature:

- public channel: being invited is enough. An invitation is an explicit,
  visible act in the channel itself; a declaration on top of it would not
  change who can pass.
- private channel: only those declared in the agent's environment. Writing
  into a place its members cannot see into demands a decision that is
  recorded in the repository.
- direct messages: no path at all.
"""

import logging
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from gete.redact import RedactRules
from gete.request_context import ToolCall, clear_tool_call, set_tool_call
from gete.runtime import slack_post

PUBLIC = "C0PUBLIC"
PRIVATE_OK = "C0PRIVATEOK"
PRIVATE_NG = "C0PRIVATENG"

LINK = "https://example.slack.com/archives/C0BQ2FS4RE1/p1787210851664919"

ACCOUNT_PATTERN = {"pattern": r"\b\d{7}\b", "replacement": "[account]"}


def _channel(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": PUBLIC,
        "name": "random",
        "is_private": False,
        "is_im": False,
        "is_mpim": False,
        "is_archived": False,
        "is_member": True,
    }
    base.update(overrides)
    return {"ok": True, "channel": base}


class Fake:
    """Routes by Slack method name; the ordered-response style cannot tell
    conversations.info from users.conversations, both being GET."""

    def __init__(
        self,
        info: dict[str, Any] | None = None,
        post: dict[str, Any] | None = None,
        pages: list[dict[str, Any]] | None = None,
        history: dict[str, Any] | None = None,
    ) -> None:
        self.info = info if info is not None else _channel()
        self.post = post if post is not None else {"ok": True, "ts": "1"}
        self.pages = pages or [{"ok": True, "channels": []}]
        self.history = history or {"ok": True, "messages": []}
        self.requests: list[httpx.Request] = []
        self.listed = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("users.conversations"):
            page = self.pages[min(self.listed, len(self.pages) - 1)]
            self.listed += 1
            return httpx.Response(200, json=page)
        if path.endswith("conversations.info"):
            return httpx.Response(200, json=self.info)
        if path.endswith("conversations.history"):
            return httpx.Response(200, json=self.history)
        if path.endswith("chat.postMessage"):
            return httpx.Response(200, json=self.post)
        raise AssertionError(f"unrouted Slack call: {path}")

    @property
    def posted(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("chat.postMessage")]


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-1-bot")
    monkeypatch.setenv("SLACK_ALLOWED_PRIVATE_CHANNELS", PRIVATE_OK)
    yield
    clear_tool_call()


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Any:
    def install(**kwargs: Any) -> Fake:
        routed = Fake(**kwargs)
        monkeypatch.setattr(
            slack_post,
            "_http_client",
            lambda: httpx.AsyncClient(transport=httpx.MockTransport(routed)),
        )
        return routed

    return install


def bind(user_id: str = "someone", rules: RedactRules | None = None) -> None:
    set_tool_call(ToolCall(SimpleNamespace(user_id=user_id), redact_rules=rules))


class TestPublicChannel:
    async def test_posts_where_the_bot_is_invited(self, fake: Any) -> None:
        """Widening the reach takes an /invite, not a release."""
        routed = fake()
        result = await slack_post.post_slack_message(PUBLIC, "hello", "#random")
        assert result["posted"] is True
        assert len(routed.posted) == 1

    async def test_refuses_where_the_bot_is_not_invited(self, fake: Any) -> None:
        fake(info=_channel(is_member=False))
        result = await slack_post.post_slack_message(PUBLIC, "hello", "#random")
        assert "invite" in result["error"]

    async def test_refuses_an_archived_channel(self, fake: Any) -> None:
        fake(info=_channel(is_archived=True))
        result = await slack_post.post_slack_message(PUBLIC, "hello", "#random")
        assert "error" in result


class TestPrivateChannel:
    async def test_posts_to_a_declared_channel(self, fake: Any) -> None:
        routed = fake(info=_channel(id=PRIVATE_OK, is_private=True))
        result = await slack_post.post_slack_message(PRIVATE_OK, "hello", "#random")
        assert result["posted"] is True
        assert len(routed.posted) == 1

    async def test_refuses_an_undeclared_channel(self, fake: Any) -> None:
        """Being invited is not enough here; members cannot see who can call
        the agent, so the allowance is recorded in the declaration."""
        fake(info=_channel(id=PRIVATE_NG, is_private=True))
        result = await slack_post.post_slack_message(PRIVATE_NG, "hello", "#random")
        assert "declar" in result["error"]

    async def test_refuses_every_private_channel_when_none_is_declared(
        self, fake: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SLACK_ALLOWED_PRIVATE_CHANNELS")
        fake(info=_channel(id=PRIVATE_OK, is_private=True))
        result = await slack_post.post_slack_message(PRIVATE_OK, "hello", "#random")
        assert "error" in result


class TestDirectMessage:
    @pytest.mark.parametrize("kind", ["is_im", "is_mpim"])
    async def test_no_path_to_direct_messages(self, fake: Any, kind: str) -> None:
        fake(info=_channel(**{kind: True}))
        result = await slack_post.post_slack_message("D0USER", "hello", "#random")
        assert "error" in result


class TestPreview:
    async def test_shows_the_channel_name(self, fake: Any) -> None:
        """An ID alone would hide a mix-up until after the post."""
        fake()
        result = await slack_post.preview_slack_post(PUBLIC, "hello")
        assert result["channel_name"] == "#random"

    async def test_does_not_post(self, fake: Any) -> None:
        routed = fake()
        await slack_post.preview_slack_post(PUBLIC, "hello")
        assert routed.posted == []

    async def test_says_it_posts_as_the_bot(self, fake: Any) -> None:
        fake()
        result = await slack_post.preview_slack_post(PUBLIC, "hello")
        assert "bot" in result["posts_as"].lower()

    async def test_refuses_what_the_post_would_refuse(self, fake: Any) -> None:
        """Passing the preview and failing the post would void the approval."""
        fake(info=_channel(id=PRIVATE_NG, is_private=True))
        result = await slack_post.preview_slack_post(PRIVATE_NG, "hello")
        assert "error" in result


class TestRefusal:
    async def test_text_the_policies_would_redact_is_not_posted(
        self, fake: Any
    ) -> None:
        """Refused outright, not masked: a masked post would sit in Slack
        with a hole its author cannot see into. Refused before any request."""
        routed = fake()
        bind(rules=RedactRules(patterns=((r"\b\d{7}\b", "[account]"),)))
        result = await slack_post.post_slack_message(
            PUBLIC, "transfer to 9876543 please", "#random"
        )
        assert "error" in result
        assert routed.requests == []

    async def test_the_preview_refuses_the_same_text(self, fake: Any) -> None:
        routed = fake()
        bind(rules=RedactRules(patterns=((r"\b\d{7}\b", "[account]"),)))
        result = await slack_post.preview_slack_post(PUBLIC, "account 9876543")
        assert "error" in result
        assert routed.requests == []

    async def test_without_rules_the_text_passes(self, fake: Any) -> None:
        """What counts as sensitive is the policies' decision, not this
        module's; with no patterns declared there is nothing to check."""
        fake()
        result = await slack_post.post_slack_message(
            PUBLIC, "transfer to 9876543 please", "#random"
        )
        assert result["posted"] is True

    async def test_without_a_token_nothing_is_sent(
        self, fake: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SLACK_BOT_TOKEN")
        routed = fake()
        result = await slack_post.post_slack_message(PUBLIC, "hello", "#random")
        assert "error" in result
        assert routed.requests == []

    async def test_a_user_token_is_not_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """xoxp- is somebody's credential; posting with it would post as them."""
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxp-1-user")
        assert slack_post.token() is None

    async def test_slacks_refusal_is_not_dressed_as_success(self, fake: Any) -> None:
        """Slack answers failures with ok:false inside an HTTP 200."""
        fake(post={"ok": False, "error": "msg_too_long"})
        result = await slack_post.post_slack_message(PUBLIC, "hello", "#random")
        assert "msg_too_long" in result["error"]

    async def test_an_unresolved_channel_is_not_posted_to(self, fake: Any) -> None:
        routed = fake(info={"ok": False, "error": "channel_not_found"})
        result = await slack_post.post_slack_message(PUBLIC, "hello", "#random")
        assert "error" in result
        assert routed.posted == []

    async def test_the_caller_is_logged(
        self, fake: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """One shared token writes for many callers; the log is what ties a
        post back to the instruction behind it."""
        fake()
        bind(user_id="someone")
        with caplog.at_level(logging.INFO):
            await slack_post.post_slack_message(PUBLIC, "hello", "#random")
        assert "caller=" in caplog.text
        assert "caller=unknown" not in caplog.text

    async def test_different_callers_leave_different_marks(
        self, fake: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        fake()
        with caplog.at_level(logging.INFO):
            bind(user_id="a")
            await slack_post.post_slack_message(PUBLIC, "one", "#random")
            bind(user_id="b")
            await slack_post.post_slack_message(PUBLIC, "two", "#random")
        lines = [r.getMessage() for r in caplog.records if "caller=" in r.getMessage()]
        assert len(lines) == 2
        assert lines[0] != lines[1]

    async def test_the_body_stays_out_of_the_log(
        self, fake: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """What was written sits in Slack; the log holds who wrote where."""
        fake()
        with caplog.at_level(logging.INFO):
            await slack_post.post_slack_message(PUBLIC, "the body text", "#random")
        assert "the body text" not in caplog.text


class TestApprovedTarget:
    """The preview shows a name so the user can check the target; the post
    must hold on to it, or the check checks nothing."""

    async def test_a_different_name_than_shown_is_refused(self, fake: Any) -> None:
        routed = fake()
        result = await slack_post.post_slack_message(PUBLIC, "hello", "#general")
        assert "error" in result
        assert routed.posted == []

    async def test_the_leading_hash_is_optional(self, fake: Any) -> None:
        """Refusing a matching name over its spelling invites workarounds."""
        fake()
        result = await slack_post.post_slack_message(PUBLIC, "hello", "random")
        assert result["posted"] is True

    async def test_the_previews_name_round_trips(self, fake: Any) -> None:
        fake()
        checked = await slack_post.preview_slack_post(PUBLIC, "hello")
        result = await slack_post.post_slack_message(
            PUBLIC, "hello", checked["channel_name"]
        )
        assert result["posted"] is True


class TestChannelByName:
    """People say names; the tools do the looking up.

    conversations.info takes only IDs and answers a name with
    channel_not_found, which reads as "not invited" and sends whoever is
    debugging in the wrong direction.
    """

    PAGE = {"ok": True, "channels": [{"id": PUBLIC, "name": "random"}]}

    async def test_a_name_resolves(self, fake: Any) -> None:
        fake(pages=[self.PAGE])
        result = await slack_post.preview_slack_post("#random", "hello")
        assert result["channel"] == PUBLIC

    async def test_the_leading_hash_is_optional(self, fake: Any) -> None:
        fake(pages=[self.PAGE])
        result = await slack_post.preview_slack_post("random", "hello")
        assert result["channel"] == PUBLIC

    async def test_an_id_is_used_as_it_is(self, fake: Any) -> None:
        """The listing runs for names only; not on every call in a large
        workspace."""
        routed = fake()
        await slack_post.preview_slack_post(PUBLIC, "hello")
        assert all(
            not r.url.path.endswith("users.conversations") for r in routed.requests
        )

    async def test_not_found_suggests_inviting(self, fake: Any) -> None:
        """Not "give the ID": inviting the bot is the fix, and the caller
        should not be sent to look IDs up."""
        fake(pages=[self.PAGE])
        result = await slack_post.preview_slack_post("#nowhere", "hello")
        assert "invite" in result["error"]
        assert "nowhere" in result["error"]

    async def test_pagination_is_followed(self, fake: Any) -> None:
        """Absent from the first page is not absent."""
        fake(
            pages=[
                {
                    "ok": True,
                    "channels": [{"id": "C0OTHER", "name": "general"}],
                    "response_metadata": {"next_cursor": "next"},
                },
                self.PAGE,
            ]
        )
        result = await slack_post.preview_slack_post("#random", "hello")
        assert result["channel"] == PUBLIC

    async def test_only_joined_channels_are_searched(self, fake: Any) -> None:
        """users.conversations lists what the bot has joined - the only
        places it could post to anyway. The workspace-wide listing is
        orders of magnitude larger and cannot be walked to the end."""
        routed = fake(pages=[self.PAGE])
        await slack_post.preview_slack_post("#random", "hello")
        assert any(r.url.path.endswith("users.conversations") for r in routed.requests)
        assert all(
            not r.url.path.endswith("conversations.list") for r in routed.requests
        )


class TestTokenDiagnosis:
    """When the token is wrong, say what is there - never the value.

    "no bot token" alone cannot tell an empty secret from a user token
    from a paste with a stray newline."""

    def test_a_user_token_is_named_by_its_shape(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxp-1-user")
        with caplog.at_level(logging.WARNING):
            assert slack_post.token() is None
        assert "xoxp-" in caplog.text

    def test_the_value_itself_never_reaches_the_log(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxp-1-secret-value-here")
        with caplog.at_level(logging.WARNING):
            slack_post.token()
        assert "secret-value-here" not in caplog.text

    def test_empty_is_reported_as_empty(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("SLACK_BOT_TOKEN", "")
        with caplog.at_level(logging.WARNING):
            assert slack_post.token() is None
        assert "empty" in caplog.text

    def test_surrounding_whitespace_is_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pasting from a browser brings a newline along; failing over one
        would be unreasonable."""
        monkeypatch.setenv("SLACK_BOT_TOKEN", "  xoxb-1-bot\n")
        assert slack_post.token() == "xoxb-1-bot"


class TestPermalink:
    """People paste message links; the channel and the timestamp are inside."""

    def test_channel_and_timestamp_are_extracted(self) -> None:
        assert slack_post.parse_permalink(LINK) == ("C0BQ2FS4RE1", "1787210851.664919")

    def test_a_link_into_a_thread_points_at_the_parent(self) -> None:
        """A reply goes to the thread, not onto one message inside it."""
        link = f"{LINK}?thread_ts=1787200000.000100&cid=C0BQ2FS4RE1"
        assert slack_post.parse_permalink(link) == ("C0BQ2FS4RE1", "1787200000.000100")

    @pytest.mark.parametrize(
        "value",
        [
            "#random",
            "C0PUBLIC",
            "https://example.com/x",
            "https://x.slack.com/archives/",
        ],
    )
    def test_what_is_not_a_link_stays_none(self, value: str) -> None:
        assert slack_post.parse_permalink(value) is None


class TestThreadReply:
    async def test_a_link_replies_into_the_thread(self, fake: Any) -> None:
        import json

        routed = fake(info=_channel(id="C0BQ2FS4RE1"))
        result = await slack_post.post_slack_message(LINK, "hello", "#random")
        assert result["posted"] is True
        body = json.loads(routed.posted[0].content)
        assert body["thread_ts"] == "1787210851.664919"

    async def test_the_preview_shows_the_reply_target(self, fake: Any) -> None:
        """A channel post and a thread reply differ in who sees them."""
        fake(info=_channel(id="C0BQ2FS4RE1"))
        result = await slack_post.preview_slack_post(LINK, "hello")
        assert result["reply_to"] == "1787210851.664919"

    async def test_a_plain_channel_does_not_thread(self, fake: Any) -> None:
        routed = fake()
        await slack_post.post_slack_message(PUBLIC, "hello", "#random")
        assert b"thread_ts" not in routed.posted[0].content

    async def test_the_linked_channel_passes_the_same_fences(self, fake: Any) -> None:
        """A thread is not a side door."""
        fake(info=_channel(id="C0BQ2FS4RE1", is_private=True))
        result = await slack_post.post_slack_message(LINK, "hello", "#random")
        assert "declar" in result["error"]


class TestPostTo:
    """A link says where; post_to says thread or channel. Conflating them
    would make "post this to the channel" impossible once a link is given.
    The chosen destination shows in the preview, before approval."""

    async def test_a_link_defaults_to_the_thread(self, fake: Any) -> None:
        fake(info=_channel(id="C0BQ2FS4RE1"))
        result = await slack_post.preview_slack_post(LINK, "hello")
        assert result["posts_to"] == "a reply in the thread"

    async def test_the_channel_can_be_chosen_over_the_thread(self, fake: Any) -> None:
        routed = fake(info=_channel(id="C0BQ2FS4RE1"))
        await slack_post.post_slack_message(LINK, "hello", "#random", "channel")
        assert b"thread_ts" not in routed.posted[0].content

    async def test_the_choice_shows_in_the_preview(self, fake: Any) -> None:
        fake(info=_channel(id="C0BQ2FS4RE1"))
        result = await slack_post.preview_slack_post(LINK, "hello", "channel")
        assert result["posts_to"] == "a post to the channel"
        assert "reply_to" not in result

    async def test_a_thread_without_a_link_is_refused_first(self, fake: Any) -> None:
        """Which thread is unknowable; falling back to the channel silently
        would post where nobody approved. Refused before any request."""
        routed = fake()
        result = await slack_post.preview_slack_post(PUBLIC, "hello", "thread")
        assert "link" in result["error"]
        assert routed.requests == []

    async def test_an_unknown_value_names_the_choices(self, fake: Any) -> None:
        routed = fake()
        result = await slack_post.preview_slack_post(PUBLIC, "hello", "somewhere")
        assert "auto" in result["error"]
        assert routed.requests == []

    async def test_a_link_in_post_to_names_the_right_slot(self, fake: Any) -> None:
        """ "Invalid value" alone sends the model trying other values; naming
        the slot ends it in one turn."""
        routed = fake()
        result = await slack_post.preview_slack_post(PUBLIC, "hello", LINK)
        assert "channel" in result["error"]
        assert routed.requests == []


class TestReadLinked:
    """One linked message, nothing around it.

    Whoever holds the link has seen the message; returning it adds little
    reach. A way to walk the channel would let anyone who can call the
    agent read every channel the bot is in - that is the door that stays
    shut, and it is shut here in code, not in the token's scope."""

    HISTORY = {
        "ok": True,
        "messages": [
            {"ts": "1787210851.664919", "user": "U0AUTHOR", "text": "the original"}
        ],
    }

    async def test_returns_the_message(self, fake: Any) -> None:
        fake(info=_channel(id="C0BQ2FS4RE1"), history=self.HISTORY)
        result = await slack_post.read_linked_message(LINK)
        assert result["text"] == "the original"
        assert result["posted_by"] == "U0AUTHOR"

    async def test_returns_a_mention_for_the_author(self, fake: Any) -> None:
        """The ID is already in the message; no directory permission needed."""
        fake(info=_channel(id="C0BQ2FS4RE1"), history=self.HISTORY)
        result = await slack_post.read_linked_message(LINK)
        assert result["mention"] == "<@U0AUTHOR>"

    async def test_no_author_means_no_mention(self, fake: Any) -> None:
        history = {
            "ok": True,
            "messages": [{"ts": "1", "user": "", "text": "the original"}],
        }
        fake(info=_channel(id="C0BQ2FS4RE1"), history=history)
        result = await slack_post.read_linked_message(LINK)
        assert result["mention"] == ""

    async def test_reads_only_through_a_link(self, fake: Any) -> None:
        """No reading by channel; that is the walking-the-channel door."""
        routed = fake()
        result = await slack_post.read_linked_message("#random")
        assert "link" in result["error"]
        assert routed.requests == []

    async def test_asks_for_exactly_one_message(self, fake: Any) -> None:
        routed = fake(info=_channel(id="C0BQ2FS4RE1"), history=self.HISTORY)
        await slack_post.read_linked_message(LINK)
        history = next(
            r for r in routed.requests if r.url.path.endswith("conversations.history")
        )
        assert history.url.params["limit"] == "1"
        assert history.url.params["latest"] == history.url.params["oldest"]

    async def test_passes_the_same_fences_as_posting(self, fake: Any) -> None:
        fake(info=_channel(id="C0BQ2FS4RE1", is_private=True))
        result = await slack_post.read_linked_message(LINK)
        assert "declar" in result["error"]

    async def test_a_missing_message_is_an_error(self, fake: Any) -> None:
        fake(info=_channel(id="C0BQ2FS4RE1"), history={"ok": True, "messages": []})
        result = await slack_post.read_linked_message(LINK)
        assert "error" in result

    async def test_the_read_is_logged_without_the_body(
        self, fake: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        fake(info=_channel(id="C0BQ2FS4RE1"), history=self.HISTORY)
        bind()
        with caplog.at_level(logging.INFO):
            await slack_post.read_linked_message(LINK)
        assert "caller=" in caplog.text
        assert "the original" not in caplog.text


class TestDocstrings:
    """Docstrings travel to the model as they are; what they leave out, the
    model fills in by guessing."""

    def _args(self, tool: Any) -> str:
        _, _, tail = (tool.__doc__ or "").partition("Args:")
        return tail

    @pytest.mark.parametrize(
        "tool", [slack_post.preview_slack_post, slack_post.post_slack_message]
    )
    def test_channel_documents_every_accepted_form(self, tool: Any) -> None:
        args = self._args(tool)
        assert "name" in args
        assert "ID" in args
        assert "link" in args

    @pytest.mark.parametrize(
        "tool", [slack_post.preview_slack_post, slack_post.post_slack_message]
    )
    def test_post_to_documents_its_values(self, tool: Any) -> None:
        args = self._args(tool)
        assert "post_to" in args
        for value in slack_post.POST_TO:
            assert value in args

    @pytest.mark.parametrize(
        "tool",
        [
            slack_post.preview_slack_post,
            slack_post.post_slack_message,
            slack_post.read_linked_message,
        ],
    )
    def test_every_parameter_is_described(self, tool: Any) -> None:
        import inspect

        args = self._args(tool)
        missing = [
            name
            for name in inspect.signature(tool).parameters
            if f"{name}:" not in args
        ]
        assert not missing, f"{tool.__name__} leaves parameters undocumented: {missing}"
