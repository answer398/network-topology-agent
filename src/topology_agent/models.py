"""Strongly typed data contracts shared by the topology workflow."""

from __future__ import annotations

from enum import StrEnum
from ipaddress import IPv4Address, IPv4Interface, IPv4Network
from pathlib import Path
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictInt,
    StringConstraints,
    field_validator,
    model_validator,
)


def _to_camel(value: str) -> str:
    first, *rest = value.split("_")
    return first + "".join(part.capitalize() for part in rest)


class _TopologyModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="forbid",
        loc_by_alias=True,
    )


NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
StrictString = Annotated[str, StringConstraints(strict=True)]
StrictNonEmptyString = Annotated[str, StringConstraints(strict=True, min_length=1)]
Confidence = Annotated[float, Field(ge=0.0, le=1.0)]
NonNegativeCoordinate = Annotated[float, Field(ge=0.0)]
PositiveDimension = Annotated[float, Field(gt=0.0)]
PrefixLength = Annotated[int, Field(ge=0, le=32)]
IPv4Candidate = IPv4Address | IPv4Interface

NodeTempId = Annotated[str, StringConstraints(pattern=r"^node_.+")]
InterfaceTempId = Annotated[str, StringConstraints(pattern=r"^if_.+")]
LinkTempId = Annotated[str, StringConstraints(pattern=r"^link_.+")]
RegionTempId = Annotated[str, StringConstraints(pattern=r"^region_.+")]
SegmentTempId = Annotated[str, StringConstraints(pattern=r"^segment_.+")]
EvidenceId = Annotated[str, StringConstraints(pattern=r"^evidence_.+")]
UnresolvedTempId = Annotated[str, StringConstraints(pattern=r"^unresolved_.+")]

PlatformNodeId = Annotated[str, StringConstraints(pattern=r"^[VWT].+")]
PlatformNicId = Annotated[str, StringConstraints(pattern=r"^P.+")]
PlatformLinkId = Annotated[str, StringConstraints(pattern=r"^L.+")]
PlatformNetworkId = Annotated[str, StringConstraints(pattern=r"^G.+")]
PlatformSubnetId = Annotated[str, StringConstraints(pattern=r"^S.+")]


class TopologyAgentError(Exception):
    """Base exception for failures in the topology workflow."""


class InputError(TopologyAgentError):
    """Raised when task input cannot be accepted."""


class ConfigurationError(TopologyAgentError):
    """Raised when application configuration is missing or invalid."""


class ModelInvocationError(TopologyAgentError):
    """Raised when a model invocation fails."""


class TopologyUnresolvedError(TopologyAgentError):
    """Raised when blocking topology ambiguity remains."""


class PlatformResourceError(TopologyAgentError):
    """Raised when a required platform resource cannot be bound."""


class PayloadValidationError(TopologyAgentError):
    """Raised when a platform payload fails validation."""


class PlatformSubmissionError(TopologyAgentError):
    """Raised when the platform explicitly rejects a submission."""


class SubmissionUncertainError(TopologyAgentError):
    """Raised when a write request has an unknown outcome."""


class SemanticDeviceType(StrEnum):
    SWITCH_L2 = "switch_l2"
    SWITCH_L3 = "switch_l3"
    ROUTER = "router"
    PUBLIC_ROUTER = "public_router"
    FIREWALL = "firewall"
    IDS = "ids"
    WAF = "waf"
    DES = "des"
    CLIENT = "client"
    SERVER = "server"
    WEB_SERVER = "web_server"
    DATABASE_SERVER = "database_server"
    MAIL_SERVER = "mail_server"
    MONITOR_SERVER = "monitor_server"
    VPC_SERVER = "vpc_server"
    UNKNOWN = "unknown"


class EvidenceSourceType(StrEnum):
    VISUAL_TEXT = "VISUAL_TEXT"
    VISUAL_ICON = "VISUAL_ICON"
    VISUAL_LINE = "VISUAL_LINE"
    VISUAL_REGION = "VISUAL_REGION"
    NETWORK_DERIVATION = "NETWORK_DERIVATION"
    MODEL_INFERENCE = "MODEL_INFERENCE"


class UnresolvedCategory(StrEnum):
    AMBIGUOUS_NODE_NAME = "AMBIGUOUS_NODE_NAME"
    AMBIGUOUS_DEVICE_TYPE = "AMBIGUOUS_DEVICE_TYPE"
    AMBIGUOUS_INTERFACE_NAME = "AMBIGUOUS_INTERFACE_NAME"
    AMBIGUOUS_IP = "AMBIGUOUS_IP"
    UNKNOWN_PREFIX = "UNKNOWN_PREFIX"
    AMBIGUOUS_LINK_ENDPOINT = "AMBIGUOUS_LINK_ENDPOINT"
    CROSSING_UNCERTAIN = "CROSSING_UNCERTAIN"
    MULTIPLE_GATEWAY_CANDIDATES = "MULTIPLE_GATEWAY_CANDIDATES"
    UNSUPPORTED_TOPOLOGY_PATTERN = "UNSUPPORTED_TOPOLOGY_PATTERN"
    NODE_DUPLICATION_AMBIGUITY = "NODE_DUPLICATION_AMBIGUITY"
    NODE_TYPE_CONFLICT = "NODE_TYPE_CONFLICT"
    NODE_NAME_CONFLICT = "NODE_NAME_CONFLICT"
    TEXT_NODE_BINDING_AMBIGUITY = "TEXT_NODE_BINDING_AMBIGUITY"
    INTERFACE_NODE_BINDING_AMBIGUITY = "INTERFACE_NODE_BINDING_AMBIGUITY"
    IP_INTERFACE_BINDING_AMBIGUITY = "IP_INTERFACE_BINDING_AMBIGUITY"
    LINK_ENDPOINT_AMBIGUITY = "LINK_ENDPOINT_AMBIGUITY"
    REGION_MEMBERSHIP_AMBIGUITY = "REGION_MEMBERSHIP_AMBIGUITY"
    CROSSING_OR_CONNECTION_AMBIGUITY = "CROSSING_OR_CONNECTION_AMBIGUITY"
    EVIDENCE_CONFLICT = "EVIDENCE_CONFLICT"
    INTERFACE_BINDING_AMBIGUITY = "INTERFACE_BINDING_AMBIGUITY"
    INTERFACE_IP_MISSING = "INTERFACE_IP_MISSING"
    INTERFACE_NAME_MISSING = "INTERFACE_NAME_MISSING"
    LINK_GEOMETRY_INCONSISTENT = "LINK_GEOMETRY_INCONSISTENT"
    TEXT_COVERAGE_INCOMPLETE = "TEXT_COVERAGE_INCOMPLETE"


class PlatformNodeType(StrEnum):
    VM = "VM"
    SW = "SW"
    TSW = "TSW"


class PlatformDevType(StrEnum):
    SERVER = "SERVER"
    CLIENT = "CLIENT"
    DRT = "DRT"
    FW = "FW"
    IDS = "IDS"
    WAF = "WAF"
    PRT = "PRT"
    DES = "DES"
    SW = "SW"
    TSW = "TSW"


class ValidationSeverity(StrEnum):
    ERROR = "ERROR"
    WARNING = "WARNING"


class TaskStatus(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    SUBMISSION_UNCERTAIN = "SUBMISSION_UNCERTAIN"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"


class Point(_TopologyModel):
    x: NonNegativeCoordinate
    y: NonNegativeCoordinate

    @field_validator("x", "y", mode="before")
    @classmethod
    def reject_boolean_coordinates(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("coordinate must be a finite number")
        return value


class BoundingBox(_TopologyModel):
    x: NonNegativeCoordinate
    y: NonNegativeCoordinate
    width: PositiveDimension
    height: PositiveDimension

    @field_validator("x", "y", "width", "height", mode="before")
    @classmethod
    def reject_boolean_dimensions(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("bounding-box values must be finite numbers")
        return value


class ImageInfo(_TopologyModel):
    width: Annotated[int, Field(gt=0)]
    height: Annotated[int, Field(gt=0)]
    format: NonEmptyString
    view_ids: list[NonEmptyString] = Field(default_factory=list)


class TaskInput(_TopologyModel):
    image_path: Path
    project_id: NonEmptyString
    network_id: NonEmptyString
    task_id: NonEmptyString

    @field_validator("image_path", mode="before")
    @classmethod
    def validate_image_path(cls, value: object) -> object:
        if not isinstance(value, (str, Path)) or not str(value).strip():
            raise ValueError("must be a non-empty image path")
        if Path(value).suffix.lower() not in {".png", ".jpg", ".jpeg"}:
            raise ValueError("must use a .png, .jpg, or .jpeg extension")
        return value


class Evidence(_TopologyModel):
    evidence_id: EvidenceId
    source_type: EvidenceSourceType
    source_view_id: NonEmptyString | None = None
    bbox: BoundingBox | None = None
    raw_text: str | None = None
    description: NonEmptyString
    confidence: Confidence


class UnresolvedItem(_TopologyModel):
    temp_id: UnresolvedTempId
    category: UnresolvedCategory
    object_ids: list[NonEmptyString] = Field(default_factory=list)
    field: NonEmptyString | None = None
    candidates: list[str] = Field(default_factory=list)
    reason: NonEmptyString
    blocking: bool
    recommended_action: NonEmptyString
    evidence_ids: list[EvidenceId] = Field(default_factory=list)


class ObservedInterface(_TopologyModel):
    observation_id: NonEmptyString
    node_candidates: list[NonEmptyString] = Field(default_factory=list)
    raw_name: str | None = None
    name_candidates: list[NonEmptyString] = Field(default_factory=list)
    raw_ip_text: str | None = None
    ip_candidates: list[IPv4Candidate] = Field(default_factory=list)
    label_bbox: BoundingBox | None = Field(default=None, alias="labelBBox")
    ip_bbox: BoundingBox | None = Field(default=None, alias="ipBBox")
    nearby_link_ids: list[NonEmptyString] = Field(default_factory=list)
    confidence: Confidence
    evidence_ids: list[EvidenceId] = Field(default_factory=list)


class ObservedNode(_TopologyModel):
    observation_id: NonEmptyString
    raw_name: str | None = None
    name_candidates: list[NonEmptyString] = Field(default_factory=list)
    semantic_type: SemanticDeviceType
    type_candidates: list[SemanticDeviceType] = Field(default_factory=list)
    vendor_model: str | None = None
    bbox: BoundingBox
    center: Point
    observed_interfaces: list[ObservedInterface] = Field(default_factory=list)
    region_candidates: list[NonEmptyString] = Field(default_factory=list)
    confidence: Confidence
    evidence_ids: list[EvidenceId] = Field(default_factory=list)
    source_view_ids: list[NonEmptyString] = Field(default_factory=list)


class ObservedLink(_TopologyModel):
    observation_id: NonEmptyString
    source_node_candidates: list[NonEmptyString] = Field(default_factory=list)
    target_node_candidates: list[NonEmptyString] = Field(default_factory=list)
    source_interface_candidates: list[NonEmptyString] = Field(default_factory=list)
    target_interface_candidates: list[NonEmptyString] = Field(default_factory=list)
    polyline: list[Point] = Field(default_factory=list)
    crossing_uncertain: bool
    confidence: Confidence
    evidence_ids: list[EvidenceId] = Field(default_factory=list)


class ObservedRegion(_TopologyModel):
    observation_id: NonEmptyString
    raw_name: str | None = None
    name_candidates: list[NonEmptyString] = Field(default_factory=list)
    bbox: BoundingBox
    fill_color: str | None = None
    member_node_candidates: list[NonEmptyString] = Field(default_factory=list)
    confidence: Confidence
    evidence_ids: list[EvidenceId] = Field(default_factory=list)


class TopologyObservation(_TopologyModel):
    task_id: NonEmptyString
    image: ImageInfo
    observed_nodes: list[ObservedNode]
    observed_interfaces: list[ObservedInterface] = Field(default_factory=list)
    observed_links: list[ObservedLink]
    observed_regions: list[ObservedRegion]
    evidence: list[Evidence]
    unresolved_items: list[UnresolvedItem]
    summary: dict[str, JsonValue]


class NodeIR(_TopologyModel):
    temp_id: NodeTempId
    raw_name: NonEmptyString
    normalized_name: NonEmptyString
    semantic_type: SemanticDeviceType
    vendor_model: str | None = None
    interface_ids: list[InterfaceTempId] = Field(default_factory=list)
    region_id: RegionTempId | None = None
    bbox: BoundingBox
    center: Point
    resource_role: NonEmptyString
    image_keywords: list[NonEmptyString] = Field(default_factory=list)
    confidence: Confidence
    evidence_ids: list[EvidenceId] = Field(default_factory=list)


class InterfaceIR(_TopologyModel):
    temp_id: InterfaceTempId
    node_id: NodeTempId
    raw_name: str | None = None
    normalized_name: NonEmptyString
    ip_address: IPv4Address | None = None
    prefix_length: PrefixLength | None = None
    link_id: LinkTempId | None = None
    segment_id: SegmentTempId | None = None
    confidence: Confidence
    evidence_ids: list[EvidenceId] = Field(default_factory=list)


class LinkIR(_TopologyModel):
    temp_id: LinkTempId
    source_node_id: NodeTempId
    target_node_id: NodeTempId
    source_interface_id: InterfaceTempId | None = None
    target_interface_id: InterfaceTempId | None = None
    polyline: list[Point] = Field(default_factory=list)
    confidence: Confidence
    evidence_ids: list[EvidenceId] = Field(default_factory=list)


class RegionIR(_TopologyModel):
    temp_id: RegionTempId
    raw_name: NonEmptyString
    normalized_name: NonEmptyString
    bbox: BoundingBox
    member_node_ids: list[NodeTempId] = Field(default_factory=list)
    fill_color: str | None = None
    confidence: Confidence
    evidence_ids: list[EvidenceId] = Field(default_factory=list)


class SegmentIR(_TopologyModel):
    temp_id: SegmentTempId
    anchor_node_id: NodeTempId
    member_node_ids: list[NodeTempId] = Field(default_factory=list)
    member_interface_ids: list[InterfaceTempId] = Field(default_factory=list)
    cidr: IPv4Network
    gateway_ip: IPv4Address | None = None
    gateway_interface_id: InterfaceTempId | None = None
    vlan: str | None = None
    name_hint: NonEmptyString
    confidence: Confidence
    evidence_ids: list[EvidenceId] = Field(default_factory=list)


class TopologyIR(_TopologyModel):
    task_id: NonEmptyString
    image: ImageInfo
    nodes: list[NodeIR] = Field(default_factory=list)
    interfaces: list[InterfaceIR] = Field(default_factory=list)
    links: list[LinkIR] = Field(default_factory=list)
    regions: list[RegionIR] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    unresolved_items: list[UnresolvedItem] = Field(default_factory=list)
    summary: dict[str, JsonValue]


class NetworkConsistencyResult(_TopologyModel):
    is_consistent: bool
    issues: list[str] = Field(default_factory=list)


class ResolvedTopologyIR(TopologyIR):
    segments: list[SegmentIR] = Field(default_factory=list)
    network_consistency: NetworkConsistencyResult


class ResourceBinding(_TopologyModel):
    node_id: NodeTempId
    image_id: NonEmptyString
    image_name: NonEmptyString
    sys_type: NonEmptyString
    cpu: Annotated[int, Field(gt=0)]
    ram: Annotated[int, Field(gt=0)]
    disk: Annotated[int, Field(gt=0)]
    match_reason: NonEmptyString


class FlavorConfig(_TopologyModel):
    cpu: StrictNonEmptyString
    ram: StrictNonEmptyString
    disk: StrictNonEmptyString


class PropertiesConfig(_TopologyModel):
    id: PlatformNodeId
    dev_type: PlatformDevType
    node_name: NonEmptyString
    description: str | None = None
    x: StrictString
    y: StrictString
    district: str | None = None
    fill_color: str | None = None
    image_id: NonEmptyString | None = None
    image_name: NonEmptyString | None = None
    sys_type: NonEmptyString | None = None
    flavor: FlavorConfig | None = None
    user_data: str = ""
    metadata: list[JsonValue] = Field(default_factory=list)
    other_attribute_list: list[JsonValue] = Field(default_factory=list)
    single_network: StrictBool = False
    transparent: StrictInt = 0


class NicConfig(_TopologyModel):
    id: PlatformNicId
    name: NonEmptyString
    subnet_id: PlatformSubnetId
    ip: IPv4Address
    mac_address: str | None = None
    bandwidth: Annotated[float, Field(ge=0.0)] | None = None
    packet_loss_rate: Annotated[float, Field(ge=0.0)] | None = None
    delay: Annotated[float, Field(ge=0.0)] | None = None


class NodeConfig(_TopologyModel):
    type: PlatformNodeType
    properties: PropertiesConfig
    nic_list: list[NicConfig] = Field(default_factory=list)
    sec_policy_cmd: list[str] = Field(default_factory=list)
    route_table: list[str] = Field(default_factory=list)


class NetworkConfig(_TopologyModel):
    id: PlatformNetworkId
    name: NonEmptyString
    mtu: Annotated[int, Field(gt=0)] = 1350
    node_id: PlatformNodeId
    vlan: str | None = None
    transmit_node_id_list: list[PlatformNodeId] = Field(default_factory=list)


class SubnetConfig(_TopologyModel):
    id: PlatformSubnetId
    name: NonEmptyString
    cidr: IPv4Network
    gateway_ip: IPv4Address | None = None
    dns: str = ""
    network_id: PlatformNetworkId
    dhcp_pool: str | None = None
    enable_dhcp: bool = True


class LinkConfig(_TopologyModel):
    id: PlatformLinkId
    s_dev_id: PlatformNodeId
    s_nic_name: NonEmptyString | None = None
    d_dev_id: PlatformNodeId
    d_nic_name: NonEmptyString | None = None


class PlatformTopologyPayload(_TopologyModel):
    project_id: NonEmptyString
    network_id: NonEmptyString
    version: str = "v2"
    network_list: list[NetworkConfig] = Field(default_factory=list)
    subnet_list: list[SubnetConfig] = Field(default_factory=list)
    node_list: list[NodeConfig] = Field(default_factory=list)
    link_list: list[LinkConfig] = Field(default_factory=list)
    port_mapping_list: list[str] = Field(default_factory=list)


class ValidationIssue(_TopologyModel):
    severity: ValidationSeverity
    path: NonEmptyString
    code: NonEmptyString
    message: NonEmptyString
    repair: str | None = None


class ValidationReport(_TopologyModel):
    issues: list[ValidationIssue] = Field(default_factory=list)
    can_submit: bool

    @model_validator(mode="after")
    def reject_submit_with_errors(self) -> Self:
        if self.can_submit and any(
            issue.severity is ValidationSeverity.ERROR for issue in self.issues
        ):
            raise ValueError("canSubmit cannot be true when an ERROR issue exists")
        return self


class SubmissionResult(_TopologyModel):
    http_status: int | None = None
    business_code: int | None = None
    message: str
    payload_hash: NonEmptyString
    task_status: TaskStatus
    write_succeeded: bool
