"""Exceptions shared across the package."""


class GeteError(Exception):
    """Base class for errors that carry a message meant for the person running gete."""


class DeclarationError(GeteError):
    """A declaration does not have the expected shape or breaks a rule."""


class UnknownConnection(GeteError):
    """An agent names a connection that neither the catalog nor gete.yaml defines."""


class RetiredConnection(GeteError):
    """An agent names a connection that has been retired, with the reason attached."""
