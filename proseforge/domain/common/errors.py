class DomainError(Exception):
    code = "DOMAIN_ERROR"
    retryable = False


class NotFoundError(DomainError):
    code = "NOT_FOUND"


class ConflictError(DomainError):
    code = "CONFLICT"


class ValidationError(DomainError):
    code = "VALIDATION_ERROR"


class ProviderError(DomainError):
    code = "PROVIDER_ERROR"


class RetryableProviderError(ProviderError):
    code = "PROVIDER_RETRYABLE"
    retryable = True
