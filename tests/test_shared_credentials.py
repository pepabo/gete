"""Declaring a shared credential: tools that write with the agent's own token.

A connection reads with the caller's token; a shared credential is the
opposite trust model - one credential, held by the agent, used for whoever
calls it. The declaration is a top-level key so that difference is visible
where the agent is reviewed, not buried in a tool entry.
"""

from typing import Any

import pytest

from gete.declaration import Agent
from gete.policies import Policy, has_write_tools, policy_applies
from gete.schema import problems
from gete.shared_credentials import SHARED_CREDENTIALS

AGENT: dict[str, Any] = {
    "name": "release-notes",
    "display_name": "Release notes",
    "description": "Drafts release notes and posts them once approved",
    "model": "gemini-2.5-flash",
    "instruction": "./instruction.md",
}

GETE: dict[str, Any] = {
    "version": 1,
    "project": "example-project",
    "location": "us-central1",
}


class TestAgentDeclaration:
    def test_slack_post_is_accepted(self) -> None:
        document = {**AGENT, "shared_credentials": ["slack_post"]}
        assert problems("agent", document) == []

    def test_an_unknown_name_is_rejected(self) -> None:
        document = {**AGENT, "shared_credentials": ["slack_search"]}
        assert problems("agent", document) != []

    def test_a_repeated_name_is_rejected(self) -> None:
        document = {**AGENT, "shared_credentials": ["slack_post", "slack_post"]}
        assert problems("agent", document) != []

    def test_the_agent_exposes_what_it_declared(self, tmp_path: Any) -> None:
        agent = Agent(
            directory=tmp_path, data={**AGENT, "shared_credentials": ["slack_post"]}
        )
        assert agent.shared_credentials == ("slack_post",)

    def test_nothing_declared_reads_as_empty(self, tmp_path: Any) -> None:
        agent = Agent(directory=tmp_path, data=AGENT)
        assert agent.shared_credentials == ()


class TestProjectDeclaration:
    def test_the_token_secret_is_accepted(self) -> None:
        document = {
            **GETE,
            "shared_credentials": {"slack_post": {"token_secret": "slack-bot-token"}},
        }
        assert problems("gete", document) == []

    def test_an_unknown_credential_is_rejected(self) -> None:
        document = {
            **GETE,
            "shared_credentials": {"slack_search": {"token_secret": "x"}},
        }
        assert problems("gete", document) != []

    def test_an_unknown_key_is_rejected(self) -> None:
        document = {
            **GETE,
            "shared_credentials": {
                "slack_post": {"token_secret": "x", "token": "sneaked-in"}
            },
        }
        assert problems("gete", document) != []

    def test_an_empty_secret_name_is_rejected(self) -> None:
        document = {
            **GETE,
            "shared_credentials": {"slack_post": {"token_secret": ""}},
        }
        assert problems("gete", document) != []


class TestPolicies:
    def test_declaring_slack_post_counts_as_having_write_tools(self) -> None:
        """Posting writes; the write policies must not need a tools entry to see it."""
        assert has_write_tools({**AGENT, "shared_credentials": ["slack_post"]})
        assert not has_write_tools(AGENT)

    def test_a_policy_can_key_on_shared_credentials(self) -> None:
        policy = Policy(name="shared", when="has_shared_credentials")
        assert policy_applies(policy, {**AGENT, "shared_credentials": ["slack_post"]})
        assert not policy_applies(policy, AGENT)

    def test_the_policy_schema_knows_the_condition(self) -> None:
        assert (
            problems("policy", [{"name": "shared", "when": "has_shared_credentials"}])
            == []
        )


class TestRegistry:
    def test_the_schema_and_the_registry_agree(self) -> None:
        """The schema's enum is what validate sees; the registry is what runs.

        One updated without the other would accept a declaration that builds
        nothing, or build tools no declaration can name.
        """
        from gete.schema import load_schema

        enum = load_schema("agent")["properties"]["shared_credentials"]["items"]["enum"]
        assert sorted(enum) == sorted(SHARED_CREDENTIALS)

    def test_slack_post_carries_its_shape(self) -> None:
        credential = SHARED_CREDENTIALS["slack_post"]
        assert credential.token_env == "SLACK_BOT_TOKEN"
        assert credential.has_write_tools
        assert credential.instruction

    @pytest.mark.parametrize("name", sorted(SHARED_CREDENTIALS))
    def test_every_credential_loads_its_tools(self, name: str) -> None:
        tools = SHARED_CREDENTIALS[name].load_tools()
        assert tools
        for function, effect in tools:
            assert callable(function)
            assert effect in ("read", "write")
