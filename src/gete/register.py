"""Registering agents with Gemini Enterprise.

Terraform has no resources for Gemini Enterprise authorizations or agent
registrations, so gete does this part: create or update one authorization per
connection, then bring the registration in line with the declaration.

A registration is never recreated. Recreating changes its id, and every
user's link and session to the agent breaks. Creating one in the first place
needs a Gemini Enterprise license, which CD does not hold; gete writes the
steps for a person instead and carries on with the next agent.
"""

import base64
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from gete.connection import Connection, Registry, authorization_id
from gete.declaration import Agent, Project
from gete.errors import DeclarationError, GeteError
from gete.gcp import GcpApi, GcpError
from gete.templates import template_text

DISCOVERY = "https://discoveryengine.googleapis.com/v1alpha"
RESOURCE_MANAGER = "https://cloudresourcemanager.googleapis.com/v1"
SECRET_MANAGER = "https://secretmanager.googleapis.com/v1"
NOTICE_FILE = "registration-notice.md"

# Where Gemini Enterprise sends the user back after consent. Fixed on its
# side; a declaration cannot change it.
REDIRECT_URI = "https://vertexaisearch.cloud.google.com/oauth-redirect"

# Gemini Enterprise answers with these when a person has to act.
LICENSE_REQUIRED = "license is not available"
AUTHORIZATION_IN_USE = "is used by another agent"


def needs_license(message: str) -> bool:
    return LICENSE_REQUIRED in message


def in_use_by_another(message: str) -> bool:
    return AUTHORIZATION_IN_USE in message


def authorization_uri(connection: Connection, client_id: str) -> str:
    """Where the user is sent to consent, with the scopes the connection declares."""
    oauth = connection.oauth
    if oauth.authorization_query is not None:
        # The form known to work for this service; Gemini Enterprise adds
        # client_id and redirect_uri itself.
        params: dict[str, str] = dict(oauth.authorization_query)
    else:
        params = {
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            oauth.scope_parameter: " ".join(oauth.scopes),
            # Without offline access there is no refresh token and reading
            # stops an hour after consent.
            "access_type": "offline",
            "prompt": "consent",
        }
    return f"{oauth.authorization_url}?{urlencode(params)}"


def authorization_body(
    parent: str,
    agent_name: str,
    connection: Connection,
    client_id: str,
    client_secret: str,
) -> dict[str, Any]:
    """The Authorization resource, named per agent so two agents never share one."""
    name = f"{parent}/authorizations/{authorization_id(agent_name, connection.id)}"
    return {
        "name": name,
        "serverSideOauth2": {
            "clientId": client_id,
            "clientSecret": client_secret,
            "authorizationUri": authorization_uri(connection, client_id),
            "tokenUri": connection.oauth.token_url,
        },
    }


def agent_body(
    declaration: Mapping[str, Any], reasoning_engine: str, authorizations: list[str]
) -> dict[str, Any]:
    """The Agent resource. The description is what Gemini Enterprise routes on."""
    body: dict[str, Any] = {
        "displayName": declaration["display_name"],
        "description": declaration["description"],
        "adkAgentDefinition": {
            "provisionedReasoningEngine": {"reasoningEngine": reasoning_engine}
        },
    }
    if authorizations:
        body["authorizationConfig"] = {"toolAuthorizations": authorizations}
    if declaration.get("starter_prompts"):
        body["starterPrompts"] = [
            {"text": text} for text in declaration["starter_prompts"]
        ]
    return body


def find_engine(engines: list[dict[str, Any]], agent_name: str) -> dict[str, Any]:
    """The reasoning engine labelled gete-agent=<name>. Exactly one, or an error."""
    matched = [
        e for e in engines if (e.get("labels") or {}).get("gete-agent") == agent_name
    ]
    if not matched:
        raise GcpError(
            0,
            f"no reasoning engine labelled gete-agent={agent_name}; "
            "has apply finished?",
        )
    if len(matched) > 1:
        names = ", ".join(str(e.get("name", "?")).rsplit("/", 1)[-1] for e in matched)
        raise GcpError(
            0,
            f"{len(matched)} reasoning engines labelled gete-agent={agent_name}: "
            f"{names}",
        )
    return matched[0]


def find_by_reasoning_engine(
    agents: list[dict[str, Any]], reasoning_engine: str
) -> list[dict[str, Any]]:
    """Registrations pointing at the engine. Display names are not compared.

    A person registering in the console chooses the name, and the console
    needs an ASCII one; it rarely equals the declaration's.
    """
    found = []
    for agent in agents:
        definition = agent.get("adkAgentDefinition") or {}
        engine = (definition.get("provisionedReasoningEngine") or {}).get(
            "reasoningEngine"
        )
        if engine == reasoning_engine:
            found.append(agent)
    return found


def registration_matches(agent: Mapping[str, Any], authorizations: list[str]) -> bool:
    """Same set of authorizations, in any order."""
    config = agent.get("authorizationConfig") or {}
    return set(config.get("toolAuthorizations") or []) == set(authorizations)


@dataclass
class Summary:
    registered: list[str] = field(default_factory=list)
    needs_human: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)

    def say(self, text: str) -> None:
        self.messages.append(text)


class Registrar:
    """Register every agent of a project. One agent's failure does not stop the rest."""

    def __init__(self, project: Project, gcp: GcpApi, notice: Path) -> None:
        self._project = project
        self._gcp = gcp
        self._notice = notice
        self._registry = Registry.from_catalog(project.connection_overrides)
        self._gcp_project = str(project.data["project"])
        self._location = str(project.data["location"])
        ge: Mapping[str, Any] = project.data.get("gemini_enterprise", {})
        self._ge_location = str(ge.get("location", "global"))
        self._number: str | None = ge.get("project_number")

    def run(self, names: list[str] | None = None) -> Summary:
        summary = Summary()
        if names:
            declared = {agent.name for agent in self._project.agents}
            unknown = sorted(set(names) - declared)
            if unknown:
                raise DeclarationError(
                    f"no agent named {', '.join(unknown)}; "
                    f"declared: {', '.join(sorted(declared))}"
                )
        for agent in self._project.agents:
            if names and agent.name not in names:
                continue
            engine = _engine_id(agent)
            if engine is None:
                summary.say(
                    f"{agent.name}: no registration.gemini_enterprise.engine; skipped"
                )
                summary.skipped.append(agent.name)
                continue
            try:
                self._register(agent, engine, summary)
            except GeteError as error:
                summary.say(f"{agent.name}: cannot register: {error}")
                summary.failed.append(agent.name)
        return summary

    @property
    def _parent(self) -> str:
        if self._number is None:
            project = self._gcp.get(f"{RESOURCE_MANAGER}/projects/{self._gcp_project}")
            self._number = str(project["projectNumber"])
        return f"projects/{self._number}/locations/{self._ge_location}"

    def _register(self, agent: Agent, engine: str, summary: Summary) -> None:
        engines_url = (
            f"https://{self._location}-aiplatform.googleapis.com/v1/projects/"
            f"{self._gcp_project}/locations/{self._location}/reasoningEngines"
        )
        reasoning_engine = str(
            find_engine(
                self._gcp.list_all(engines_url, "reasoningEngines"), agent.name
            )["name"]
        )
        summary.say(
            f"{agent.name}: reasoning engine {reasoning_engine.rsplit('/', 1)[-1]}"
        )
        authorizations = [
            self._upsert_authorization(
                agent, self._registry.get(connection_id), summary
            )
            for connection_id in agent.connections
        ]
        agents_url = (
            f"{DISCOVERY}/{self._parent}/collections/default_collection/engines/{engine}"
            "/assistants/default_assistant/agents"
        )
        registered = find_by_reasoning_engine(
            self._gcp.list_all(agents_url, "agents"), reasoning_engine
        )
        if not registered:
            summary.say(f"{agent.name}: not registered yet; writing the steps")
            self._write_notice(agent, engine, reasoning_engine, authorizations, "new")
            summary.needs_human.append(agent.name)
            return
        if len(registered) > 1:
            summary.say(
                f"{agent.name}: {len(registered)} registrations point at the engine"
            )
            self._write_notice(
                agent,
                engine,
                reasoning_engine,
                authorizations,
                "duplicated",
                registered,
            )
            summary.needs_human.append(agent.name)
            return
        found = registered[0]
        if registration_matches(found, authorizations):
            summary.say(f"{agent.name}: registration is current")
            summary.registered.append(agent.name)
            return
        try:
            self._gcp.patch(
                f"{DISCOVERY}/{found['name']}",
                agent_body(agent.data, reasoning_engine, authorizations),
                params={"updateMask": "authorizationConfig"},
            )
        except GcpError as error:
            if in_use_by_another(error.message):
                summary.say(f"{agent.name}: an authorization is held by another agent")
                self._write_notice(
                    agent, engine, reasoning_engine, authorizations, "in_use"
                )
                summary.needs_human.append(agent.name)
                return
            if needs_license(error.message):
                summary.say(
                    f"{agent.name}: updating needs a license too; writing the steps"
                )
                self._write_notice(
                    agent, engine, reasoning_engine, authorizations, "stale"
                )
                summary.needs_human.append(agent.name)
                return
            raise
        summary.say(f"{agent.name}: registration updated")
        summary.registered.append(agent.name)

    def _upsert_authorization(
        self, agent: Agent, connection: Connection, summary: Summary
    ) -> str:
        body = authorization_body(
            self._parent,
            agent.name,
            connection,
            self._secret(connection.client_id_secret),
            self._secret(connection.client_secret_secret),
        )
        resource = str(body["name"])
        identifier = resource.rsplit("/", 1)[-1]
        existing = self._gcp.list_all(
            f"{DISCOVERY}/{self._parent}/authorizations", "authorizations"
        )
        if any(row.get("name") == resource for row in existing):
            self._gcp.patch(
                f"{DISCOVERY}/{resource}",
                body,
                params={"updateMask": "serverSideOauth2"},
            )
            summary.say(f"{agent.name}: authorization updated: {identifier}")
        else:
            self._gcp.post(
                f"{DISCOVERY}/{self._parent}/authorizations",
                body,
                params={"authorizationId": identifier},
            )
            summary.say(f"{agent.name}: authorization created: {identifier}")
        return resource

    def _secret(self, name: str) -> str:
        url = (
            f"{SECRET_MANAGER}/projects/{self._gcp_project}/secrets/{name}"
            "/versions/latest:access"
        )
        try:
            payload = self._gcp.get(url)
        except GcpError as error:
            raise GcpError(
                error.status,
                f"cannot read secret {name}; put the OAuth client there: "
                f"{error.message}",
            ) from error
        return base64.b64decode(payload["payload"]["data"]).decode().strip()

    def _write_notice(
        self,
        agent: Agent,
        engine: str,
        reasoning_engine: str,
        authorizations: list[str],
        case: str,
        duplicated: list[dict[str, Any]] | None = None,
    ) -> None:
        """Append the steps a person has to take; one release may add several agents."""
        display_name = str(agent.data["display_name"])
        ids = ", ".join(f"`{a.rsplit('/', 1)[-1]}`" for a in authorizations)
        if case == "in_use":
            title = f"🔗 The authorizations of {display_name} are held elsewhere"
            lead = (
                f"Binding {ids} was refused because another agent uses them. "
                "An authorization serves one agent, and **deleting an agent can "
                "leave the binding behind.** Delete the authorization; the next "
                "release recreates and binds it. CD does not delete it, because it "
                "cannot tell this apart from an agent that really uses it."
            )
        elif case == "duplicated":
            names = ", ".join(
                f"`{a.get('displayName', '?')}`" for a in duplicated or []
            )
            title = f"⚠️ {display_name} is registered more than once"
            lead = (
                f"{len(duplicated or [])} registrations point at the same reasoning "
                f"engine ({names}). CD cannot decide which to keep and touches none "
                "of them. Delete the extra ones; the next release brings the "
                "remaining one in line."
            )
        elif case == "stale":
            title = f"♻️ Update the registration of {display_name}"
            lead = (
                "The registration differs from the declaration. CD tried to update "
                "it and was refused for lack of a license. Until fixed, the old "
                "authorizations apply."
            )
        else:
            title = f"🆕 Register {display_name} with Gemini Enterprise"
            lead = (
                "This release added the agent. Nobody can call it until it is "
                "registered."
            )
        template = self._template()
        text = _fill(
            template,
            title=title,
            lead=lead,
            console_url=(
                "https://console.cloud.google.com/gemini-enterprise/locations/"
                f"{self._ge_location}/engines/{engine}"
                f"/agentic/agents?project={self._gcp_project}"
            ),
            display_name=display_name,
            name=agent.name,
            description=str(agent.data["description"]),
            reasoning_engine=reasoning_engine,
            project=self._gcp_project,
            authorization_config_json=json.dumps(
                {"authorizationConfig": {"toolAuthorizations": authorizations}}
            ),
        )
        if self._notice.exists():
            text = self._notice.read_text(encoding="utf-8") + "\n---\n\n" + text
        self._notice.parent.mkdir(parents=True, exist_ok=True)
        self._notice.write_text(text, encoding="utf-8")

    def _template(self) -> str:
        registration: Mapping[str, Any] = self._project.data.get("registration", {})
        custom = registration.get("notice_template")
        if custom:
            path = self._project.root / str(custom)
            if not path.is_file():
                raise DeclarationError(
                    f"registration.notice_template {path} does not exist"
                )
            return path.read_text(encoding="utf-8")
        return template_text("notice.md")


def _fill(template: str, **values: str) -> str:
    """Fill the notice template, naming a placeholder gete does not know."""
    try:
        return template.format(**values)
    except (KeyError, IndexError) as error:
        raise DeclarationError(
            f"the notice template uses {error}, which gete does not fill; "
            f"available: {', '.join(sorted(values))}"
        ) from None


def _engine_id(agent: Agent) -> str | None:
    registration: Mapping[str, Any] = agent.data.get("registration", {})
    engine = registration.get("gemini_enterprise", {}).get("engine")
    return str(engine) if engine else None


def register_project(
    project: Project, gcp: GcpApi, notice: Path, names: list[str] | None = None
) -> Summary:
    """Register every agent (or the named ones) and return what happened."""
    return Registrar(project, gcp, notice).run(names)
