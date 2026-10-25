"""Official metadata observations; no capability validation or effective policy."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from pydantic.alias_generators import to_camel

from control_plane.app.modules.model_gateway.domain import (
    ConnectionRef,
    Deployment,
    ProviderKind,
    ProviderModelId,
    TokenLimit,
)
from control_plane.app.modules.model_gateway.domain.connections import (
    EXECUTION_LEASE_SECONDS,
    MAX_RESPONSE_BYTES,
    PROBE_TIMEOUT_SECONDS,
    ConnectionDefinition,
    EnvironmentName,
    Region,
    VersionLabel,
    digest,
)

METADATA_ADAPTER_VERSION = "bailian-model-metadata-v1"
METADATA_PAGE_NO: Literal[1] = 1
METADATA_PAGE_SIZE: Literal[2] = 2
METADATA_MAX_BYTES = MAX_RESPONSE_BYTES
METADATA_TIMEOUT_SECONDS = PROBE_TIMEOUT_SECONDS
METADATA_LEASE_SECONDS = EXECUTION_LEASE_SECONDS
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ServiceSite = Literal[
    "global",
    "international",
    "asia-pacific-china",
    "cn-hongkong",
    "european-union",
    "united-states",
    "japan",
]
ProviderLabel = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")
]
WorkspaceId = Annotated[
    str, StringConstraints(min_length=1, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]*$")
]


class MetadataModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, alias_generator=to_camel, populate_by_name=True
    )


class MetadataReason(StrEnum):
    SOURCE_UNCONFIGURED = "SOURCE_UNCONFIGURED"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    SOURCE_NOT_APPROVED = "SOURCE_NOT_APPROVED"
    SOURCE_SCOPE_MISMATCH = "SOURCE_SCOPE_MISMATCH"
    CONNECTION_UNAVAILABLE = "CONNECTION_UNAVAILABLE"
    CANDIDATE_ARCHIVED = "CANDIDATE_ARCHIVED"
    INPUT_CHANGED = "INPUT_CHANGED"
    ACTOR_INELIGIBLE = "ACTOR_INELIGIBLE"
    AUTHORIZATION_UNAVAILABLE = "AUTHORIZATION_UNAVAILABLE"
    MATERIAL_UNAVAILABLE = "MATERIAL_UNAVAILABLE"
    MATERIAL_VERSION_CHANGED = "MATERIAL_VERSION_CHANGED"
    CONNECTION_BUSY = "CONNECTION_BUSY"
    TARGET_UNAVAILABLE = "TARGET_UNAVAILABLE"
    TARGET_NOT_ALLOWED = "TARGET_NOT_ALLOWED"
    PROVIDER_REJECTED = "PROVIDER_REJECTED"
    PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    REDIRECT_REJECTED = "REDIRECT_REJECTED"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    INVALID_PAGE = "INVALID_PAGE"
    RESULT_NOT_UNIQUE = "RESULT_NOT_UNIQUE"
    INCOMPLETE_RESULT = "INCOMPLETE_RESULT"
    MODEL_IDENTITY_MISMATCH = "MODEL_IDENTITY_MISMATCH"
    INVALID_TOKEN_VALUE = "INVALID_TOKEN_VALUE"
    RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"
    REQUEST_OUTCOME_UNKNOWN = "REQUEST_OUTCOME_UNKNOWN"
    RESPONSE_CLOSE_FAILED = "RESPONSE_CLOSE_FAILED"
    EXECUTION_EXPIRED = "EXECUTION_EXPIRED"


class MetadataBlocked(Exception):
    def __init__(self, reason: MetadataReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


class MetadataSourceDefinition(MetadataModel):
    purpose: Literal["MODEL_METADATA_OBSERVATION"]
    source_id: VersionLabel
    version: VersionLabel
    connection_ref: ConnectionRef
    provider_kind: ProviderKind
    region: Region
    workspace_id: WorkspaceId | None
    service_site: ServiceSite
    allowed_model_ids: tuple[ProviderModelId, ...] = Field(min_length=1, max_length=100)
    inference_provider: ProviderLabel
    secret_ref: Annotated[
        str, StringConstraints(pattern=r"^secret-ref:[A-Za-z0-9][A-Za-z0-9._/-]*$", max_length=266)
    ]
    material_version: VersionLabel

    @model_validator(mode="after")
    def approved_scope(self) -> Self:
        fixed_host = self.region in {"ap-southeast-1", "cn-hongkong"}
        if (self.workspace_id is None) != fixed_host:
            raise ValueError(
                "Workspace is required only for workspace-qualified metadata endpoints"
            )
        if len(set(self.allowed_model_ids)) != len(self.allowed_model_ids):
            raise ValueError("allowed metadata models must be unique")
        return self

    @property
    def hostname(self) -> str:
        if self.region == "ap-southeast-1":
            return "dashscope-intl.aliyuncs.com"
        if self.region == "cn-hongkong":
            return "cn-hongkong.dashscope.aliyuncs.com"
        return f"{self.workspace_id}.{self.region}.maas.aliyuncs.com"


class MetadataSourceView(MetadataModel):
    source_id: VersionLabel
    version: VersionLabel = Field(
        description=(
            "Server-approved source configuration version; not a Provider specification version."
        )
    )
    fingerprint: Sha256
    region: Region
    workspace_id: WorkspaceId | None = Field(
        description="Local approved request binding, not a Provider authentication echo."
    )
    service_site: ServiceSite
    inference_provider: ProviderLabel


def metadata_connection_values(connection: ConnectionDefinition) -> dict[str, object]:
    # Inference allowlists, credentials and search options do not grant metadata permission.
    return {
        key: getattr(connection, key)
        for key in ("reference", "version", "provider_kind", "region", "workspace_id")
    }


class MetadataInput(MetadataModel):
    deployment_id: str
    deployment_revision: int = Field(strict=True, ge=1)
    provider_kind: ProviderKind
    provider_model_id: ProviderModelId
    declared_context_window: TokenLimit | None
    declared_max_output_tokens: TokenLimit | None
    connection_ref: ConnectionRef
    connection_version: VersionLabel
    connection_fingerprint: Sha256
    environment: EnvironmentName
    source: MetadataSourceView
    material_version: VersionLabel
    adapter_version: VersionLabel
    page_no: Literal[1] = METADATA_PAGE_NO
    page_size: Literal[2] = METADATA_PAGE_SIZE
    input_digest: Sha256

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        if self.input_digest != digest(self.model_dump(mode="json", exclude={"input_digest"})):
            raise ValueError("metadata input digest mismatch")
        return self

    @classmethod
    def capture(
        cls,
        candidate: Deployment,
        connection: ConnectionDefinition,
        source: MetadataSourceDefinition,
        environment: str,
    ) -> "MetadataInput":
        used_connection = metadata_connection_values(connection)
        source_fingerprint = digest(
            {
                "environment": environment,
                "source": source.model_dump(mode="json"),
                "connection": used_connection,
                "model": candidate.provider_model_id,
            }
        )
        values = {
            "deployment_id": candidate.id,
            "deployment_revision": candidate.revision,
            "provider_kind": candidate.provider_kind,
            "provider_model_id": candidate.provider_model_id,
            "declared_context_window": candidate.declared_context_window,
            "declared_max_output_tokens": candidate.declared_max_output_tokens,
            "connection_ref": connection.reference,
            "connection_version": connection.version,
            "connection_fingerprint": digest(used_connection),
            "environment": environment,
            "source": MetadataSourceView(
                source_id=source.source_id,
                version=source.version,
                fingerprint=source_fingerprint,
                region=source.region,
                workspace_id=source.workspace_id,
                service_site=source.service_site,
                inference_provider=source.inference_provider,
            ).model_dump(mode="json"),
            "material_version": source.material_version,
            "adapter_version": METADATA_ADAPTER_VERSION,
            "page_no": METADATA_PAGE_NO,
            "page_size": METADATA_PAGE_SIZE,
        }
        return cls.model_validate(values | {"input_digest": digest(values)})


class ReportedLimit(MetadataModel):
    state: Literal["VALUE", "NULL", "MISSING"]
    value: TokenLimit | None = None

    @model_validator(mode="after")
    def value_matches_state(self) -> Self:
        if (self.state == "VALUE") != (self.value is not None):
            raise ValueError("reported limit state mismatch")
        return self


class MetadataLimitComparison(MetadataModel):
    reported: ReportedLimit
    declared: TokenLimit | None
    comparison: Literal["EQUAL", "DIFFERENT", "UNDECLARED", "NOT_COMPARABLE"]

    @model_validator(mode="after")
    def comparison_matches(self) -> Self:
        expected = (
            "UNDECLARED"
            if self.declared is None
            else "NOT_COMPARABLE"
            if self.reported.state != "VALUE"
            else "EQUAL"
            if self.reported.value == self.declared
            else "DIFFERENT"
        )
        if expected != self.comparison:
            raise ValueError("limit comparison mismatch")
        return self


class MetadataReport(MetadataModel):
    model: ProviderModelId
    provider: ProviderLabel
    inference_provider: ProviderLabel
    context_window: ReportedLimit
    max_input_tokens: ReportedLimit
    max_output_tokens: ReportedLimit
    specification_version: None = None
    expires_at: None = None


class MetadataState(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    REPORTED = "REPORTED"
    NOT_FOUND = "NOT_FOUND"
    INDETERMINATE = "INDETERMINATE"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"


class MetadataApplicability(StrEnum):
    MATCHING = "MATCHING"
    STALE = "STALE"
    UNVERIFIABLE = "UNVERIFIABLE"


class MetadataOutcome(MetadataModel):
    state: MetadataState
    reason: MetadataReason | None = None
    elapsed_ms: int | None = Field(default=None, strict=True, ge=0)
    response_sha256: Sha256 | None = None
    response_bytes: int | None = Field(default=None, strict=True, ge=0, le=METADATA_MAX_BYTES)
    provider_request_id: str | None = Field(default=None, max_length=128)
    report: MetadataReport | None = None

    @model_validator(mode="after")
    def consistent_outcome(self) -> Self:
        if (self.state == MetadataState.REPORTED) != (self.report is not None):
            raise ValueError("only a reported observation contains model values")
        reason_required = self.state in {
            MetadataState.INDETERMINATE,
            MetadataState.FAILED,
            MetadataState.BLOCKED,
            MetadataState.UNKNOWN,
        }
        if reason_required != (self.reason is not None):
            raise ValueError("observation state/reason mismatch")
        if (self.response_sha256 is None) != (self.response_bytes is None):
            raise ValueError("only a complete response has a digest/byte measurement")
        if self.state in {MetadataState.REPORTED, MetadataState.NOT_FOUND} and (
            self.response_sha256 is None or not self.response_bytes
        ):
            raise ValueError("reported metadata requires a complete response measurement")
        return self


class MetadataObservation(MetadataOutcome):
    id: str
    deployment_id: str
    revision: int = Field(strict=True, ge=1)
    requested_by: str
    requested_at: datetime
    input: MetadataInput
    attempt: Literal[0, 1] = 0
    execution_token: str | None = None
    started_at: datetime | None = None
    deadline_at: datetime | None = None
    finished_at: datetime | None = None
    material_currentness: MetadataApplicability = MetadataApplicability.UNVERIFIABLE

    @model_validator(mode="after")
    def consistent_execution(self) -> Self:
        if self.deployment_id != self.input.deployment_id:
            raise ValueError("observation target mismatch")
        fence = (self.execution_token, self.started_at, self.deadline_at)
        if (self.attempt == 0 and any(x is not None for x in fence)) or (
            self.attempt == 1 and any(x is None for x in fence)
        ):
            raise ValueError("invalid observation execution fence")
        if (
            self.started_at is not None
            and self.deadline_at is not None
            and self.deadline_at <= self.started_at
        ):
            raise ValueError("invalid observation deadline")
        active = self.state in {MetadataState.QUEUED, MetadataState.RUNNING}
        if active == (self.finished_at is not None):
            raise ValueError("invalid observation completion time")
        if self.state in {MetadataState.QUEUED, MetadataState.BLOCKED} and self.attempt != 0:
            raise ValueError("unsent observation cannot have an attempt")
        if self.state not in {MetadataState.QUEUED, MetadataState.BLOCKED} and self.attempt != 1:
            raise ValueError("executed observation requires a fence")
        if self.report and (
            self.report.model != self.input.provider_model_id
            or self.report.inference_provider != self.input.source.inference_provider
        ):
            raise ValueError("report identity mismatch")
        return self


class MetadataAdmission(MetadataModel):
    available: bool
    reason: MetadataReason | None
    source: MetadataSourceView | None

    @model_validator(mode="after")
    def consistent_admission(self) -> Self:
        if self.available:
            if self.reason is not None or self.source is None:
                raise ValueError("approved metadata source required")
        elif self.reason is None or self.source is not None:
            raise ValueError("unavailable admission requires a reason only")
        return self


def compare_limit(reported: ReportedLimit, declared: int | None) -> MetadataLimitComparison:
    return MetadataLimitComparison(
        reported=reported,
        declared=declared,
        comparison="UNDECLARED"
        if declared is None
        else "NOT_COMPARABLE"
        if reported.state != "VALUE"
        else "EQUAL"
        if reported.value == declared
        else "DIFFERENT",
    )
