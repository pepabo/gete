"""Exceptions shared across the package."""


class GeteError(Exception):
    """Base class for errors that carry a message meant for the person running gete."""


class UserFacingError(GeteError):
    """An error whose message is written to be shown to the model and the user.

    Raising it is a declaration by the raiser that the text is safe to show:
    no response bodies, no credentials, no unsanitized URLs. Every other
    exception reaches the model as its type name and nothing else.
    """


class DeclarationError(GeteError):
    """A declaration does not have the expected shape or breaks a rule."""


class UnknownConnection(GeteError):
    """An agent names a connection that neither the catalog nor gete.yaml defines."""


class RetiredConnection(GeteError):
    """An agent names a connection that has been retired, with the reason attached."""
