"""Small explicit contracts for local frozen mail, without a transport framework."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from pydantic import Field

from signalnest.contracts import Contract, NoticeContent
from signalnest.notifications.contracts import Decision

RENDERING_VERSION = "signalnest-text-mail-v1"


class MailError(RuntimeError):
    def __init__(self, code: str, *, event_id: int | None = None, mail_id: int | None = None):
        self.code = code
        self.event_id = event_id
        self.mail_id = mail_id
        super().__init__(f"{code} (event_id={event_id}, mail_id={mail_id})")


class PlanOptions(Contract):
    max_messages: int = Field(default=5, ge=1, le=20, strict=True)
    max_events: int = Field(default=50, ge=1, le=100, strict=True)
    max_bytes: int = Field(default=128 * 1024, ge=1024, le=1024 * 1024, strict=True)


@dataclass(frozen=True)
class MailItem:
    event_id: int
    decision_id: int
    document_id: int
    source_document_id: str
    kind: Literal["new", "update", "activation_recent", "opportunity_review"]
    occurred_at: int
    content: NoticeContent = field(repr=False)
    page_url: str
    decision: Decision = field(repr=False)


@dataclass(frozen=True)
class RenderedMail:
    sender: str
    recipient: str
    subject: str
    message_id: str
    date_at: int
    rendering_version: str
    body_text: str = field(repr=False)
    payload: bytes = field(repr=False)
    payload_sha256: str


@dataclass(frozen=True)
class FrozenMessage:
    mail_id: int
    kind: Literal["immediate", "digest"]
    rendered: RenderedMail = field(repr=False)
    members_json: str = field(repr=False)


class SendOutcome(StrEnum):
    ACCEPTED = "accepted"
    RETRYABLE = "retryable"
    UNCERTAIN = "uncertain"
    PERMANENT = "permanent"


class SendStage(StrEnum):
    CONFIG = "config"
    CONNECT = "connect"
    HELLO = "hello"
    TLS = "tls"
    AUTH = "auth"
    MAIL = "mail"
    RCPT = "rcpt"
    BODY_OR_FINAL = "body_or_final"
    ACCEPTED = "accepted"


class SendErrorCode(StrEnum):
    INVALID_FROZEN_MAIL = "invalid_frozen_mail"
    CREDENTIALS_MISSING = "credentials_missing"
    CREDENTIALS_INVALID = "credentials_invalid"
    TLS_VERIFICATION_FAILED = "tls_verification_failed"
    TLS_NOT_SUPPORTED = "tls_not_supported"
    TLS_FAILED = "tls_failed"
    AUTH_NOT_SUPPORTED = "auth_not_supported"
    AUTH_MECHANISM_NOT_SUPPORTED = "auth_mechanism_not_supported"
    AUTHENTICATION_REJECTED = "authentication_rejected"
    SERVER_REJECTED = "server_rejected"
    CONNECTION_FAILED = "connection_failed"
    TIMEOUT = "timeout"
    DISCONNECTED = "disconnected"
    PROTOCOL_ERROR = "protocol_error"


@dataclass(frozen=True)
class SendResult:
    """An observation of one SMTP transaction, not inbox delivery or a retry policy."""

    outcome: SendOutcome
    stage: SendStage
    error_code: SendErrorCode | None = None
    smtp_code: int | None = None
    scope: Literal["message", "channel"] = "message"
    cleanup_failed: bool = False

    def __post_init__(self):
        if (
            not isinstance(self.outcome, SendOutcome)
            or not isinstance(self.stage, SendStage)
            or (self.error_code is not None and not isinstance(self.error_code, SendErrorCode))
            or self.scope not in {"message", "channel"}
            or type(self.cleanup_failed) is not bool
            or (
                self.smtp_code is not None
                and (type(self.smtp_code) is not int or not 100 <= self.smtp_code <= 599)
            )
        ):
            raise ValueError("invalid finite SMTP result")
        if self.outcome == SendOutcome.ACCEPTED:
            if (
                self.stage != SendStage.ACCEPTED
                or self.smtp_code != 250
                or self.error_code is not None
            ):
                raise ValueError("SMTP acceptance requires the final 250")
        elif self.error_code is None or self.stage == SendStage.ACCEPTED:
            raise ValueError("SMTP failure requires a finite error and failure stage")
        if self.outcome == SendOutcome.UNCERTAIN and self.stage != SendStage.BODY_OR_FINAL:
            raise ValueError("uncertain is reserved for the DATA boundary")
