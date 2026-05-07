from __future__ import annotations

from decimal import Decimal


class FXError(Exception):
    """Base class for all FX engine errors."""
    status_code: int = 500
    error_code: str = "internal_error"


class QuoteNotFoundError(FXError):
    status_code = 404
    error_code = "quote_not_found"

    def __init__(self, quote_id: str) -> None:
        super().__init__(f"Quote {quote_id} not found")


class QuoteExpiredError(FXError):
    status_code = 409
    error_code = "quote_expired"

    def __init__(self, quote_id: str) -> None:
        super().__init__(f"Quote {quote_id} has expired")


class QuoteAlreadyExecutedError(FXError):
    status_code = 409
    error_code = "quote_already_executed"

    def __init__(self, quote_id: str) -> None:
        super().__init__(f"Quote {quote_id} has already been executed")


class InsufficientBalanceError(FXError):
    status_code = 422
    error_code = "insufficient_balance"

    def __init__(self, currency: str, required: Decimal, available: Decimal) -> None:
        super().__init__(
            f"Insufficient {currency} balance: required {required}, available {available}"
        )
        self.currency = currency
        self.required = required
        self.available = available


class CustomerNotFoundError(FXError):
    status_code = 404
    error_code = "customer_not_found"

    def __init__(self, customer_id: str) -> None:
        super().__init__(f"Customer {customer_id} not found")


class RatesUnavailableError(FXError):
    status_code = 503
    error_code = "rates_unavailable"

    def __init__(self, reason: str = "Exchange rates are stale or unavailable") -> None:
        super().__init__(reason)


class UnsupportedCurrencyPairError(FXError):
    status_code = 400
    error_code = "unsupported_currency_pair"

    def __init__(self, from_ccy: str, to_ccy: str) -> None:
        super().__init__(f"No rate available for {from_ccy}/{to_ccy}")


class InvalidAmountError(FXError):
    status_code = 400
    error_code = "invalid_amount"
