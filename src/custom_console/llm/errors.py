"""Errors shared by the model providers."""


class ProviderUnavailableError(RuntimeError):
    """A model provider cannot be reached, or refused the request (e.g. a wrong API key)."""
