"""Model-provider failures. These are not shop-rule errors."""


class ConfigError(Exception):
    """The model provider is not configured."""


class LLMError(Exception):
    """The model response could not be used."""
