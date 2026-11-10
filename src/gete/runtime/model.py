"""The model an agent declares, as ADK's LlmAgent takes it."""

from collections.abc import Mapping
from typing import Any

from gete.errors import DeclarationError


def gemini_class(model_id: str) -> type[Any] | None:
    """ADK's class for the model when it is a Gemini, or None.

    The class comes from ADK's own registry, the one LlmAgent resolves a
    plain name through, so a model ADK serves with another class - or does
    not know - is not mistaken for a Gemini.
    """
    from google.adk.models import Gemini
    from google.adk.models.registry import LLMRegistry

    try:
        resolved = LLMRegistry.resolve(model_id)
    except ValueError:
        return None
    return resolved if issubclass(resolved, Gemini) else None


def build_model(declared: str | Mapping[str, str]) -> Any:
    """A plain name as it is, or a Gemini bound to the declared location.

    A plain name is called in the region the agent runs in, which Agent
    Engine sets in GOOGLE_CLOUD_LOCATION. A full resource path naming another
    location does not help - the request still goes to the regional endpoint -
    so the location has to reach the client itself.
    """
    if isinstance(declared, str):
        return declared
    model_id = declared["id"]
    cls = gemini_class(model_id)
    if cls is None:
        raise DeclarationError(
            f"model.id: {model_id!r} is not a Gemini model; "
            "only a Gemini model's client can be pointed at a location"
        )
    # client_kwargs rather than a subclass overriding api_client: the field is
    # what ADK reads when it builds both its generate and its live clients,
    # so one setting covers both.
    return cls(
        model=model_id,
        client_kwargs={"enterprise": True, "location": declared["location"]},
    )
