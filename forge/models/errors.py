"""Forge-level errors for the model layer.

Every provider translates its own SDK exceptions into these. Code outside
`forge.models.providers` only ever needs to catch `ModelError` (or one of
its subclasses) and never needs to know which SDK raised the problem.
"""


class ModelError(Exception):
    """Base class for all model-layer errors."""


class ProviderNotFoundError(ModelError):
    """No provider is registered under the requested name."""


class ProviderConfigError(ModelError):
    """The provider is missing a setting it needs, such as a model name."""


class AuthenticationError(ModelError):
    """The API key is missing or was rejected by the provider."""


class ModelRequestError(ModelError):
    """The request failed: network problem, timeout, rate limit, server error, ..."""


class InvalidModelResponseError(ModelError):
    """The provider replied, but the reply could not be understood."""
