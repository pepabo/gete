"""Posting to Slack as a bot: the tools behind the slack_post shared credential.

This is not the caller's authorization. Connection tools read with the token
the calling user authorized; these tools write with one bot token the agent
holds. Whoever can call the agent can post to channels they are not in
themselves - as long as the bot is - so the guardrails live here, next to
the credential, and a declaration can only switch the tools on:

- the fence moves with the destination: a public channel takes an
  invitation, a private channel a declaration, a direct message nothing
- the preview and the post judge a destination by the same rules, and the
  post checks the name the preview showed
- text the policies would redact is refused, not masked: a masked post
  would sit in Slack with a hole its author cannot see into
- who posted where goes to the log; what was posted does not
- reading is one linked message, never the channel around it

This is not impersonation: Slack shows the app's name on every post, and
no path posts as the calling user.
"""

import logging
import os
import re
import urllib.parse
from collections.abc import Callable
from typing import Any

import httpx

from gete.redact import redact_text
from gete.request_context import caller_fingerprint, current_tool_call
from gete.shared_credentials import SHARED_CREDENTIALS

logger = logging.getLogger(__name__)

BASE_URL = "https://slack.com/api"

TIMEOUT_SECONDS = 30.0

# Delivery puts the Secret Manager value here; gete terraform wires it from
# the project declaration's token_secret.
TOKEN_ENV = SHARED_CREDENTIALS["slack_post"].token_env

# Private channel IDs the agent may post to, comma separated, set through
# the agent's declared environment. Public channels are not listed here;
# inviting the bot is the visible, sufficient act.
PRIVATE_CHANNELS_ENV = "SLACK_ALLOWED_PRIVATE_CHANNELS"

# Only a bot token is accepted. xoxp- is somebody's credential; posting
# with it would post as that person.
BOT_PREFIX = "xoxb-"

POSTS_AS = "the bot (the post shows the app's name, never the calling user)"

# Where the post goes. auto: a thread when a link was given, else the channel.
POST_TO = ("auto", "thread", "channel")


def _http_client() -> httpx.AsyncClient:
    """One client per call; tests replace this with a mock transport."""
    return httpx.AsyncClient(timeout=TIMEOUT_SECONDS)


def declared_private_channels() -> tuple[str, ...]:
    raw = os.environ.get(PRIVATE_CHANNELS_ENV, "")
    return tuple(c.strip() for c in raw.split(",") if c.strip())


def _shape_of(value: str) -> str:
    """The shape of what is there - never the value or a part of it.

    "no bot token" alone cannot tell an empty secret from a user token
    from a paste with a stray newline.
    """
    for prefix in ("xoxb-", "xoxp-", "xoxe.xoxp-", "xoxa-", "xapp-"):
        if value.startswith(prefix):
            return f"it starts with {prefix}"
    return "it is not shaped like a Slack token"


def token() -> str | None:
    """The bot token, or None when it is missing or the wrong kind.

    The shape is checked so a user token is caught here: posting with one
    would go through and land as that person's own words, with no way to
    tell afterwards. Surrounding whitespace is dropped; a paste from a
    browser brings a newline along.
    """
    value = os.environ.get(TOKEN_ENV, "").strip()
    if not value:
        # Silence here would leave "not delivered" and "wrong value"
        # indistinguishable.
        logger.warning(
            "%s is empty; check that the secret has an enabled version", TOKEN_ENV
        )
        return None
    if not value.startswith(BOT_PREFIX):
        logger.warning(
            "%s does not hold a bot token (%s): %s. Use the Bot User OAuth "
            "Token from the Slack app's install page",
            TOKEN_ENV,
            BOT_PREFIX,
            _shape_of(value),
        )
        return None
    return value


def _policy_refusal(text: str) -> dict[str, Any] | None:
    """Refuse a body the policies' patterns would change, before any request.

    Refused, not masked: a masked post would sit in Slack with a hole its
    author cannot see into. What counts as sensitive is the installation's
    redact rules; with none declared there is nothing to check against.
    """
    call = current_tool_call()
    rules = call.redact_rules if call is not None else None
    if rules is None or not rules.patterns:
        return None
    if redact_text(text, rules) != text:
        return {
            "error": "the text holds something the policies would redact; "
            "it is not posted, masked or otherwise"
        }
    return None


async def _call(
    path: str, params: dict[str, Any] | None, body: dict[str, Any] | None
) -> dict[str, Any]:
    """Call Slack and hand back the payload, failures included.

    Slack answers failures with ok:false inside an HTTP 200; not reading
    ok would dress a permission problem as a success.
    """
    credential = token()
    if credential is None:
        return {"ok": False, "error": "there is no credential to act with"}

    headers = {"Authorization": f"Bearer {credential}"}
    try:
        async with _http_client() as client:
            if body is None:
                response = await client.get(
                    f"{BASE_URL}/{path}", params=params, headers=headers
                )
            else:
                response = await client.post(
                    f"{BASE_URL}/{path}", json=body, headers=headers
                )
            payload: dict[str, Any] = response.json()
    except (httpx.HTTPError, ValueError) as error:
        logger.warning(
            "Slack could not be reached path=%s %s", path, type(error).__name__
        )
        return {"ok": False, "error": "Slack could not be reached"}
    return payload


# A message permalink: https://<workspace>.slack.com/archives/<channel>/p<digits>
_PERMALINK = re.compile(
    r"^https://[\w.-]+\.slack\.com/archives/([A-Z0-9]+)/p(\d{10})(\d{6})"
)


def parse_permalink(value: str) -> tuple[str, str] | None:
    """The channel and the reply target inside a message permalink.

    People paste links: "reply to this" arrives as the message's permalink,
    and not taking it would send the user off to dissect the URL.

    A link to a message inside a thread carries thread_ts. The reply target
    is the parent, not that message, so thread_ts wins when present.
    """
    found = _PERMALINK.match(value.strip())
    if not found:
        return None
    channel, seconds, fraction = found.groups()

    query = urllib.parse.parse_qs(urllib.parse.urlparse(value).query)
    if parent := (query.get("thread_ts") or [""])[0]:
        return channel, parent
    return channel, f"{seconds}.{fraction}"


def _message_ts(value: str) -> str:
    """The timestamp of the message the link names.

    parse_permalink answers with the thread's parent, which is where a
    reply goes; reading needs the named message itself.
    """
    found = _PERMALINK.match(value.strip())
    if not found:
        return ""
    _, seconds, fraction = found.groups()
    return f"{seconds}.{fraction}"


def _destination(channel: str, post_to: str) -> tuple[str, str] | dict[str, Any]:
    """Reply in the thread, or post to the channel.

    A link does not force a thread: "post this to the channel" often points
    at the channel with a link. The choice shows in the preview, so a
    mix-up surfaces before approval.

    Returns (what the preview says, thread timestamp); an empty timestamp
    means the channel.
    """
    if post_to not in POST_TO:
        if parse_permalink(post_to):
            # Naming the right slot ends it in one turn; "invalid value"
            # alone sends the model trying other values.
            return {
                "error": "a message link goes in channel, not post_to; "
                f"post_to is one of {list(POST_TO)} and is usually omitted"
            }
        return {"error": f"post_to {post_to!r} is not one of {list(POST_TO)}"}

    link = parse_permalink(channel)
    if post_to == "channel" or (post_to == "auto" and link is None):
        return "a post to the channel", ""
    if link is None:
        # Which thread is unknowable; falling back to the channel silently
        # would post where nobody approved.
        return {"error": "replying to a thread needs the message's link"}
    return "a reply in the thread", link[1]


# Pages of the joined-channels listing to walk. Only channels the bot has
# joined appear there, so this is plenty.
LIST_PAGES = 5

LIST_SIZE = 1000


async def _resolve(channel: str) -> str | dict[str, Any]:
    """Turn what a person said into a channel ID.

    People say names. conversations.info takes only IDs and answers a name
    with channel_not_found, which reads as "not invited" and sends whoever
    is debugging in the wrong direction.

    The search runs over the channels the bot has joined
    (users.conversations) - the only places it could post to anyway. The
    workspace-wide listing is orders of magnitude larger and cannot be
    walked to the end; giving up there would send the user off to look up
    IDs, defeating the point of taking names.
    """
    if link := parse_permalink(channel):
        return link[0]

    name = channel.lstrip("#").strip()
    if not name:
        return {"error": "no channel was given"}
    # C is a channel (public or private), G an older private one, D a DM.
    # Real channel names are not all-uppercase alphanumerics.
    if (
        channel[0] in "CGD"
        and channel.upper() == channel
        and channel[1:].replace("_", "").isalnum()
    ):
        return channel

    cursor = ""
    for _ in range(LIST_PAGES):
        params = {
            "types": "public_channel,private_channel",
            "exclude_archived": "true",
            "limit": str(LIST_SIZE),
        }
        if cursor:
            params["cursor"] = cursor
        payload = await _call("users.conversations", params, None)
        if not payload.get("ok"):
            return {
                "error": "could not list the bot's channels: "
                f"{payload.get('error', 'unknown')}"
            }
        for found in payload.get("channels") or []:
            if found.get("name") == name:
                return str(found.get("id"))
        cursor = ((payload.get("response_metadata") or {}).get("next_cursor")) or ""
        if not cursor:
            return {
                "error": f"#{name} is not among the channels the bot has joined. "
                "Check the name, or invite the bot to that channel"
            }
    return {
        "error": f"could not finish looking for #{name}; the bot has joined "
        "too many channels. Give the channel ID instead"
    }


async def _channel(channel: str) -> dict[str, Any]:
    """The destination's nature: name, visibility, membership.

    The name is what the user checks the target by; the same answer also
    decides whether this is a place to post at all.
    """
    resolved = await _resolve(channel)
    if isinstance(resolved, dict):
        return resolved

    payload = await _call("conversations.info", {"channel": resolved}, None)
    if not payload.get("ok"):
        return {
            "error": f"could not look up the channel: {payload.get('error', 'unknown')}"
        }
    found = dict(payload.get("channel") or {})
    # Whatever the user said, posting and checking go by the resolved ID.
    found.setdefault("id", resolved)
    return found


def _refuse(found: dict[str, Any]) -> str | None:
    """Whether this is a place to act on; the reason when it is not.

    The preview and the post call this alike: a destination that passes
    the preview and fails the post would void the approval in between.
    """
    if error := found.get("error"):
        return str(error)
    if found.get("is_im") or found.get("is_mpim"):
        # Personal conversations; no path in from the outside.
        return "not posting to direct messages"
    if found.get("is_archived"):
        return "not posting to an archived channel"
    if found.get("is_private") and found.get("id") not in declared_private_channels():
        # Writing into a place its members cannot see into; the bot being
        # invited is not a decision anyone recorded.
        return (
            "not posting to an undeclared private channel. Declared: "
            f"{list(declared_private_channels()) or 'none'}. To allow one, add "
            f"its ID to {PRIVATE_CHANNELS_ENV} in the agent's declaration"
        )
    if not found.get("is_member"):
        return "the bot has not joined this channel; invite it first"
    return None


def _describe(found: dict[str, Any]) -> str:
    kind = "private" if found.get("is_private") else "public"
    return f"#{found.get('name', '?')} ({kind})"


def _same_channel_name(shown: str, found: dict[str, Any]) -> bool:
    """Whether the name the user saw is the place about to be posted to.

    Spelling does not decide it: refusing a matching name over a missing
    hash mark would invite workarounds.
    """
    return shown.strip().lstrip("#") == str(found.get("name", ""))


async def preview_slack_post(
    channel: str, text: str, post_to: str = "auto"
) -> dict[str, Any]:
    """Show what would be posted, without posting.

    Returns the destination's name and visibility, the text, and who the
    post will appear as. It does not post - that is post_slack_message,
    called only after the user approves - and it does not read the
    channel's members or messages.

    A destination the post would refuse (a direct message, an archived
    channel, an undeclared private channel, a channel the bot has not
    joined) is refused here too, as is text the policies would redact.

    Args:
        channel: The destination. A name ("#random"), an ID ("C0123456"),
            or a message link ("https://...slack.com/archives/C.../p...")
            all work - pass whatever the user gave. A link makes the post
            a reply in that thread by default.
        text: The text to post.
        post_to: Where the post goes: "auto", "thread", or "channel".
            Usually omitted - "auto" decides by whether a link was given.
            Pass "channel" only when a link was given but the post should
            go to the channel itself.
    """
    if refused := _policy_refusal(text):
        return refused

    destination = _destination(channel, post_to)
    if isinstance(destination, dict):
        return destination
    posts_to, thread = destination

    found = await _channel(channel)
    if refused_reason := _refuse(found):
        return {"error": refused_reason}

    shown: dict[str, Any] = {
        "channel": found.get("id", channel),
        "channel_name": f"#{found.get('name', '?')}",
        "visibility": "private" if found.get("is_private") else "public",
        "text": text,
        "posts_as": POSTS_AS,
        "confirm": "show this to the user and post only after they approve",
        # The post takes the same channel value the preview took, so what
        # was approved is what is posted - a link keeps its thread.
        "next": "once approved, call post_slack_message with "
        f"channel={channel!r} unchanged",
        # A channel post and a thread reply differ in who sees them; which
        # one this is belongs in the approval.
        "posts_to": posts_to,
    }
    if thread:
        shown["reply_to"] = thread
    return shown


async def post_slack_message(
    channel: str, text: str, channel_name: str, post_to: str = "auto"
) -> dict[str, Any]:
    """Post to Slack. Call only after the user approved the preview.

    Returns whether the post went through and its timestamp. The post
    appears as the bot, never as the calling user, and nothing is read
    back afterwards.

    channel_name ties the approval to the destination: when the name the
    preview showed is not the place this call resolves to, nothing is
    posted. The tool cannot know whether the user actually approved; that
    step is the instruction's to keep.

    Args:
        channel: The destination. A name ("#random"), an ID ("C0123456"),
            or a message link ("https://...slack.com/archives/C.../p...")
            all work - pass whatever the user gave. A link makes the post
            a reply in that thread by default.
        text: The text to post.
        channel_name: The channel_name the preview returned, unchanged.
        post_to: Where the post goes: "auto", "thread", or "channel".
            Usually omitted - "auto" decides by whether a link was given.
            Pass "channel" only when a link was given but the post should
            go to the channel itself.
    """
    if refused := _policy_refusal(text):
        return refused
    if token() is None:
        return {"error": "there is no credential to post with; nothing was sent"}

    destination = _destination(channel, post_to)
    if isinstance(destination, dict):
        return destination
    posts_to, thread = destination

    found = await _channel(channel)
    if refused_reason := _refuse(found):
        return {"error": refused_reason}

    if not _same_channel_name(channel_name, found):
        # The user approved a different place than this call resolves to.
        return {
            "error": f"the name checked ({channel_name}) is not the destination "
            f"({_describe(found)}); check the target again before posting"
        }

    # One shared token writes for many callers; the log is what ties a post
    # back to the instruction behind it. The body stays out: it sits in
    # Slack already, and the log must not hold a second copy.
    logger.info(
        "posting to Slack channel=%s caller=%s",
        _describe(found),
        caller_fingerprint(),
    )

    body: dict[str, Any] = {"channel": found.get("id", channel), "text": text}
    if thread:
        body["thread_ts"] = thread

    payload = await _call("chat.postMessage", None, body)
    if not payload.get("ok"):
        return {"error": f"Slack did not post it: {payload.get('error', 'unknown')}"}

    return {
        "posted": True,
        "channel_name": f"#{found.get('name', '?')}",
        "posted_to": posts_to,
        "posted_at": payload.get("ts", ""),
    }


async def read_linked_message(link: str) -> dict[str, Any]:
    """Read the one message a link names, and nothing around it.

    Returns that message's text, its author's ID, a ready-made mention
    ("<@U...>") for the author, and the timestamp. It does not return the
    messages before or after: whoever holds the link has seen the message,
    but a way to walk the channel would let anyone who can call the agent
    read every channel the bot is in. The token's scope cannot make that
    distinction; this code is where it is made.

    Reading passes the same fences as posting: no direct messages, no
    archived channels, no undeclared private channels, no channels the
    bot has not joined.

    Args:
        link: The message's permalink
            ("https://...slack.com/archives/C.../p..."). A channel name or
            ID is refused; only a linked message is read.
    """
    if parse_permalink(link) is None:
        return {"error": "pass the message's link; a channel is not read"}

    found = await _channel(link)
    if refused_reason := _refuse(found):
        return {"error": refused_reason}

    message_ts = _message_ts(link)
    payload = await _call(
        "conversations.history",
        {
            "channel": str(found.get("id", "")),
            "latest": message_ts,
            "oldest": message_ts,
            "inclusive": "true",
            "limit": "1",
        },
        None,
    )
    if not payload.get("ok"):
        return {
            "error": f"could not read the message: {payload.get('error', 'unknown')}"
        }

    messages = payload.get("messages") or []
    if not messages:
        return {
            "error": "no message at that link. A message inside a thread "
            "cannot be read through its parent's link"
        }

    message = messages[0]
    # What was read sits in Slack; the log holds who read where.
    logger.info(
        "reading a linked Slack message channel=%s caller=%s",
        _describe(found),
        caller_fingerprint(),
    )
    author = str(message.get("user", ""))
    return {
        "channel_name": f"#{found.get('name', '?')}",
        "posted_by": author,
        # The ID is already in the message; a mention needs no directory
        # permission. Bots and apps post without one - then there is none.
        "mention": f"<@{author}>" if author else "",
        "posted_at": message.get("ts", ""),
        "text": str(message.get("text", "")),
    }


# What the shared credential puts on a declaring agent, with each tool's
# effect. The result rides through the policies' redaction like any other.
TOOLS: list[tuple[Callable[..., Any], str]] = [
    (read_linked_message, "read"),
    (preview_slack_post, "read"),
    (post_slack_message, "write"),
]
