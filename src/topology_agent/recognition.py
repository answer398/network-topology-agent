"""Four-pass complete-image topology recognition and deterministic fusion."""

from __future__ import annotations

import json
import hashlib
import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from ipaddress import IPv4Address, IPv4Interface, ip_address, ip_interface
from pathlib import Path
from typing import Annotated, Any, Literal, Sequence

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .image import (
    ImageBundle,
    ImageView,
    view_bbox_to_original,
    view_point_to_original,
    view_polyline_to_original,
)
from .llm import (
    ModelHttpAttempt,
    ModelImage,
    ModelUsage,
    OpenAICompatibleModelClient,
    SkillName,
)
from .models import (
    BoundingBox,
    Confidence,
    Evidence,
    EvidenceSourceType,
    InputError,
    ModelInvocationError,
    ObservedInterface,
    ObservedLink,
    ObservedNode,
    ObservedRegion,
    Point,
    SemanticDeviceType,
    TopologyObservation,
    UnresolvedCategory,
    UnresolvedItem,
)


def _to_camel(value: str) -> str:
    first, *rest = value.split("_")
    return first + "".join(part.capitalize() for part in rest)


class _PassModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="forbid",
        loc_by_alias=True,
    )

    @model_validator(mode="before")
    @classmethod
    def inherit_missing_evidence_confidence(cls, value: Any) -> Any:
        """Use an observation confidence only when a nested visual Evidence omits it."""

        if not isinstance(value, dict):
            return value
        parent_confidence = value.get("confidence")
        if (
            isinstance(parent_confidence, bool)
            or not isinstance(parent_confidence, (int, float))
            or not isinstance(value.get("evidence"), list)
        ):
            return value
        evidence = value["evidence"]
        if not any(
            isinstance(item, dict) and "confidence" not in item
            for item in evidence
        ):
            return value
        normalized = dict(value)
        normalized["evidence"] = [
            (
                {**item, "confidence": parent_confidence}
                if isinstance(item, dict) and "confidence" not in item
                else item
            )
            for item in evidence
        ]
        return normalized


_TemporaryModelId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=r"^tmp_[A-Za-z0-9][A-Za-z0-9_-]{0,63}$",
    ),
]
_ConflictId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^conflict_[0-9]{3,}$"),
]
_EvidenceDescription = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=160),
]
_TextRaw = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=240),
]
_GroupedInterfaceTextRaw = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=4096),
]
_TextCandidate = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=160),
]
_ReasonCode = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=80,
        pattern=r"^[A-Z0-9_]+$",
    ),
]
_PrefixCandidate = Annotated[int, Field(ge=0, le=32)]
_VisualEvidenceSource = Literal[
    EvidenceSourceType.VISUAL_TEXT,
    EvidenceSourceType.VISUAL_ICON,
    EvidenceSourceType.VISUAL_LINE,
    EvidenceSourceType.VISUAL_REGION,
    EvidenceSourceType.MODEL_INFERENCE,
]


_ModelCoordinate = Annotated[float, Field(multiple_of=0.01)]
_ModelDimension = Annotated[float, Field(gt=0.0, multiple_of=0.01)]


class _ModelPoint(_PassModel):
    """A visual-model point in current-view pixels centered at (0, 0)."""

    x: _ModelCoordinate
    y: _ModelCoordinate

    @field_validator("x", "y", mode="before")
    @classmethod
    def validate_coordinate(cls, value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("model coordinate must be a finite number")
        coordinate = float(value)
        if not math.isfinite(coordinate):
            raise ValueError("model coordinate must be a finite number")
        if round(coordinate, 2) != coordinate:
            raise ValueError("model coordinate must use at most two decimal places")
        return coordinate


class _ModelBoundingBox(_PassModel):
    """A visual-model box with signed center-origin x/y and pixel dimensions."""

    x: _ModelCoordinate
    y: _ModelCoordinate
    width: _ModelDimension
    height: _ModelDimension

    @field_validator("x", "y", "width", "height", mode="before")
    @classmethod
    def validate_dimension(cls, value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("model bounding-box value must be a finite number")
        dimension = float(value)
        if not math.isfinite(dimension):
            raise ValueError("model bounding-box value must be a finite number")
        if round(dimension, 2) != dimension:
            raise ValueError("model bounding-box values must use at most two decimal places")
        return dimension


class _PassEvidence(_PassModel):
    source_type: _VisualEvidenceSource
    bbox: _ModelBoundingBox | None = Field(
        default=None,
        description=(
            "Tight box in current-view pixels with the image center at (0,0). "
            "x/y may be negative, x points right, y points down, and every value uses at most two decimals. "
            "First locate bbox_2d=[x1,y1,x2,y2], then use x=x1, y=y1, width=x2-x1, height=y2-y1."
        ),
    )
    raw_text: str | None = None
    description: _EvidenceDescription
    confidence: Confidence

    @model_validator(mode="after")
    def require_visual_location(self) -> "_PassEvidence":
        if self.source_type is EvidenceSourceType.MODEL_INFERENCE:
            return self
        if self.bbox is None and not (
            isinstance(self.raw_text, str) and self.raw_text.strip()
        ):
            raise ValueError("evidence requires bbox or rawText")
        return self


class _PassUnresolved(_PassModel):
    category: str = "UNSUPPORTED_TOPOLOGY_PATTERN"
    object_ids: list[str] = Field(default_factory=list)
    field: str | None = None
    candidates: list[str] = Field(default_factory=list)
    reason: str = "visual ambiguity"
    blocking: bool = False
    recommended_action: str = "preserve candidates"
    evidence: list[_PassEvidence] = Field(default_factory=list, max_length=2)


class _StructureNodeCandidate(_PassModel):
    temporary_model_id: _TemporaryModelId
    bbox: _ModelBoundingBox = Field(
        description=(
            "Tight device-icon box in current-view pixels centered at (0,0). Locate native "
            "bbox_2d=[x1,y1,x2,y2], then emit x=x1, y=y1, width=x2-x1, height=y2-y1. "
            "Exclude labels, interface/IP text, lines, and empty margins."
        )
    )
    center: _ModelPoint = Field(
        description="Exact geometric center of bbox: x=bbox.x+bbox.width/2 and y=bbox.y+bbox.height/2."
    )
    raw_name_candidates: list[str] = Field(default_factory=list)
    semantic_type_candidates: list[SemanticDeviceType] = Field(default_factory=list)
    semantic_type: SemanticDeviceType | None = None
    region_candidates: list[str] = Field(default_factory=list)
    confidence: Confidence
    evidence: list[_PassEvidence] = Field(min_length=1, max_length=2)


class _StructureRegionCandidate(_PassModel):
    temporary_model_id: _TemporaryModelId
    bbox: _ModelBoundingBox = Field(
        description=(
            "Region boundary in current-view pixels centered at (0,0), derived from native "
            "bbox_2d corners."
        )
    )
    raw_name_candidates: list[str] = Field(default_factory=list)
    confidence: Confidence
    evidence: list[_PassEvidence] = Field(min_length=1, max_length=2)


class _StructurePassResponse(_PassModel):
    nodes: list[_StructureNodeCandidate] = Field(default_factory=list)
    regions: list[_StructureRegionCandidate] = Field(default_factory=list)
    unresolved_items: list[_PassUnresolved] = Field(default_factory=list)


class _LinkCandidate(_PassModel):
    temporary_model_id: _TemporaryModelId
    source_node_candidates: list[str] = Field(default_factory=list)
    target_node_candidates: list[str] = Field(default_factory=list)
    source_interface_candidates: list[str] = Field(default_factory=list)
    target_interface_candidates: list[str] = Field(default_factory=list)
    polyline: list[_ModelPoint] = Field(
        min_length=2,
        description=(
            "Visible path as current-view center-origin pixel points, starting and ending where the line touches "
            "the actual endpoint icon boundary and retaining every obvious bend."
        ),
    )
    crossing_uncertain: bool = False
    confidence: Confidence
    evidence: list[_PassEvidence] = Field(min_length=1, max_length=2)


class _AdditionalNodeCandidate(_StructureNodeCandidate):
    """A node candidate found during the links stage."""


class _LinksPassResponse(_PassModel):
    links: list[_LinkCandidate] = Field(default_factory=list)
    additional_node_candidates: list[_AdditionalNodeCandidate] = Field(default_factory=list)
    unresolved_items: list[_PassUnresolved] = Field(default_factory=list)


class _NodeTextObservation(_PassModel):
    raw_text: _TextRaw | None = None
    label_bbox: _ModelBoundingBox | None = Field(
        default=None, alias="labelBBox", description="Tight center-origin pixel box around visible node text only."
    )
    ip_bbox: _ModelBoundingBox | None = Field(
        default=None, alias="ipBBox", description="Tight center-origin pixel box around visible IPv4/prefix text only."
    )
    node_id_candidates: list[str] = Field(default_factory=list, max_length=8)
    name_candidates: list[_TextCandidate] = Field(default_factory=list, max_length=4)
    confidence: Confidence
    evidence: list[_PassEvidence] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def require_visible_text(self) -> "_NodeTextObservation":
        if (self.label_bbox is None and self.ip_bbox is None) or not (
            (isinstance(self.raw_text, str) and self.raw_text.strip())
            or self.name_candidates
        ):
            raise ValueError("node text requires labelBBox or ipBBox and visible text")
        return self


class _InterfaceTextObservation(_PassModel):
    raw_text: _GroupedInterfaceTextRaw | None = None
    label_bbox: _ModelBoundingBox | None = Field(
        default=None, alias="labelBBox", description="Tight center-origin pixel box around interface text only."
    )
    ip_bbox: _ModelBoundingBox | None = Field(
        default=None, alias="ipBBox", description="Tight center-origin pixel box around visible IPv4/prefix text only."
    )
    interface_name_candidates: list[_TextCandidate] = Field(default_factory=list, max_length=128)
    ipv4_candidates: list[_TextCandidate] = Field(default_factory=list, max_length=128)
    prefix_length_candidates: list[_PrefixCandidate] = Field(default_factory=list, max_length=128)
    node_id_candidates: list[str] = Field(default_factory=list, max_length=4)
    nearby_link_ids: list[str] = Field(default_factory=list, max_length=8)
    confidence: Confidence
    evidence: list[_PassEvidence] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def require_visible_text(self) -> "_InterfaceTextObservation":
        has_text = (
            (isinstance(self.raw_text, str) and self.raw_text.strip())
            or self.interface_name_candidates
            or self.ipv4_candidates
            or self.prefix_length_candidates
        )
        if not has_text or (self.label_bbox is None and self.ip_bbox is None):
            raise ValueError("interface text requires a bbox and visible text")
        return self


class _RegionTextObservation(_PassModel):
    raw_text: _TextRaw | None = None
    label_bbox: _ModelBoundingBox | None = Field(default=None, alias="labelBBox")
    region_id_candidates: list[str] = Field(default_factory=list, max_length=8)
    name_candidates: list[_TextCandidate] = Field(default_factory=list, max_length=4)
    confidence: Confidence
    evidence: list[_PassEvidence] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def require_visible_text(self) -> "_RegionTextObservation":
        if self.label_bbox is None or not (
            (isinstance(self.raw_text, str) and self.raw_text.strip())
            or self.name_candidates
        ):
            raise ValueError("region text requires labelBBox and visible text")
        return self


class _TextPassResponse(_PassModel):
    node_text_observations: list[_NodeTextObservation] = Field(
        default_factory=list, max_length=96
    )
    interface_observations: list[_InterfaceTextObservation] = Field(
        default_factory=list, max_length=192
    )
    region_text_observations: list[_RegionTextObservation] = Field(
        default_factory=list, max_length=48
    )
    checked_node_ids: list[str] = Field(default_factory=list, max_length=256)
    text_coverage_complete: bool = False
    unresolved_items: list[_PassUnresolved] = Field(default_factory=list)

    @model_validator(mode="after")
    def enforce_compact_unique_text_output(
        self, info: ValidationInfo
    ) -> "_TextPassResponse":
        limits = _text_limits_from_context(info.context)
        if limits is not None:
            text_blocks = (
                len(self.node_text_observations)
                + len(self.region_text_observations)
            )
            if text_blocks > limits.text_blocks:
                raise ValueError(
                    f"textBlocks exceeds dynamic limit {limits.text_blocks}"
                )
            if len(self.interface_observations) > limits.interfaces:
                raise ValueError(
                    f"interfaces exceeds dynamic limit {limits.interfaces}"
                )
            ipv4_observations = sum(
                len(item.ipv4_candidates) or len(_extract_ip_tokens([item.raw_text] if item.raw_text else []))
                for item in self.interface_observations
            )
            if ipv4_observations > limits.ipv4_observations:
                raise ValueError(
                    "ipv4Observations exceeds dynamic limit "
                    f"{limits.ipv4_observations}"
                )
            prefix_observations = sum(
                len(item.prefix_length_candidates)
                or sum("/" in token for token in _extract_ip_tokens([item.raw_text] if item.raw_text else []))
                for item in self.interface_observations
            )
            if prefix_observations > limits.prefix_observations:
                raise ValueError(
                    "prefixObservations exceeds dynamic limit "
                    f"{limits.prefix_observations}"
                )

        seen: set[str] = set()
        for item in (
            *self.node_text_observations,
            *self.interface_observations,
            *self.region_text_observations,
        ):
            signature = _text_observation_signature(item)
            if signature in seen:
                raise ValueError(
                    "text observations must not repeat the same text, bbox, and Evidence"
                )
            seen.add(signature)
        return self


@dataclass(frozen=True, slots=True)
class _TextOutputLimits:
    text_blocks: int
    interfaces: int
    ipv4_observations: int
    prefix_observations: int

    def as_dict(self) -> dict[str, int]:
        return {
            "textBlocks": self.text_blocks,
            "interfaces": self.interfaces,
            "ipv4Observations": self.ipv4_observations,
            "prefixObservations": self.prefix_observations,
        }


def _text_limits_from_context(context: Any) -> _TextOutputLimits | None:
    if not isinstance(context, dict):
        return None
    values = context.get("textLimits")
    if not isinstance(values, dict):
        return None
    names = (
        ("textBlocks", "text_blocks"),
        ("interfaces", "interfaces"),
        ("ipv4Observations", "ipv4_observations"),
        ("prefixObservations", "prefix_observations"),
    )
    parsed: dict[str, int] = {}
    for source_name, target_name in names:
        value = values.get(source_name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return None
        parsed[target_name] = value
    return _TextOutputLimits(**parsed)


def _text_output_limits(
    nodes: Sequence[_NodeState],
    regions: Sequence[_RegionState],
    links: Sequence[_LinkState],
) -> _TextOutputLimits:
    node_count = len(nodes)
    region_count = len(regions)
    link_count = len(links)
    # interfaceObservations are compact node-local blocks. A block can contain
    # many visible interface/IP facts, so the response does not grow once per
    # physical port merely because a device has many labelled ports.
    interface_limit = max(16, node_count + 8)
    ipv4_limit = max(32, node_count * 4 + link_count)
    prefix_limit = max(32, node_count * 4 + link_count)
    return _TextOutputLimits(
        text_blocks=min(96, max(8, node_count + region_count + 8)),
        interfaces=min(192, interface_limit),
        ipv4_observations=min(192, ipv4_limit),
        prefix_observations=min(192, prefix_limit),
    )


def _model_point_to_view(view: ImageView, point: _ModelPoint) -> Point:
    x = round(float(point.x) + view.width / 2.0, 2)
    y = round(float(point.y) + view.height / 2.0, 2)
    if not (0.0 <= x < view.width and 0.0 <= y < view.height):
        raise InputError(
            f"center-origin model point is outside view {view.view_id!r}"
        )
    return Point(x=x, y=y)


def _model_bbox_to_view(
    view: ImageView, bbox: _ModelBoundingBox
) -> BoundingBox:
    left = round(float(bbox.x) + view.width / 2.0, 2)
    top = round(float(bbox.y) + view.height / 2.0, 2)
    width = round(float(bbox.width), 2)
    height = round(float(bbox.height), 2)
    right = left + width
    bottom = top + height
    if (
        left < -1e-6
        or top < -1e-6
        or right > view.width + 1e-6
        or bottom > view.height + 1e-6
    ):
        raise InputError(
            f"center-origin model bbox is outside view {view.view_id!r}"
        )
    left = max(0.0, left)
    top = max(0.0, top)
    right = min(float(view.width), right)
    bottom = min(float(view.height), bottom)
    return BoundingBox(
        x=round(left, 2),
        y=round(top, 2),
        width=round(right - left, 2),
        height=round(bottom - top, 2),
    )


def _model_polyline_to_view(
    view: ImageView, polyline: Sequence[_ModelPoint]
) -> list[Point]:
    if not isinstance(polyline, Sequence):
        raise InputError("center-origin model polyline must be a sequence")
    return [_model_point_to_view(view, point) for point in polyline]


def _view_point_to_model(view: ImageView, point: Point) -> _ModelPoint:
    if not (0.0 <= point.x < view.width and 0.0 <= point.y < view.height):
        raise InputError(f"point is outside view {view.view_id!r}")
    return _ModelPoint(
        x=round(float(point.x) - view.width / 2.0, 2),
        y=round(float(point.y) - view.height / 2.0, 2),
    )


def _view_bbox_to_model(
    view: ImageView, bbox: BoundingBox
) -> _ModelBoundingBox:
    right = float(bbox.x + bbox.width)
    bottom = float(bbox.y + bbox.height)
    if (
        bbox.x < 0.0
        or bbox.y < 0.0
        or right > view.width + 1e-6
        or bottom > view.height + 1e-6
    ):
        raise InputError(f"bbox is outside view {view.view_id!r}")
    return _ModelBoundingBox(
        x=round(float(bbox.x) - view.width / 2.0, 2),
        y=round(float(bbox.y) - view.height / 2.0, 2),
        width=round(float(bbox.width), 2),
        height=round(float(bbox.height), 2),
    )


def _pass_evidence_in_view(
    values: Sequence[_PassEvidence], view: ImageView
) -> list[_PassEvidence]:
    return [
        item.model_copy(
            update={
                "bbox": (
                    _model_bbox_to_view(view, item.bbox)
                    if item.bbox is not None
                    else None
                )
            }
        )
        for item in values
    ]


def _structure_response_in_view(
    response: _StructurePassResponse, view: ImageView
) -> _StructurePassResponse:
    nodes = [
        item.model_copy(
            update={
                "bbox": _model_bbox_to_view(view, item.bbox),
                "center": _model_point_to_view(view, item.center),
                "evidence": _pass_evidence_in_view(item.evidence, view),
            }
        )
        for item in response.nodes
    ]
    regions = [
        item.model_copy(
            update={
                "bbox": _model_bbox_to_view(view, item.bbox),
                "evidence": _pass_evidence_in_view(item.evidence, view),
            }
        )
        for item in response.regions
    ]
    unresolved = [
        item.model_copy(
            update={"evidence": _pass_evidence_in_view(item.evidence, view)}
        )
        for item in response.unresolved_items
    ]
    return response.model_copy(
        update={"nodes": nodes, "regions": regions, "unresolved_items": unresolved}
    )


def _links_response_in_view(
    response: _LinksPassResponse, view: ImageView
) -> _LinksPassResponse:
    links = [
        item.model_copy(
            update={
                "polyline": _model_polyline_to_view(view, item.polyline),
                "evidence": _pass_evidence_in_view(item.evidence, view),
            }
        )
        for item in response.links
    ]
    additions = [
        item.model_copy(
            update={
                "bbox": _model_bbox_to_view(view, item.bbox),
                "center": _model_point_to_view(view, item.center),
                "evidence": _pass_evidence_in_view(item.evidence, view),
            }
        )
        for item in response.additional_node_candidates
    ]
    unresolved = [
        item.model_copy(
            update={"evidence": _pass_evidence_in_view(item.evidence, view)}
        )
        for item in response.unresolved_items
    ]
    return response.model_copy(
        update={
            "links": links,
            "additional_node_candidates": additions,
            "unresolved_items": unresolved,
        }
    )


def _text_response_in_view(
    response: _TextPassResponse, view: ImageView
) -> _TextPassResponse:
    nodes = [
        item.model_copy(
            update={
                "label_bbox": (
                    _model_bbox_to_view(view, item.label_bbox)
                    if item.label_bbox is not None
                    else None
                ),
                "ip_bbox": (
                    _model_bbox_to_view(view, item.ip_bbox)
                    if item.ip_bbox is not None
                    else None
                ),
                "evidence": _pass_evidence_in_view(item.evidence, view),
            }
        )
        for item in response.node_text_observations
    ]
    interfaces = [
        item.model_copy(
            update={
                "label_bbox": (
                    _model_bbox_to_view(view, item.label_bbox)
                    if item.label_bbox is not None
                    else None
                ),
                "ip_bbox": (
                    _model_bbox_to_view(view, item.ip_bbox)
                    if item.ip_bbox is not None
                    else None
                ),
                "evidence": _pass_evidence_in_view(item.evidence, view),
            }
        )
        for item in response.interface_observations
    ]
    regions = [
        item.model_copy(
            update={
                "label_bbox": _model_bbox_to_view(view, item.label_bbox),
                "evidence": _pass_evidence_in_view(item.evidence, view),
            }
        )
        for item in response.region_text_observations
    ]
    unresolved = [
        item.model_copy(
            update={"evidence": _pass_evidence_in_view(item.evidence, view)}
        )
        for item in response.unresolved_items
    ]
    return response.model_copy(
        update={
            "node_text_observations": nodes,
            "interface_observations": interfaces,
            "region_text_observations": regions,
            "unresolved_items": unresolved,
        }
    )


def _text_observation_signature(
    item: _NodeTextObservation | _InterfaceTextObservation | _RegionTextObservation,
) -> str:
    raw_text = item.raw_text
    candidate_values: list[str] = []
    for field_name in (
        "name_candidates",
        "interface_name_candidates",
        "ipv4_candidates",
        "prefix_length_candidates",
    ):
        values = getattr(item, field_name, ())
        candidate_values.extend(str(value) for value in values)
    text = (
        " ".join(raw_text.split()).casefold()
        if isinstance(raw_text, str) and raw_text.strip()
        else "|".join(sorted(set(candidate_values)))
    )
    boxes = [
        value.model_dump(mode="json")
        for value in (getattr(item, "label_bbox", None), getattr(item, "ip_bbox", None))
        if value is not None
    ]
    evidence = [
        {
            "sourceType": value.source_type.value,
            "bbox": value.bbox.model_dump(mode="json") if value.bbox else None,
            "rawText": value.raw_text,
        }
        for value in item.evidence
    ]
    evidence_keys = [
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for value in evidence
    ]
    if len(evidence_keys) != len(set(evidence_keys)):
        raise ValueError("text observation repeats the same Evidence")
    return json.dumps(
        {"text": text, "bboxes": boxes, "evidence": sorted(evidence_keys)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class _FusionDecision(_PassModel):
    conflict_id: _ConflictId
    action: Literal[
        "SELECT_CANDIDATE",
        "KEEP_MULTIPLE_CANDIDATES",
        "MERGE_OBJECTS",
        "KEEP_OBJECTS_SEPARATE",
        "BIND_REFERENCE",
        "LEAVE_UNRESOLVED",
    ]
    selected_candidate_indexes: list[int] = Field(default_factory=list)
    confidence: Confidence
    reason_code: _ReasonCode


class _FusionPassResponse(_PassModel):
    decisions: list[_FusionDecision] = Field(default_factory=list)
    consistency_findings: list[str] = Field(default_factory=list)


class _ConflictType(StrEnum):
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


@dataclass(slots=True)
class _CandidateOption:
    value: Any
    source_stage: str
    source_view_id: str
    confidence: float
    evidence_ids: list[str]


@dataclass(slots=True)
class _Conflict:
    conflict_id: str
    conflict_type: _ConflictType
    target_ids: list[str]
    candidate_options: list[_CandidateOption]
    supporting_evidence_ids: list[str]
    spatial_context: dict[str, Any] = field(default_factory=dict)
    pending_binding: _PendingBinding | None = None
    field_name: str | None = None
    resolved: bool = False


@dataclass(slots=True)
class _NodeState:
    ref: str
    observation: ObservedNode
    view_bbox: BoundingBox
    view_center: Point
    temporary_ids: set[str] = field(default_factory=set)


@dataclass(slots=True)
class _RegionState:
    ref: str
    observation: ObservedRegion
    view_bbox: BoundingBox
    temporary_ids: set[str] = field(default_factory=set)


@dataclass(slots=True)
class _LinkState:
    ref: str
    observation: ObservedLink
    temporary_id: str
    raw_source_interface_candidates: list[str] = field(default_factory=list)
    raw_target_interface_candidates: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _PendingBinding:
    binding_id: str
    kind: Literal["node_text", "interface", "region_text"]
    target_ids: list[str]
    names: list[str]
    raw_text: str | None
    evidence_ids: list[str]
    confidence: float
    candidate_values: list[str] = field(default_factory=list)
    interface: ObservedInterface | None = None
    resolved: bool = False


@dataclass(frozen=True, slots=True)
class RecognitionStatistics:
    structure_logical_calls: int
    links_logical_calls: int
    text_logical_calls: int
    fusion_logical_calls: int
    structure_http_requests: int
    links_http_requests: int
    text_http_requests: int
    fusion_http_requests: int
    structure_input_tokens: int
    structure_output_tokens: int
    links_input_tokens: int
    links_output_tokens: int
    text_input_tokens: int
    text_output_tokens: int
    fusion_input_tokens: int
    fusion_output_tokens: int
    node_count_after_pass1: int
    additional_node_count_after_pass2: int
    text_observation_count: int
    fusion_conflict_count: int
    fusion_decision_count: int
    rejected_fusion_decision_count: int
    final_unresolved_count: int

    @property
    def total_logical_calls(self) -> int:
        return self.structure_logical_calls + self.links_logical_calls + self.text_logical_calls + self.fusion_logical_calls

    @property
    def total_http_requests(self) -> int:
        return self.structure_http_requests + self.links_http_requests + self.text_http_requests + self.fusion_http_requests

    @property
    def total_input_tokens(self) -> int:
        return self.structure_input_tokens + self.links_input_tokens + self.text_input_tokens + self.fusion_input_tokens

    @property
    def total_output_tokens(self) -> int:
        return self.structure_output_tokens + self.links_output_tokens + self.text_output_tokens + self.fusion_output_tokens

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens

    def as_dict(self) -> dict[str, int]:
        values = {
            "structureLogicalCalls": self.structure_logical_calls,
            "linksLogicalCalls": self.links_logical_calls,
            "textLogicalCalls": self.text_logical_calls,
            "fusionLogicalCalls": self.fusion_logical_calls,
            "structureHttpRequests": self.structure_http_requests,
            "linksHttpRequests": self.links_http_requests,
            "textHttpRequests": self.text_http_requests,
            "fusionHttpRequests": self.fusion_http_requests,
            "structureInputTokens": self.structure_input_tokens,
            "structureOutputTokens": self.structure_output_tokens,
            "linksInputTokens": self.links_input_tokens,
            "linksOutputTokens": self.links_output_tokens,
            "textInputTokens": self.text_input_tokens,
            "textOutputTokens": self.text_output_tokens,
            "fusionInputTokens": self.fusion_input_tokens,
            "fusionOutputTokens": self.fusion_output_tokens,
            "totalLogicalCalls": self.total_logical_calls,
            "totalHttpRequests": self.total_http_requests,
            "totalInputTokens": self.total_input_tokens,
            "totalOutputTokens": self.total_output_tokens,
            "totalTokens": self.total_tokens,
            "nodeCountAfterPass1": self.node_count_after_pass1,
            "additionalNodeCountAfterPass2": self.additional_node_count_after_pass2,
            "textObservationCount": self.text_observation_count,
            "fusionConflictCount": self.fusion_conflict_count,
            "fusionDecisionCount": self.fusion_decision_count,
            "rejectedFusionDecisionCount": self.rejected_fusion_decision_count,
            "finalUnresolvedCount": self.final_unresolved_count,
        }
        return values


@dataclass(slots=True)
class _RecognitionRunArtifacts:
    run_dir: Path
    log_path: Path
    sequence: int = 0

    @classmethod
    def create(
        cls, task_id: str, bundle: ImageBundle
    ) -> "_RecognitionRunArtifacts":
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", task_id):
            directory_name = task_id
        else:
            digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:16]
            directory_name = f"task_{digest}"
        task_dir = (
            Path(__file__).resolve().parents[2]
            / "runtime"
            / "runs"
            / directory_name
        )
        try:
            task_dir.mkdir(parents=True, exist_ok=True)
            run_dir = _next_attempt_directory(task_dir)
            log_path = run_dir / "recognition.jsonl"
            log_path.touch(exist_ok=False)
        except OSError:
            raise InputError("cannot create recognition run artifacts") from None
        artifacts = cls(run_dir=run_dir, log_path=log_path)
        artifacts.write(
            {
                "event": "recognitionStarted",
                "taskId": task_id,
                "imageWidth": bundle.image_info.width,
                "imageHeight": bundle.image_info.height,
                "imageFormat": bundle.image_info.format,
                "imageSha256": bundle.sha256,
                "expectedLogicalCalls": 4,
                "attemptId": run_dir.name,
            }
        )
        return artifacts

    def write(self, values: dict[str, Any]) -> None:
        self.sequence += 1
        record = {"sequence": self.sequence, **values}
        try:
            with self.log_path.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                    + "\n"
                )
        except OSError:
            raise InputError("cannot write recognition stage log") from None

    def render_view(self, stage: str, view: ImageView) -> None:
        filename = f"{view.view_id}.png"
        try:
            view.image.save(self.run_dir / filename, format="PNG")
        except (OSError, ValueError):
            raise InputError(f"cannot render {stage} stage image") from None
        self.write(
            {
                "event": "stageImageRendered",
                "stage": stage,
                "viewId": view.view_id,
                "filename": filename,
                "width": view.width,
                "height": view.height,
                "coversCompleteImage": True,
            }
        )

    def write_stage_context(self, stage: str, values: dict[str, Any]) -> None:
        filename = f"{_checked_stage(stage)}_context.json"
        self._write_json_artifact(filename, values)
        self.write(
            {
                "event": "stageContextWritten",
                "stage": stage,
                "filename": filename,
            }
        )

    def write_model_responses(
        self, stage: str, responses: Sequence[dict[str, Any]]
    ) -> None:
        filename = f"{_checked_stage(stage)}_model_responses.json"
        self._write_json_artifact(
            filename,
            {
                "stage": stage,
                "responseCount": len(responses),
                "responses": list(responses),
            },
        )
        self.write(
            {
                "event": "modelResponsesWritten",
                "stage": stage,
                "filename": filename,
                "responseCount": len(responses),
            }
        )

    def write_http_attempts(self, attempts: Sequence[ModelHttpAttempt]) -> None:
        for attempt in attempts:
            self.write({"event": "httpAttempt", **attempt.as_dict()})

    def write_evidence_snapshot(
        self,
        stage: str,
        view_id: str,
        evidence: dict[str, Evidence],
    ) -> None:
        filename = f"{_checked_visual_stage(stage)}_evidence.json"
        values = [
            evidence[evidence_id].model_dump(mode="json", by_alias=True)
            for evidence_id in sorted(evidence)
            if evidence[evidence_id].source_view_id == view_id
        ]
        self._write_json_artifact(
            filename,
            {
                "stage": stage,
                "sourceViewId": view_id,
                "evidence": values,
            },
        )
        self.write(
            {
                "event": "stageEvidenceWritten",
                "stage": stage,
                "viewId": view_id,
                "filename": filename,
                "evidenceCount": len(values),
            }
        )

    def write_observation(self, observation: TopologyObservation) -> None:
        filename = "topology_observation.json"
        self._write_json_artifact(
            filename,
            observation.model_dump(mode="json", by_alias=True),
        )
        self.write(
            {
                "event": "observationWritten",
                "filename": filename,
                "evidenceCount": len(observation.evidence),
                "unresolvedCount": len(observation.unresolved_items),
            }
        )

    def write_fusion_patch(self, response: _FusionPassResponse) -> None:
        filename = "fusion_patch.json"
        self._write_json_artifact(
            filename,
            response.model_dump(mode="json", by_alias=True),
        )
        self.write(
            {
                "event": "fusionPatchWritten",
                "filename": filename,
                "decisionCount": len(response.decisions),
            }
        )

    def _write_json_artifact(self, filename: str, values: Any) -> None:
        try:
            (self.run_dir / filename).write_text(
                json.dumps(values, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except (OSError, TypeError, ValueError):
            raise InputError(f"cannot write recognition artifact {filename}") from None

    def stage_started(self, stage: str, view_id: str | None) -> None:
        self.write(
            {
                "event": "stageStarted",
                "stage": stage,
                "viewId": view_id,
                "imageAttached": view_id is not None,
            }
        )

    def stage_succeeded(self, stage: str, usage: ModelUsage) -> None:
        self.write(
            {
                "event": "stageSucceeded",
                "stage": stage,
                "logicalCalls": usage.logical_call_count,
                "httpRequests": usage.request_count,
                "inputTokens": usage.prompt_tokens,
                "outputTokens": usage.completion_tokens,
            }
        )

    def stage_failed(
        self,
        stage: str,
        error: Exception,
        usage: ModelUsage | None = None,
    ) -> None:
        values: dict[str, Any] = {
            "event": "stageFailed",
            "stage": stage,
            "errorType": type(error).__name__,
        }
        if usage is not None:
            values.update(
                {
                    "logicalCalls": usage.logical_call_count,
                    "httpRequests": usage.request_count,
                    "inputTokens": usage.prompt_tokens,
                    "outputTokens": usage.completion_tokens,
                }
            )
        self.write(values)


def recognize_topology(
    *,
    task_id: str,
    image_bundle: ImageBundle,
    model_client: OpenAICompatibleModelClient,
) -> TopologyObservation:
    """Run structure, links, text, and text-only fusion exactly once each."""

    if not isinstance(task_id, str) or not task_id.strip():
        raise InputError("taskId must be a non-empty string")
    if not isinstance(image_bundle, ImageBundle):
        raise InputError("imageBundle must be an ImageBundle")
    if not isinstance(model_client, OpenAICompatibleModelClient):
        raise InputError("modelClient must be an OpenAICompatibleModelClient")
    task_id = task_id.strip()
    _ensure_four_call_budget(model_client)
    initial_logical_calls = _client_logical_call_count(model_client)
    model_client.last_stage_usages = {}
    artifacts = _RecognitionRunArtifacts.create(task_id, image_bundle)

    evidence: dict[str, Evidence] = {}
    unresolved: list[UnresolvedItem] = []
    artifacts.stage_started("structure", image_bundle.structure_view.view_id)
    stage_usage_before = model_client.usage
    stage_attempt_start = len(model_client.http_attempts)
    try:
        artifacts.render_view("structure", image_bundle.structure_view)
        nodes, regions, pass_unresolved, structure_result = _run_structure_pass(
            task_id, image_bundle, model_client, evidence, artifacts
        )
        artifacts.write_model_responses(
            "structure", model_client.last_call_responses
        )
        artifacts.write_evidence_snapshot(
            "structure", image_bundle.structure_view.view_id, evidence
        )
    except Exception as exc:
        artifacts.write_model_responses(
            "structure", model_client.last_call_responses
        )
        _write_new_http_attempts(artifacts, model_client, stage_attempt_start)
        artifacts.stage_failed(
            "structure",
            exc,
            _model_usage_delta(model_client.usage, stage_usage_before),
        )
        raise
    _write_new_http_attempts(artifacts, model_client, stage_attempt_start)
    artifacts.stage_succeeded("structure", structure_result.usage)
    unresolved.extend(pass_unresolved)
    node_count_after_pass1 = len(nodes)
    artifacts.write(
        {
            "event": "stageCounts",
            "stage": "structure",
            "nodeCount": len(nodes),
            "regionCount": len(regions),
        }
    )

    artifacts.stage_started("links", "global_links")
    stage_usage_before = model_client.usage
    stage_attempt_start = len(model_client.http_attempts)
    try:
        links_view = image_bundle.register_links_view(
            [
                (
                    node.ref,
                    node.view_bbox,
                    node.view_center,
                )
                for node in nodes
            ],
            [(region.ref, region.view_bbox) for region in regions],
        )
        artifacts.render_view("links", links_view)
        links, additional_count, pass_unresolved = _run_links_pass(
            task_id,
            image_bundle,
            model_client,
            nodes,
            regions,
            evidence,
            artifacts,
        )
        artifacts.write_model_responses("links", model_client.last_call_responses)
        raw_link_count = len(links)
        composed_path_rejections = _remove_composed_path_links(links, nodes)
        for rejection in composed_path_rejections:
            artifacts.write({"event": "composedPathLinkRejected", **rejection})
        artifacts.write_evidence_snapshot("links", links_view.view_id, evidence)
    except Exception as exc:
        artifacts.write_model_responses("links", model_client.last_call_responses)
        _write_new_http_attempts(artifacts, model_client, stage_attempt_start)
        artifacts.stage_failed(
            "links",
            exc,
            _model_usage_delta(model_client.usage, stage_usage_before),
        )
        raise
    _write_new_http_attempts(artifacts, model_client, stage_attempt_start)
    artifacts.stage_succeeded(
        "links", _last_stage_usage(model_client, "links")
    )
    unresolved.extend(pass_unresolved)
    artifacts.write(
        {
            "event": "stageCounts",
            "stage": "links",
            "rawLinkCount": raw_link_count,
            "linkCount": len(links),
            "composedPathRejectedCount": len(composed_path_rejections),
            "additionalNodeCount": additional_count,
        }
    )

    artifacts.stage_started("text", image_bundle.text_enhanced_view.view_id)
    stage_usage_before = model_client.usage
    stage_attempt_start = len(model_client.http_attempts)
    try:
        artifacts.render_view("text", image_bundle.text_enhanced_view)
        text_count, pass_unresolved, pending_bindings = _run_text_pass(
            task_id,
            image_bundle,
            model_client,
            nodes,
            regions,
            links,
            evidence,
            artifacts,
        )
        artifacts.write_model_responses("text", model_client.last_call_responses)
        artifacts.write_evidence_snapshot(
            "text", image_bundle.text_enhanced_view.view_id, evidence
        )
    except Exception as exc:
        artifacts.write_model_responses("text", model_client.last_call_responses)
        _write_new_http_attempts(artifacts, model_client, stage_attempt_start)
        artifacts.stage_failed(
            "text",
            exc,
            _model_usage_delta(model_client.usage, stage_usage_before),
        )
        raise
    _write_new_http_attempts(artifacts, model_client, stage_attempt_start)
    artifacts.stage_succeeded(
        "text", _last_stage_usage(model_client, "text")
    )
    unresolved.extend(pass_unresolved)
    artifacts.write(
        {
            "event": "stageCounts",
            "stage": "text",
            "textObservationCount": text_count,
        }
    )

    artifacts.write({"event": "preFusionStarted"})
    try:
        _sanitize_node_names(nodes)
        unresolved.extend(
            _materialize_link_interfaces(links, nodes, pending_bindings)
        )
        unresolved.extend(_normalize_link_interface_candidates(links, nodes))
        unresolved = _renumber_unresolved(
            _normalize_unresolved_references(
                unresolved,
                nodes,
                regions,
                links,
                evidence,
                extra_interfaces=[
                    item.interface
                    for item in pending_bindings
                    if item.interface is not None
                ],
            )
        )
        conflicts = _build_conflicts(
            nodes, regions, links, evidence, unresolved, pending_bindings
        )
    except Exception as exc:
        artifacts.stage_failed("preFusion", exc)
        raise
    artifacts.write(
        {
            "event": "preFusionSucceeded",
            "conflictCount": len(conflicts),
            "unresolvedCount": len(unresolved),
        }
    )

    artifacts.stage_started("fusion", None)
    stage_usage_before = model_client.usage
    stage_attempt_start = len(model_client.http_attempts)
    try:
        fusion_result = _run_fusion_pass(
            task_id,
            image_bundle,
            model_client,
            nodes,
            regions,
            links,
            evidence,
            conflicts,
            unresolved,
            pending_bindings,
            artifacts,
        )
        artifacts.write_model_responses("fusion", model_client.last_call_responses)
        artifacts.write_fusion_patch(fusion_result.value)
    except Exception as exc:
        artifacts.write_model_responses("fusion", model_client.last_call_responses)
        _write_new_http_attempts(artifacts, model_client, stage_attempt_start)
        artifacts.stage_failed(
            "fusion",
            exc,
            _model_usage_delta(model_client.usage, stage_usage_before),
        )
        raise
    _write_new_http_attempts(artifacts, model_client, stage_attempt_start)
    artifacts.stage_succeeded("fusion", fusion_result.usage)
    if fusion_result.repaired:
        rejected = len(fusion_result.value.decisions)
        unresolved.extend(
            _conflict_unresolved(
                conflict,
                "fusion patch required structural repair and was not applied",
            )
            for conflict in conflicts
        )
    else:
        rejected = _apply_fusion_decisions(
            fusion_result.value.decisions,
            conflicts,
            nodes,
            regions,
            links,
            unresolved,
            pending_bindings,
        )
    _sanitize_node_names(nodes)
    _normalize_link_interface_candidates(links, nodes, emit_unresolved=False)
    unresolved.extend(_unresolved_for_remaining_conflicts(conflicts))
    artifacts.write(
        {
            "event": "stageCounts",
            "stage": "fusion",
            "conflictCount": len(conflicts),
            "decisionCount": len(fusion_result.value.decisions),
            "rejectedDecisionCount": rejected,
            "imageAttached": False,
        }
    )

    artifacts.write({"event": "finalizationStarted"})
    try:
        _dedupe_node_interfaces(nodes, links)
        unresolved.extend(_audit_link_geometry(links, nodes))
        observation = _build_observation(
            task_id,
            image_bundle,
            nodes,
            regions,
            links,
            evidence,
            unresolved,
            pending_bindings,
        )
        statistics = _make_statistics(
            structure_result.usage,
            _last_stage_usage(model_client, "links"),
            _last_stage_usage(model_client, "text"),
            fusion_result.usage,
            node_count_after_pass1,
            additional_count,
            text_count,
            len(conflicts),
            len(fusion_result.value.decisions),
            rejected,
            len(observation.unresolved_items),
            _client_logical_call_count(model_client) - initial_logical_calls,
        )
        model_client.last_recognition_statistics = statistics
        observation.summary = {**observation.summary, **statistics.as_dict()}
        artifacts.write_observation(observation)
    except Exception as exc:
        artifacts.stage_failed("finalization", exc)
        raise
    artifacts.write(
        {
            "event": "recognitionSucceeded",
            "summary": observation.summary,
        }
    )
    return observation


def _run_structure_pass(
    task_id: str,
    bundle: ImageBundle,
    client: OpenAICompatibleModelClient,
    evidence: dict[str, Evidence],
    artifacts: _RecognitionRunArtifacts,
) -> tuple[list[_NodeState], list[_RegionState], list[UnresolvedItem], Any]:
    view = bundle.structure_view
    request_context = _visual_request_context(task_id, "structure", view, bundle)
    artifacts.write_stage_context("structure", request_context)
    task_text = _stage_header(task_id, "structure", view, bundle) + "\n" + (
        "Identify visible device nodes and regions only. Use native 2D object positioning on each device icon: "
        "first locate bbox_2d=[x1,y1,x2,y2], then return x=x1, y=y1, width=x2-x1, height=y2-y1 "
        "as current-view pixel values relative to the image center. Use at most two decimals for every geometric "
        "value; x increases rightward and y increases downward. Boxes must tightly enclose icon pixels and exclude labels, lines, "
        "IP/interface text, and empty margins. Return node and region candidates, names, "
        "coarse type candidates, evidence, and unresolved items. "
        "Do not identify links, interfaces, IP values, network semantics, or platform fields.\n"
        "Structured request context:\n"
        + json.dumps(request_context, ensure_ascii=False, separators=(",", ":"))
    )
    result = client.call_structured(
        task_text=task_text,
        images=[ModelImage(view_id=view.view_id, image=view.image)],
        response_model=_StructurePassResponse,
        skill=SkillName.TOPOLOGY_RECOGNITION,
        allow_repair=False,
        request_stage="structure",
    )
    response = _structure_response_in_view(result.value, view)
    _require_unique_temporary_ids(
        [candidate.temporary_model_id for candidate in response.nodes],
        "structure nodes",
    )
    _require_unique_temporary_ids(
        [candidate.temporary_model_id for candidate in response.regions],
        "structure regions",
    )
    _require_unique_temporary_ids(
        [
            *[candidate.temporary_model_id for candidate in response.nodes],
            *[candidate.temporary_model_id for candidate in response.regions],
        ],
        "structure objects",
    )
    node_candidates = sorted(
        enumerate(response.nodes), key=lambda item: (item[1].bbox.y, item[1].bbox.x, item[0])
    )
    region_candidates = sorted(
        enumerate(response.regions), key=lambda item: (item[1].bbox.y, item[1].bbox.x, item[0])
    )
    nodes: list[_NodeState] = []
    region_candidates_by_temp: dict[str, list[str]] = {}
    for index, (_, candidate) in enumerate(node_candidates, start=1):
        candidate_bbox = _fit_view_bbox_to_bounds(candidate.bbox, view)
        _validate_view_candidate_geometry(candidate_bbox, candidate.center, view)
        ref = f"N{index:03d}"
        obs_id = f"obs_node_{index:03d}"
        type_candidates = _unique_types(candidate.semantic_type_candidates)
        if candidate.semantic_type is not None:
            type_candidates = _unique_types([candidate.semantic_type, *type_candidates])
        if not type_candidates:
            type_candidates = [SemanticDeviceType.UNKNOWN]
        semantic_type = type_candidates[0] if type_candidates else SemanticDeviceType.UNKNOWN
        names = _clean_node_name_candidates(candidate.raw_name_candidates)
        evidence_ids = _register_pass_evidence(
            evidence, view, candidate.evidence, fallback_bbox=candidate_bbox
        )
        node = ObservedNode(
            observation_id=obs_id,
            raw_name=names[0] if names else None,
            name_candidates=names,
            semantic_type=semantic_type,
            type_candidates=type_candidates,
            bbox=view_bbox_to_original(view, candidate_bbox),
            center=view_point_to_original(view, candidate.center),
            observed_interfaces=[],
            region_candidates=[],
            confidence=candidate.confidence,
            evidence_ids=evidence_ids,
            source_view_ids=[view.view_id],
        )
        nodes.append(
            _NodeState(
                ref,
                node,
                candidate_bbox,
                candidate.center,
                {candidate.temporary_model_id},
            )
        )
        region_candidates_by_temp[candidate.temporary_model_id] = list(candidate.region_candidates)

    regions: list[_RegionState] = []
    region_temp_to_ref: dict[str, str] = {}
    for index, (_, candidate) in enumerate(region_candidates, start=1):
        candidate_bbox = _fit_view_bbox_to_bounds(candidate.bbox, view)
        ref = f"R{index:03d}"
        obs_id = f"obs_region_{index:03d}"
        names = _unique_strings(candidate.raw_name_candidates)
        evidence_ids = _register_pass_evidence(
            evidence, view, candidate.evidence, fallback_bbox=candidate_bbox
        )
        region = ObservedRegion(
            observation_id=obs_id,
            raw_name=names[0] if names else None,
            name_candidates=names,
            bbox=view_bbox_to_original(view, candidate_bbox),
            member_node_candidates=[],
            confidence=candidate.confidence,
            evidence_ids=evidence_ids,
        )
        regions.append(_RegionState(ref, region, candidate_bbox, {candidate.temporary_model_id}))
        region_temp_to_ref[candidate.temporary_model_id] = ref

    region_refs = {item.ref for item in regions}
    unknown_region_unresolved: list[UnresolvedItem] = []
    for node in nodes:
        supplied_regions = [
            candidate
            for temporary_id in node.temporary_ids
            for candidate in region_candidates_by_temp.get(temporary_id, [])
        ]
        explicit_refs = _resolve_refs(
            supplied_regions,
            region_temp_to_ref,
            region_refs,
        )
        unknown_regions = [
            value
            for value in supplied_regions
            if _resolve_ref(value, region_temp_to_ref, region_refs) is None
        ]
        geometric_refs = [
            region.ref
            for region in regions
            if _point_in_bbox(node.view_center, region.view_bbox)
        ]
        candidate_refs = _unique_strings([*explicit_refs, *geometric_refs])
        if candidate_refs:
            node.observation = _replace_node(
                node.observation,
                region_candidates=[
                    _region_by_ref(regions, value).observation.observation_id
                    for value in candidate_refs
                ],
            )
        if unknown_regions:
            unknown_region_unresolved.append(
                _make_binding_unresolved(
                    "REGION_MEMBERSHIP_AMBIGUITY",
                    [node.observation.observation_id],
                    "structure node references an unknown region",
                    node.observation.evidence_ids,
                    candidates=unknown_regions,
                    field="regionCandidates",
                )
            )
    for region in regions:
        members = [
            node.observation.observation_id
            for node in nodes
            if region.observation.observation_id in node.observation.region_candidates
        ]
        region.observation = _replace_region(
            region.observation,
            member_node_candidates=_unique_strings(members),
        )
    unresolved = _convert_pass_unresolved(response.unresolved_items, view, evidence, "structure")
    unresolved.extend(unknown_region_unresolved)
    _record_stage_usage(client, "structure", result.usage)
    return nodes, regions, unresolved, result


def _run_links_pass(
    task_id: str,
    bundle: ImageBundle,
    client: OpenAICompatibleModelClient,
    nodes: list[_NodeState],
    regions: list[_RegionState],
    evidence: dict[str, Evidence],
    artifacts: _RecognitionRunArtifacts,
) -> tuple[list[_LinkState], int, list[UnresolvedItem]]:
    view = bundle.links_view
    if view is None:
        raise InputError("global_links was not registered")
    structure_summary = {
        "nodes": [
            {
                "id": node.ref,
                "bbox": _view_bbox_to_model(view, node.view_bbox).model_dump(by_alias=True),
                "center": _view_point_to_model(view, node.view_center).model_dump(by_alias=True),
                "typeCandidates": [item.value for item in node.observation.type_candidates],
            }
            for node in nodes
        ],
        "regions": [
            {
                "id": region.ref,
                "bbox": _view_bbox_to_model(view, region.view_bbox).model_dump(by_alias=True),
            }
            for region in regions
        ],
    }
    request_context = _visual_request_context(task_id, "links", view, bundle)
    request_context["numberedStructure"] = structure_summary
    artifacts.write_stage_context("links", request_context)
    task_text = _stage_header(task_id, "links", view, bundle) + "\n" + (
        "Identify visible physical or logical connection lines using only the numbered complete image. "
        "Return endpoint candidates, polylines as current-view center-origin pixel points with at most two "
        "decimal places, crossing uncertainty, "
        "evidence, and any additional node candidates. "
        "A numbered device terminates a link: do not concatenate segments through a third numbered device into "
        "a direct edge. If two segments meet at that device, output one link for each segment. "
        "Every evidence bbox must have positive width and height; for a vertical or horizontal line, "
        "enclose the visible stroke thickness instead of returning a zero-sized line bound. "
        "Preserve a visible interface label attached to a link endpoint in sourceInterfaceCandidates or "
        "targetInterfaceCandidates. Do not extract IP values, CIDR, gateways, unrelated text, or platform fields.\n"
        "Structured request context:\n"
        + json.dumps(request_context, ensure_ascii=False, separators=(",", ":"))
    )
    result = client.call_structured(
        task_text=task_text,
        images=[ModelImage(view_id=view.view_id, image=view.image)],
        response_model=_LinksPassResponse,
        skill=SkillName.TOPOLOGY_RECOGNITION,
        allow_repair=False,
        request_stage="links",
    )
    response = _links_response_in_view(result.value, view)
    _require_unique_temporary_ids(
        [candidate.temporary_model_id for candidate in response.additional_node_candidates],
        "links additional nodes",
    )
    _require_unique_temporary_ids(
        [candidate.temporary_model_id for candidate in response.links],
        "links",
    )
    known_temporary_ids = {
        temporary_id
        for node in nodes
        for temporary_id in node.temporary_ids
    } | {
        temporary_id
        for region in regions
        for temporary_id in region.temporary_ids
    }
    additional_ids = {
        candidate.temporary_model_id
        for candidate in response.additional_node_candidates
    }
    if known_temporary_ids.intersection(additional_ids):
        raise ModelInvocationError("links additional nodes contain reused temporary IDs")
    link_ids = {
        candidate.temporary_model_id for candidate in response.links
    }
    if known_temporary_ids.intersection(link_ids) or additional_ids.intersection(link_ids):
        raise ModelInvocationError("links contain reused temporary IDs")
    node_refs = {node.ref for node in nodes}
    temp_to_ref = {temp: node.ref for node in nodes for temp in node.temporary_ids}
    temp_to_ref.update(
        {node.observation.observation_id: node.ref for node in nodes}
    )
    region_refs = {region.ref for region in regions}
    region_aliases = _all_temp_refs(regions)
    unresolved = _convert_pass_unresolved(response.unresolved_items, view, evidence, "links")
    additions = sorted(
        enumerate(response.additional_node_candidates),
        key=lambda item: (item[1].bbox.y, item[1].bbox.x, item[0]),
    )
    additional_count = 0
    next_node_number = len(nodes) + 1
    for _, candidate in additions:
        candidate_bbox = _fit_view_bbox_to_bounds(candidate.bbox, view)
        _validate_view_candidate_geometry(candidate_bbox, candidate.center, view)
        explicit_regions = _resolve_refs(
            candidate.region_candidates,
            region_aliases,
            region_refs,
        )
        geometric_regions = [
            region.ref
            for region in regions
            if _point_in_bbox(candidate.center, region.view_bbox)
        ]
        region_ids = [
            _region_by_ref(regions, ref).observation.observation_id
            for ref in _unique_strings([*explicit_regions, *geometric_regions])
        ]
        unknown_regions = [
            value
            for value in candidate.region_candidates
            if _resolve_ref(value, region_aliases, region_refs) is None
        ]
        duplicate = _obvious_duplicate(candidate_bbox, candidate.center, nodes, view)
        if duplicate is not None:
            ids = _register_pass_evidence(evidence, view, candidate.evidence, fallback_bbox=candidate_bbox)
            type_candidates = _unique_types(candidate.semantic_type_candidates)
            if candidate.semantic_type is not None:
                type_candidates = _unique_types([candidate.semantic_type, *type_candidates])
            if not type_candidates:
                type_candidates = [SemanticDeviceType.UNKNOWN]
            names = _clean_node_name_candidates(candidate.raw_name_candidates)
            duplicate.observation = _replace_node(
                duplicate.observation,
                raw_name=duplicate.observation.raw_name or (names[0] if names else None),
                name_candidates=_unique_strings(
                    [*duplicate.observation.name_candidates, *names]
                ),
                type_candidates=_unique_types(
                    [*duplicate.observation.type_candidates, *type_candidates]
                ),
                region_candidates=_unique_strings(
                    [*duplicate.observation.region_candidates, *region_ids]
                ),
                evidence_ids=_unique_strings([*duplicate.observation.evidence_ids, *ids]),
                source_view_ids=_unique_strings([*duplicate.observation.source_view_ids, view.view_id]),
            )
            if (
                duplicate.observation.semantic_type is SemanticDeviceType.UNKNOWN
                and type_candidates
            ):
                duplicate.observation = _replace_node(
                    duplicate.observation,
                    semantic_type=type_candidates[0],
                )
            duplicate.temporary_ids.add(candidate.temporary_model_id)
            temp_to_ref[candidate.temporary_model_id] = duplicate.ref
            if unknown_regions:
                unresolved.append(
                    _make_binding_unresolved(
                        "REGION_MEMBERSHIP_AMBIGUITY",
                        [duplicate.observation.observation_id],
                        "additional node references an unknown region",
                        ids,
                        candidates=unknown_regions,
                        field="regionCandidates",
                    )
                )
            continue
        ref = f"N{next_node_number:03d}"
        obs_id = f"obs_node_{next_node_number:03d}"
        next_node_number += 1
        additional_count += 1
        type_candidates = _unique_types(candidate.semantic_type_candidates)
        if candidate.semantic_type is not None:
            type_candidates = _unique_types([candidate.semantic_type, *type_candidates])
        if not type_candidates:
            type_candidates = [SemanticDeviceType.UNKNOWN]
        names = _clean_node_name_candidates(candidate.raw_name_candidates)
        ids = _register_pass_evidence(evidence, view, candidate.evidence, fallback_bbox=candidate_bbox)
        nodes.append(
            _NodeState(
                ref,
                ObservedNode(
                    observation_id=obs_id,
                    raw_name=names[0] if names else None,
                    name_candidates=names,
                    semantic_type=type_candidates[0] if type_candidates else SemanticDeviceType.UNKNOWN,
                    type_candidates=type_candidates,
                    bbox=view_bbox_to_original(view, candidate_bbox),
                    center=view_point_to_original(view, candidate.center),
                    observed_interfaces=[],
                    region_candidates=region_ids,
                    confidence=candidate.confidence,
                    evidence_ids=ids,
                    source_view_ids=[view.view_id],
                ),
                candidate_bbox,
                candidate.center,
                {candidate.temporary_model_id},
            )
        )
        temp_to_ref[candidate.temporary_model_id] = ref
        node_refs.add(ref)
        if unknown_regions:
            unresolved.append(
                _make_binding_unresolved(
                    "REGION_MEMBERSHIP_AMBIGUITY",
                    [obs_id],
                    "additional node references an unknown region",
                    ids,
                    candidates=unknown_regions,
                    field="regionCandidates",
                )
            )

    for region in regions:
        members = [
            node.observation.observation_id
            for node in nodes
            if region.observation.observation_id
            in node.observation.region_candidates
        ]
        region.observation = _replace_region(
            region.observation,
            member_node_candidates=_unique_strings(members),
        )

    links: list[_LinkState] = []
    for index, candidate in enumerate(response.links, start=1):
        _validate_view_polyline(candidate.polyline, view)
        source = _resolve_refs(candidate.source_node_candidates, temp_to_ref, node_refs)
        target = _resolve_refs(candidate.target_node_candidates, temp_to_ref, node_refs)
        unknown_source = [value for value in candidate.source_node_candidates if _resolve_ref(value, temp_to_ref, node_refs) is None]
        unknown_target = [value for value in candidate.target_node_candidates if _resolve_ref(value, temp_to_ref, node_refs) is None]
        source_interfaces = _unique_strings(candidate.source_interface_candidates)
        target_interfaces = _unique_strings(candidate.target_interface_candidates)
        ids = _register_pass_evidence(
            evidence,
            view,
            candidate.evidence,
            fallback_bbox=_polyline_bbox(candidate.polyline, view),
        )
        link_id = f"obs_link_{index:03d}"
        link = ObservedLink(
            observation_id=link_id,
            source_node_candidates=[_node_obs_id(nodes, item) for item in source],
            target_node_candidates=[_node_obs_id(nodes, item) for item in target],
            source_interface_candidates=source_interfaces,
            target_interface_candidates=target_interfaces,
            polyline=view_polyline_to_original(view, candidate.polyline),
            crossing_uncertain=candidate.crossing_uncertain,
            confidence=candidate.confidence,
            evidence_ids=ids,
        )
        links.append(
            _LinkState(
                link_id,
                link,
                candidate.temporary_model_id,
                raw_source_interface_candidates=source_interfaces,
                raw_target_interface_candidates=target_interfaces,
            )
        )
        for field_name, resolved, unknown, supplied in (
            (
                "sourceNodeCandidates",
                source,
                unknown_source,
                candidate.source_node_candidates,
            ),
            (
                "targetNodeCandidates",
                target,
                unknown_target,
                candidate.target_node_candidates,
            ),
        ):
            if resolved and not unknown:
                continue
            candidates = _unique_strings(
                [*unknown, *supplied]
                or [f"{field_name} was not observed"]
            )
            unresolved.append(
                _make_binding_unresolved(
                    "LINK_ENDPOINT_AMBIGUITY",
                    [link_id],
                    f"{field_name} is missing or references an unknown numbered node",
                    ids,
                    candidates=candidates,
                    field=field_name,
                )
            )
    _record_stage_usage(client, "links", result.usage)
    return links, additional_count, unresolved


def _run_text_pass(
    task_id: str,
    bundle: ImageBundle,
    client: OpenAICompatibleModelClient,
    nodes: list[_NodeState],
    regions: list[_RegionState],
    links: list[_LinkState],
    evidence: dict[str, Evidence],
    artifacts: _RecognitionRunArtifacts,
) -> tuple[int, list[UnresolvedItem], list[_PendingBinding]]:
    view = bundle.text_enhanced_view
    output_limits = _text_output_limits(nodes, regions, links)
    summary = {
        "nodes": [
            {
                "id": node.ref,
                "observationId": node.observation.observation_id,
                "bbox": _view_bbox_to_model(view, node.view_bbox).model_dump(by_alias=True),
                "center": _view_point_to_model(view, node.view_center).model_dump(by_alias=True),
                "typeCandidates": [item.value for item in node.observation.type_candidates],
                "knownNameCandidates": node.observation.name_candidates,
            }
            for node in nodes
        ],
        "regions": [
            {
                "id": region.ref,
                "observationId": region.observation.observation_id,
                "bbox": _view_bbox_to_model(view, region.view_bbox).model_dump(by_alias=True),
                "knownNameCandidates": region.observation.name_candidates,
            }
            for region in regions
        ],
        "links": [
            {
                "id": link.ref,
                "sourceNodeCandidates": link.observation.source_node_candidates,
                "targetNodeCandidates": link.observation.target_node_candidates,
                "knownSourceInterfaceLabels": link.raw_source_interface_candidates,
                "knownTargetInterfaceLabels": link.raw_target_interface_candidates,
            }
            for link in links
        ],
    }
    request_context = _visual_request_context(task_id, "text", view, bundle)
    request_context["currentTopology"] = summary
    request_context["outputLimits"] = output_limits.as_dict()
    artifacts.write_stage_context("text", request_context)
    artifacts.write({"event": "textOutputLimits", **output_limits.as_dict()})
    task_text = _stage_header(task_id, "text", view, bundle) + "\n" + (
        "This is an incremental, compact text pass over one complete enhanced image. currentTopology already "
        "contains structure names, region titles, and link-stage interface labels. Do not repeat a node or "
        "region label that is already represented by knownNameCandidates. Do not repeat a non-IP interface label "
        "that is already represented by the link's knownSourceInterfaceLabels or knownTargetInterfaceLabels. "
        "Only output newly visible text, corrections, IP/prefix facts, or unresolved bindings. Split node names, "
        "interface names, and IPv4 into separate fields. interfaceObservations are compact node-local text blocks, "
        "not one JSON object per port: use one block per node, place one visible interface fact per rawText line "
        "in the form '<interface> <IPv4/prefix>' or '<interface>', and keep the candidate arrays aligned with those "
        "lines. For every router, firewall, and switch_l3, inspect the whole area around the device and include all "
        "visible interface/IP facts, including labels not on a link. Clients, servers, and VPC nodes with visible "
        "IP labels also require an interface block. If an interface/IP relationship is uncertain, preserve the "
        "candidate facts rather than dropping an address. Return checkedNodeIds for every node in currentTopology "
        "and set textCoverageComplete only when that coverage was completed. Preserve raw text and ambiguity. Do "
        "not calculate networks or bind ambiguous text. Treat outputLimits as hard limits: textBlocks counts node "
        "and region text together, while interfaces counts compact node-local blocks.\n"
        "Structured request context:\n"
        + json.dumps(request_context, ensure_ascii=False, separators=(",", ":"))
    )
    result = client.call_structured(
        task_text=task_text,
        images=[ModelImage(view_id=view.view_id, image=view.image)],
        response_model=_TextPassResponse,
        skill=SkillName.TOPOLOGY_RECOGNITION,
        allow_repair=False,
        request_stage="text",
        response_validation_context={"textLimits": output_limits.as_dict()},
    )
    response = _text_response_in_view(result.value, view)
    node_refs = {node.ref for node in nodes}
    region_refs = {region.ref for region in regions}
    text_count = (
        len(response.node_text_observations)
        + sum(
            len(_expand_grouped_interface_text_observation(item))
            for item in response.interface_observations
        )
        + len(response.region_text_observations)
    )
    unresolved = _convert_pass_unresolved(response.unresolved_items, view, evidence, "text")
    pending: list[_PendingBinding] = []

    expected_coverage = set(_text_coverage_node_refs(nodes))
    aliases = _all_temp_refs(nodes)
    checked_coverage = set(_resolve_refs(response.checked_node_ids, aliases, node_refs))
    unknown_checked_ids = [
        value
        for value in response.checked_node_ids
        if _resolve_ref(value, aliases, node_refs) is None
    ]
    missing_coverage = sorted(expected_coverage - checked_coverage)
    if (
        checked_coverage != expected_coverage
        or unknown_checked_ids
        or not response.text_coverage_complete
    ):
        missing_ids = [_node_obs_id(nodes, ref) for ref in missing_coverage]
        unresolved.append(
            UnresolvedItem(
                temp_id="unresolved_text_coverage",
                category=UnresolvedCategory.TEXT_COVERAGE_INCOMPLETE,
                object_ids=missing_ids,
                field="checkedNodeIds",
                candidates=_unique_strings([*missing_ids, *unknown_checked_ids]),
                reason="text pass did not confirm complete node coverage",
                blocking=False,
                recommended_action="repeat text coverage in a later observation pass",
                evidence_ids=[],
            )
        )

    for item in response.node_text_observations:
        aliases = _all_temp_refs(nodes)
        candidates = _resolve_refs(item.node_id_candidates, aliases, node_refs)
        unknown = [value for value in item.node_id_candidates if _resolve_ref(value, aliases, node_refs) is None]
        ids = _register_text_evidence(
            evidence,
            view,
            item.evidence,
            raw_text=item.raw_text,
            fallback_bbox=item.label_bbox or item.ip_bbox,
            confidence=item.confidence,
        )
        names, ip_texts, inline_interfaces = _split_node_text_observation(
            item,
            nodes,
        )
        if len(candidates) == 1 and not unknown:
            node = _node_by_ref(nodes, candidates[0])
            merged_names = _unique_strings([*node.observation.name_candidates, *names])
            node.observation = _replace_node(node.observation, raw_name=merged_names[0] if merged_names else node.observation.raw_name, name_candidates=merged_names, evidence_ids=_unique_strings([*node.observation.evidence_ids, *ids]), source_view_ids=_unique_strings([*node.observation.source_view_ids, view.view_id]))
            _attach_text_interface_facts(
                node,
                ip_texts=ip_texts,
                interface_names=inline_interfaces,
                raw_text=item.raw_text,
                label_bbox=item.label_bbox,
                ip_bbox=item.ip_bbox,
                evidence_ids=ids,
                confidence=item.confidence,
                view=view,
                links=links,
                unresolved=unresolved,
                all_nodes=nodes,
            )
        else:
            target_ids = [_node_obs_id(nodes, value) for value in candidates]
            binding_id = f"pending_node_text_{len(pending) + 1:03d}"
            pending.append(
                _PendingBinding(
                    binding_id,
                    "node_text",
                    target_ids,
                    names,
                    item.raw_text,
                    ids,
                    item.confidence,
                    candidate_values=_unique_strings(
                        [binding_id, *target_ids, *unknown, *names]
                    ),
                )
            )
            unresolved.append(
                _make_binding_unresolved(
                    "TEXT_NODE_BINDING_AMBIGUITY",
                    target_ids,
                    item.raw_text or "node text is not uniquely bound",
                    ids,
                    candidates=_unique_strings(
                        [binding_id, *target_ids, *unknown, *names]
                    ),
                    field="rawName",
                )
            )

    expanded_interface_observations: list[
        tuple[_InterfaceTextObservation, list[str]]
    ] = []
    for grouped_item in response.interface_observations:
        group_evidence_ids = _register_text_evidence(
            evidence,
            view,
            grouped_item.evidence,
            raw_text=grouped_item.raw_text,
            fallback_bbox=grouped_item.label_bbox or grouped_item.ip_bbox,
            confidence=grouped_item.confidence,
        )
        expanded_interface_observations.extend(
            (item, group_evidence_ids)
            for item in _expand_grouped_interface_text_observation(grouped_item)
        )

    next_interface = _next_interface_number(nodes)
    for item, ids in expanded_interface_observations:
        aliases = _all_temp_refs(nodes)
        candidates = _resolve_refs(item.node_id_candidates, aliases, node_refs)
        unknown = [value for value in item.node_id_candidates if _resolve_ref(value, aliases, node_refs) is None]
        interface_id = f"obs_if_{next_interface:03d}"
        next_interface += 1
        interface, invalid_values, unknown_link_ids = _make_observed_interface(
            item, view, links, ids, interface_id
        )
        target_ids = [_node_obs_id(nodes, value) for value in candidates]
        interface_values = _interface_candidate_values(interface, invalid_values)
        ip_values = _ip_candidate_values(interface, invalid_values)
        missing_prefix = bool(interface.ip_candidates) and all(
            _is_bare_ipv4(value) for value in interface.ip_candidates
        )
        if len(candidates) == 1 and not unknown:
            node = _node_by_ref(nodes, candidates[0])
            interface = _replace_interface(
                interface,
                node_candidates=[node.observation.observation_id],
            )
            interface = _upsert_node_interface(node, interface)
            node.observation = _replace_node(
                node.observation,
                evidence_ids=_unique_strings([*node.observation.evidence_ids, *ids]),
                source_view_ids=_unique_strings(
                    [*node.observation.source_view_ids, view.view_id]
                ),
            )
            if invalid_values:
                unresolved.append(
                    _make_binding_unresolved(
                        "IP_INTERFACE_BINDING_AMBIGUITY",
                        [interface.observation_id],
                        "preserved unreadable IPv4 or prefix candidate: " + ", ".join(invalid_values),
                        ids,
                        candidates=ip_values,
                        field="ipCandidates",
                    )
                )
            if missing_prefix:
                unresolved.append(
                    _make_binding_unresolved(
                        "UNKNOWN_PREFIX",
                        [interface.observation_id],
                        "IPv4 text has no visible prefix candidate",
                        ids,
                        candidates=ip_values,
                        field="ipCandidates",
                    )
                )
            if not interface.name_candidates and interface.ip_candidates:
                unresolved.append(
                    _make_binding_unresolved(
                        "INTERFACE_NAME_MISSING",
                        [interface.observation_id],
                        "IPv4 was visible but no interface name was visible in the same text observation",
                        ids,
                        candidates=ip_values,
                        field="rawName",
                    )
                )
            if unknown_link_ids:
                unresolved.append(
                    _make_binding_unresolved(
                        "INTERFACE_BINDING_AMBIGUITY",
                        [interface.observation_id],
                        "interface text references an unknown nearby link",
                        ids,
                        candidates=unknown_link_ids,
                        field="nearbyLinkIds",
                    )
                )
            continue
        binding_id = f"pending_interface_{len(pending) + 1:03d}"
        interface = _replace_interface(interface, node_candidates=target_ids)
        pending.append(
            _PendingBinding(
                binding_id,
                "interface",
                target_ids,
                interface.name_candidates,
                item.raw_text,
                ids,
                item.confidence,
                candidate_values=_unique_strings(
                    [
                        binding_id,
                        interface.observation_id,
                        *target_ids,
                        *unknown,
                        *interface_values,
                    ]
                ),
                interface=interface,
            )
        )
        unresolved.append(
            _make_binding_unresolved(
                "INTERFACE_NODE_BINDING_AMBIGUITY",
                [interface.observation_id],
                item.raw_text or "interface text is not uniquely bound",
                ids,
                candidates=_unique_strings(
                    [
                        binding_id,
                        interface.observation_id,
                        *target_ids,
                        *unknown,
                        *interface_values,
                    ]
                ),
                field="observedInterfaces",
            )
        )
        if invalid_values:
            unresolved.append(
                _make_binding_unresolved(
                    "IP_INTERFACE_BINDING_AMBIGUITY",
                    [interface.observation_id],
                    "preserved unreadable IPv4 or prefix candidate: " + ", ".join(invalid_values),
                    ids,
                    candidates=ip_values,
                    field="ipCandidates",
                )
            )
        if missing_prefix:
            unresolved.append(
                _make_binding_unresolved(
                    "UNKNOWN_PREFIX",
                    [interface.observation_id],
                    "IPv4 text has no visible prefix candidate",
                    ids,
                    candidates=ip_values,
                    field="ipCandidates",
                )
            )
        if not interface.name_candidates and interface.ip_candidates:
            unresolved.append(
                _make_binding_unresolved(
                    "INTERFACE_NAME_MISSING",
                    [interface.observation_id],
                    "IPv4 was visible but no interface name was visible in the same text observation",
                    ids,
                    candidates=ip_values,
                    field="rawName",
                )
            )
        if unknown_link_ids:
            unresolved.append(
                _make_binding_unresolved(
                    "INTERFACE_BINDING_AMBIGUITY",
                    [interface.observation_id],
                    "interface text references an unknown nearby link",
                    ids,
                    candidates=unknown_link_ids,
                    field="nearbyLinkIds",
                )
            )

    for item in response.region_text_observations:
        aliases = _all_temp_refs(regions)
        candidates = _resolve_refs(item.region_id_candidates, aliases, region_refs)
        unknown = [value for value in item.region_id_candidates if _resolve_ref(value, aliases, region_refs) is None]
        ids = _register_text_evidence(
            evidence,
            view,
            item.evidence,
            raw_text=item.raw_text,
            fallback_bbox=item.label_bbox,
            confidence=item.confidence,
        )
        names = _unique_strings(
            [*item.name_candidates, *([item.raw_text] if item.raw_text else [])]
        )
        if len(candidates) == 1 and not unknown:
            region = _region_by_ref(regions, candidates[0])
            merged_names = _unique_strings([*region.observation.name_candidates, *names])
            region.observation = _replace_region(region.observation, raw_name=merged_names[0] if merged_names else region.observation.raw_name, name_candidates=merged_names, evidence_ids=_unique_strings([*region.observation.evidence_ids, *ids]))
        else:
            target_ids = [_region_by_ref(regions, value).observation.observation_id for value in candidates]
            binding_id = f"pending_region_text_{len(pending) + 1:03d}"
            pending.append(
                _PendingBinding(
                    binding_id,
                    "region_text",
                    target_ids,
                    names,
                    item.raw_text,
                    ids,
                    item.confidence,
                    candidate_values=_unique_strings(
                        [binding_id, *target_ids, *unknown, *names]
                    ),
                )
            )
            unresolved.append(
                _make_binding_unresolved(
                    "REGION_MEMBERSHIP_AMBIGUITY",
                    target_ids,
                    item.raw_text or "region title is not uniquely bound",
                    ids,
                    candidates=_unique_strings(
                        [binding_id, *target_ids, *unknown, *names]
                    ),
                    field="rawName",
                )
            )
    _record_stage_usage(client, "text", result.usage)
    return text_count, unresolved, pending


def _run_fusion_pass(
    task_id: str,
    bundle: ImageBundle,
    client: OpenAICompatibleModelClient,
    nodes: list[_NodeState],
    regions: list[_RegionState],
    links: list[_LinkState],
    evidence: dict[str, Evidence],
    conflicts: list[_Conflict],
    unresolved: list[UnresolvedItem],
    pending_bindings: Sequence[_PendingBinding],
    artifacts: _RecognitionRunArtifacts,
) -> Any:
    payload = {
        "taskId": task_id,
        "imageWidth": bundle.image_info.width,
        "imageHeight": bundle.image_info.height,
        "nodes": [_fusion_node(node) for node in nodes],
        "interfaces": [
            *[
                _fusion_interface(node, interface)
                for node in nodes
                for interface in node.observation.observed_interfaces
            ],
            *[
                _fusion_pending_binding(item)
                for item in pending_bindings
                if item.kind == "interface"
            ],
        ],
        "links": [_fusion_link(link) for link in links],
        "regions": [_fusion_region(region) for region in regions],
        "conflicts": [
            _fusion_conflict(conflict, evidence) for conflict in conflicts
        ],
        "unresolvedItems": [item.model_dump(by_alias=True) for item in unresolved],
        "pendingBindings": [
            _fusion_pending_binding(item) for item in pending_bindings
        ],
    }
    artifacts.write_stage_context("fusion", payload)
    task_text = (
        "stage: fusion\n"
        "This is a text-only semantic fusion pass over three completed visual observations. "
        "Do not re-identify an image. Only cite conflictId and candidateIndex values present in the input. "
        "Do not create visual facts, objects, IDs, coordinates, polylines, Evidence, network calculations, "
        "or platform fields. NAME_PLUS_IP, normalization differences, and containment are not name conflicts; "
        "an interface that was materialized from a link label is not a node endpoint ambiguity. Do not create "
        "new interfaces or bindings. Preserve ambiguity when evidence is insufficient. Return only the JSON patch schema.\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )
    result = client.call_structured(
        task_text=task_text,
        images=(),
        response_model=_FusionPassResponse,
        skill=SkillName.TOPOLOGY_RECOGNITION,
        allow_repair=False,
        allow_empty_images=True,
        request_stage="fusion",
    )
    _record_stage_usage(client, "fusion", result.usage)
    return result


def _build_conflicts(
    nodes: list[_NodeState],
    regions: list[_RegionState],
    links: list[_LinkState],
    evidence: dict[str, Evidence],
    unresolved: Sequence[UnresolvedItem],
    pending_bindings: Sequence[_PendingBinding],
) -> list[_Conflict]:
    conflicts: list[_Conflict] = []

    def add(
        kind: _ConflictType,
        targets: list[str],
        values: list[Any],
        source: str,
        view_id: str,
        confidence: float = 0.5,
        evidence_ids: Sequence[str] = (),
        *,
        field_name: str | None = None,
        spatial_context: dict[str, Any] | None = None,
    ) -> _Conflict | None:
        unique_values = _unique_option_values(values)
        if len(unique_values) < 2:
            return None
        signature = _conflict_signature(kind, targets, unique_values, field_name)
        if any(
            _conflict_signature(
                item.conflict_type,
                item.target_ids,
                [option.value for option in item.candidate_options],
                item.field_name,
            )
            == signature
            for item in conflicts
        ):
            return None
        conflict_id = f"conflict_{len(conflicts) + 1:03d}"
        options = [
            _candidate_option(
                value,
                source,
                view_id,
                confidence,
                evidence_ids,
                evidence,
            )
            for value in unique_values
        ]
        conflict = _Conflict(
            conflict_id,
            kind,
            _unique_strings(targets),
            options,
            _unique_strings(evidence_ids),
            spatial_context or {},
            field_name=field_name,
        )
        conflicts.append(conflict)
        return conflict

    for node in nodes:
        node_context = {
            "bbox": node.observation.bbox.model_dump(by_alias=True),
            "center": node.observation.center.model_dump(by_alias=True),
        }
        if len(node.observation.type_candidates) > 1:
            add(_ConflictType.NODE_TYPE_CONFLICT, [node.observation.observation_id], [item.value for item in node.observation.type_candidates], "structure", node.observation.source_view_ids[0], node.observation.confidence, node.observation.evidence_ids, field_name="semanticType", spatial_context=node_context)
        name_conflict_values = _name_conflict_values(node.observation.name_candidates)
        if len(name_conflict_values) > 1:
            add(_ConflictType.NODE_NAME_CONFLICT, [node.observation.observation_id], name_conflict_values, "mixed", node.observation.source_view_ids[-1], node.observation.confidence, node.observation.evidence_ids, field_name="rawName", spatial_context=node_context)
        if len(node.observation.region_candidates) > 1:
            add(_ConflictType.REGION_MEMBERSHIP_AMBIGUITY, [node.observation.observation_id], node.observation.region_candidates, "structure", "global_structure", node.observation.confidence, node.observation.evidence_ids, field_name="regionCandidates", spatial_context=node_context)
        for interface in node.observation.observed_interfaces:
            interface_context = {
                "nodeId": node.observation.observation_id,
                "labelBBox": interface.label_bbox.model_dump(by_alias=True) if interface.label_bbox else None,
                "ipBBox": interface.ip_bbox.model_dump(by_alias=True) if interface.ip_bbox else None,
            }
            if len(interface.ip_candidates) > 1:
                add(_ConflictType.IP_INTERFACE_BINDING_AMBIGUITY, [interface.observation_id], [str(item) for item in interface.ip_candidates], "text", "global_text", interface.confidence, interface.evidence_ids, field_name="ipCandidates", spatial_context=interface_context)
            if len(interface.name_candidates) > 1:
                add(_ConflictType.EVIDENCE_CONFLICT, [interface.observation_id], interface.name_candidates, "text", "global_text", interface.confidence, interface.evidence_ids, field_name="rawName", spatial_context=interface_context)

    for region in regions:
        if len(region.observation.name_candidates) > 1:
            add(_ConflictType.EVIDENCE_CONFLICT, [region.observation.observation_id], region.observation.name_candidates, "mixed", "global_text", region.observation.confidence, region.observation.evidence_ids, field_name="rawName", spatial_context={"bbox": region.observation.bbox.model_dump(by_alias=True)})

    for link in links:
        link_context = {
            "polyline": [point.model_dump(by_alias=True) for point in link.observation.polyline],
        }
        for field_name, candidates in (
            ("sourceNodeCandidates", link.observation.source_node_candidates),
            ("targetNodeCandidates", link.observation.target_node_candidates),
            ("sourceInterfaceCandidates", link.observation.source_interface_candidates),
            ("targetInterfaceCandidates", link.observation.target_interface_candidates),
        ):
            if len(candidates) > 1:
                conflict_type = (
                    _ConflictType.LINK_ENDPOINT_AMBIGUITY
                    if field_name.endswith("NodeCandidates")
                    else _ConflictType.INTERFACE_BINDING_AMBIGUITY
                )
                add(conflict_type, [link.observation.observation_id], list(candidates), "links", "global_links", link.observation.confidence, link.observation.evidence_ids, field_name=field_name, spatial_context={**link_context, "endpointField": field_name})
        if link.observation.crossing_uncertain:
            add(_ConflictType.CROSSING_OR_CONNECTION_AMBIGUITY, [link.observation.observation_id], ["connected", "crossing"], "links", "global_links", link.observation.confidence, link.observation.evidence_ids, field_name="crossingUncertain", spatial_context=link_context)

    unresolved_type_map = {
        UnresolvedCategory.AMBIGUOUS_NODE_NAME: _ConflictType.NODE_NAME_CONFLICT,
        UnresolvedCategory.NODE_NAME_CONFLICT: _ConflictType.NODE_NAME_CONFLICT,
        UnresolvedCategory.AMBIGUOUS_DEVICE_TYPE: _ConflictType.NODE_TYPE_CONFLICT,
        UnresolvedCategory.NODE_TYPE_CONFLICT: _ConflictType.NODE_TYPE_CONFLICT,
        UnresolvedCategory.AMBIGUOUS_INTERFACE_NAME: _ConflictType.EVIDENCE_CONFLICT,
        UnresolvedCategory.EVIDENCE_CONFLICT: _ConflictType.EVIDENCE_CONFLICT,
        UnresolvedCategory.AMBIGUOUS_IP: _ConflictType.IP_INTERFACE_BINDING_AMBIGUITY,
        UnresolvedCategory.IP_INTERFACE_BINDING_AMBIGUITY: _ConflictType.IP_INTERFACE_BINDING_AMBIGUITY,
        UnresolvedCategory.AMBIGUOUS_LINK_ENDPOINT: _ConflictType.LINK_ENDPOINT_AMBIGUITY,
        UnresolvedCategory.LINK_ENDPOINT_AMBIGUITY: _ConflictType.LINK_ENDPOINT_AMBIGUITY,
        UnresolvedCategory.CROSSING_UNCERTAIN: _ConflictType.CROSSING_OR_CONNECTION_AMBIGUITY,
        UnresolvedCategory.CROSSING_OR_CONNECTION_AMBIGUITY: _ConflictType.CROSSING_OR_CONNECTION_AMBIGUITY,
        UnresolvedCategory.NODE_DUPLICATION_AMBIGUITY: _ConflictType.NODE_DUPLICATION_AMBIGUITY,
        UnresolvedCategory.TEXT_NODE_BINDING_AMBIGUITY: _ConflictType.TEXT_NODE_BINDING_AMBIGUITY,
        UnresolvedCategory.INTERFACE_NODE_BINDING_AMBIGUITY: _ConflictType.INTERFACE_NODE_BINDING_AMBIGUITY,
        UnresolvedCategory.INTERFACE_BINDING_AMBIGUITY: _ConflictType.INTERFACE_BINDING_AMBIGUITY,
        UnresolvedCategory.INTERFACE_IP_MISSING: _ConflictType.INTERFACE_IP_MISSING,
        UnresolvedCategory.INTERFACE_NAME_MISSING: _ConflictType.INTERFACE_NAME_MISSING,
        UnresolvedCategory.LINK_GEOMETRY_INCONSISTENT: _ConflictType.LINK_GEOMETRY_INCONSISTENT,
        UnresolvedCategory.TEXT_COVERAGE_INCOMPLETE: _ConflictType.TEXT_COVERAGE_INCOMPLETE,
        UnresolvedCategory.REGION_MEMBERSHIP_AMBIGUITY: _ConflictType.REGION_MEMBERSHIP_AMBIGUITY,
    }
    pending_ids = {item.binding_id for item in pending_bindings}
    for item in unresolved:
        if pending_ids.intersection(item.candidates):
            continue
        kind = unresolved_type_map.get(item.category)
        values = _unique_strings(item.candidates)
        if kind is None or len(values) < 2:
            continue
        supporting_items = [
            evidence[evidence_id]
            for evidence_id in item.evidence_ids
            if evidence_id in evidence
        ]
        source_view_id = next(
            (
                value.source_view_id
                for value in supporting_items
                if value.source_view_id
                in {"global_structure", "global_links", "global_text"}
            ),
            _default_view_for_conflict(kind),
        )
        confidence = max(
            (value.confidence for value in supporting_items), default=0.5
        )
        add(
            kind,
            item.object_ids,
            values,
            _stage_for_view(source_view_id),
            source_view_id,
            confidence,
            item.evidence_ids,
            field_name=item.field,
            spatial_context={
                "unresolvedId": item.temp_id,
                "field": item.field,
                "evidenceBBoxes": [
                    value.bbox.model_dump(by_alias=True)
                    for value in supporting_items
                    if value.bbox is not None
                ],
            },
        )

    pending_type_map = {
        "node_text": _ConflictType.TEXT_NODE_BINDING_AMBIGUITY,
        "interface": _ConflictType.INTERFACE_NODE_BINDING_AMBIGUITY,
        "region_text": _ConflictType.REGION_MEMBERSHIP_AMBIGUITY,
    }
    for item in pending_bindings:
        kind = pending_type_map[item.kind]
        pending_conflict = add(
            kind,
            [item.binding_id],
            item.target_ids,
            "text",
            "global_text",
            item.confidence,
            item.evidence_ids,
            field_name="targetCandidates",
            spatial_context=_pending_spatial_context(item),
        )
        if pending_conflict is not None:
            pending_conflict.pending_binding = item

    for left_index, left in enumerate(nodes):
        for right in nodes[left_index + 1 :]:
            if _bbox_iou(left.observation.bbox, right.observation.bbox) > 0.15:
                add(_ConflictType.NODE_DUPLICATION_AMBIGUITY, [left.observation.observation_id, right.observation.observation_id], [left.observation.observation_id, right.observation.observation_id], "links", "global_links", min(left.observation.confidence, right.observation.confidence), [*left.observation.evidence_ids, *right.observation.evidence_ids], field_name="objects", spatial_context={"leftBBox": left.observation.bbox.model_dump(by_alias=True), "rightBBox": right.observation.bbox.model_dump(by_alias=True)})

    return conflicts


def _apply_fusion_decisions(
    decisions: Sequence[_FusionDecision],
    conflicts: list[_Conflict],
    nodes: list[_NodeState],
    regions: list[_RegionState],
    links: list[_LinkState],
    unresolved: list[UnresolvedItem],
    pending_bindings: Sequence[_PendingBinding],
) -> int:
    conflict_map = {conflict.conflict_id: conflict for conflict in conflicts}
    rejected = 0
    decision_counts: dict[str, int] = {}
    for decision in decisions:
        decision_counts[decision.conflict_id] = (
            decision_counts.get(decision.conflict_id, 0) + 1
        )
    duplicate_conflict_ids = {
        conflict_id for conflict_id, count in decision_counts.items() if count > 1
    }
    for conflict_id in sorted(duplicate_conflict_ids):
        rejected += decision_counts[conflict_id]
        conflict = conflict_map.get(conflict_id)
        unresolved.append(
            _conflict_unresolved(
                conflict, "fusion returned multiple decisions for one conflict"
            )
            if conflict is not None
            else _invalid_fusion_unresolved(conflict_id)
        )

    ordered_decisions = sorted(
        enumerate(decisions),
        key=lambda item: (item[1].action == "MERGE_OBJECTS", item[0]),
    )
    for _, decision in ordered_decisions:
        if decision.conflict_id in duplicate_conflict_ids:
            continue
        conflict = conflict_map.get(decision.conflict_id)
        reason = decision.reason_code.strip() if isinstance(decision.reason_code, str) else "invalid reason"
        selection_action = decision.action in {"SELECT_CANDIDATE", "BIND_REFERENCE"}
        valid = (
            conflict is not None
            and bool(reason)
            and _action_allowed(conflict, decision.action)
            and (
                len(decision.selected_candidate_indexes) == 1
                if selection_action
                else True
            )
            and len(decision.selected_candidate_indexes)
            == len(set(decision.selected_candidate_indexes))
            and all(
                0 <= index < len(conflict.candidate_options)
                for index in decision.selected_candidate_indexes
            )
            and (
                conflict.pending_binding is None
                or conflict.pending_binding in pending_bindings
            )
        )
        if not valid:
            rejected += 1
            if conflict is not None:
                unresolved.append(_conflict_unresolved(conflict, "fusion decision rejected"))
            else:
                unresolved.append(_invalid_fusion_unresolved(decision.conflict_id))
            continue
        assert conflict is not None
        action = decision.action
        if action in {"SELECT_CANDIDATE", "BIND_REFERENCE"}:
            selected_index = decision.selected_candidate_indexes[0]
            selected_value = str(
                conflict.candidate_options[selected_index].value
            )
            if decision.confidence < 0.60:
                unresolved.append(
                    _conflict_unresolved(
                        conflict,
                        "selection confidence is below the candidate-ordering threshold",
                    )
                )
                continue
            pending_interface_id = (
                conflict.pending_binding.interface.observation_id
                if (
                    conflict.pending_binding is not None
                    and conflict.pending_binding.kind == "interface"
                    and conflict.pending_binding.interface is not None
                )
                else None
            )
            try:
                applied = (
                    _apply_selection(conflict, selected_index, nodes, regions, links)
                    if decision.confidence >= 0.85
                    else _reorder_selection(
                        conflict, selected_index, nodes, regions, links
                    )
                )
            except (ModelInvocationError, ValueError):
                applied = False
            if (
                applied
                and pending_interface_id is not None
                and conflict.pending_binding is not None
                and conflict.pending_binding.interface is not None
                and pending_interface_id
                != conflict.pending_binding.interface.observation_id
            ):
                _replace_unresolved_reference(
                    unresolved,
                    pending_interface_id,
                    conflict.pending_binding.interface.observation_id,
                )
            if not applied:
                rejected += 1
                unresolved.append(_conflict_unresolved(conflict, "fusion selection could not be applied safely"))
            elif decision.confidence < 0.85:
                unresolved.append(_conflict_unresolved(conflict, "selection confidence is below the unique-decision threshold"))
            elif (
                conflict.conflict_type
                is _ConflictType.CROSSING_OR_CONNECTION_AMBIGUITY
                and selected_value == "crossing"
            ):
                unresolved.append(
                    _conflict_unresolved(
                        conflict,
                        "a confirmed visual crossing is not a confirmed link connection",
                    )
                )
            else:
                conflict.resolved = True
                _remove_resolved_conflict_unresolved(unresolved, conflict)
        elif action == "MERGE_OBJECTS":
            if decision.confidence < 0.85 or not _merge_nodes_if_allowed(conflict, nodes, links, regions, unresolved):
                unresolved.append(_conflict_unresolved(conflict, "merge was not sufficiently supported"))
            else:
                conflict.resolved = True
                _remove_resolved_conflict_unresolved(unresolved, conflict)
        elif action == "KEEP_OBJECTS_SEPARATE":
            if decision.confidence < 0.85:
                unresolved.append(_conflict_unresolved(conflict, "separation confidence is below the unique-decision threshold"))
            else:
                conflict.resolved = True
                _remove_resolved_conflict_unresolved(unresolved, conflict)
        elif action in {"KEEP_MULTIPLE_CANDIDATES", "LEAVE_UNRESOLVED"}:
            unresolved.append(_conflict_unresolved(conflict, f"fusion action {action} preserves ambiguity"))
        else:
            rejected += 1
            unresolved.append(_conflict_unresolved(conflict, "unsupported fusion action"))
    return rejected


def _action_allowed(conflict: _Conflict, action: str) -> bool:
    if action == "MERGE_OBJECTS":
        return conflict.conflict_type is _ConflictType.NODE_DUPLICATION_AMBIGUITY
    if action == "KEEP_OBJECTS_SEPARATE":
        return conflict.conflict_type is _ConflictType.NODE_DUPLICATION_AMBIGUITY
    if action == "BIND_REFERENCE":
        return conflict.conflict_type in {
            _ConflictType.TEXT_NODE_BINDING_AMBIGUITY,
            _ConflictType.INTERFACE_NODE_BINDING_AMBIGUITY,
            _ConflictType.IP_INTERFACE_BINDING_AMBIGUITY,
            _ConflictType.LINK_ENDPOINT_AMBIGUITY,
            _ConflictType.INTERFACE_BINDING_AMBIGUITY,
            _ConflictType.REGION_MEMBERSHIP_AMBIGUITY,
        }
    if action == "SELECT_CANDIDATE":
        return conflict.conflict_type is not _ConflictType.NODE_DUPLICATION_AMBIGUITY
    return action in {"KEEP_MULTIPLE_CANDIDATES", "LEAVE_UNRESOLVED"}


def _remove_resolved_conflict_unresolved(
    unresolved: list[UnresolvedItem], conflict: _Conflict
) -> None:
    categories = {_unresolved_category(conflict.conflict_type.value)}
    pending = conflict.pending_binding
    target_ids = set(conflict.target_ids)
    conflict_candidates = {
        str(option.value) for option in conflict.candidate_options
    }
    if pending is not None:
        categories.add(
            {
                "node_text": UnresolvedCategory.TEXT_NODE_BINDING_AMBIGUITY,
                "interface": UnresolvedCategory.INTERFACE_NODE_BINDING_AMBIGUITY,
                "region_text": UnresolvedCategory.REGION_MEMBERSHIP_AMBIGUITY,
            }[pending.kind]
        )
        target_ids.update(pending.target_ids)
    if conflict.conflict_type is _ConflictType.LINK_ENDPOINT_AMBIGUITY:
        categories.add(UnresolvedCategory.AMBIGUOUS_LINK_ENDPOINT)
    if pending is not None:
        unresolved[:] = [
            item
            for item in unresolved
            if not (
                item.category in categories
                and pending.binding_id in item.candidates
            )
        ]
        return
    unresolved[:] = [
        item
        for item in unresolved
        if not (
            item.category in categories
            and (not target_ids or target_ids.intersection(item.object_ids))
            and (
                conflict.field_name is None
                or item.field is None
                or item.field == conflict.field_name
            )
            and (
                not item.candidates
                or set(item.candidates).issubset(conflict_candidates)
            )
        )
    ]


def _replace_unresolved_reference(
    unresolved: list[UnresolvedItem], old: str, new: str
) -> None:
    unresolved[:] = [
        UnresolvedItem(
            temp_id=item.temp_id,
            category=item.category,
            object_ids=_replace_ref(item.object_ids, old, new),
            field=item.field,
            candidates=_replace_ref(item.candidates, old, new),
            reason=item.reason,
            blocking=item.blocking,
            recommended_action=item.recommended_action,
            evidence_ids=item.evidence_ids,
        )
        for item in unresolved
    ]


def _apply_selection(
    conflict: _Conflict,
    selected_index: int,
    nodes: list[_NodeState],
    regions: list[_RegionState],
    links: list[_LinkState],
) -> bool:
    selected = conflict.candidate_options[selected_index].value
    target = conflict.target_ids[0] if conflict.target_ids else ""
    pending = conflict.pending_binding
    if pending is not None:
        if str(selected) not in pending.target_ids:
            return False
        if pending.kind == "node_text":
            node = _node_by_observation_id(nodes, str(selected))
            names = _unique_strings([*pending.names, *node.observation.name_candidates])
            node.observation = _replace_node(node.observation, raw_name=names[0] if names else node.observation.raw_name, name_candidates=names, evidence_ids=_unique_strings([*node.observation.evidence_ids, *pending.evidence_ids]), source_view_ids=_unique_strings([*node.observation.source_view_ids, "global_text"]))
        elif pending.kind == "interface" and pending.interface is not None:
            node = _node_by_observation_id(nodes, str(selected))
            original_interface_id = pending.interface.observation_id
            stored_interface = _upsert_node_interface(
                node,
                _replace_interface(
                    pending.interface,
                    node_candidates=[node.observation.observation_id],
                ),
            )
            pending.interface = stored_interface
            if stored_interface.observation_id != original_interface_id:
                for link in links:
                    link.observation = _replace_link(
                        link.observation,
                        source_interface_candidates=_replace_ref(
                            link.observation.source_interface_candidates,
                            original_interface_id,
                            stored_interface.observation_id,
                        ),
                        target_interface_candidates=_replace_ref(
                            link.observation.target_interface_candidates,
                            original_interface_id,
                            stored_interface.observation_id,
                        ),
                    )
            node.observation = _replace_node(
                node.observation,
                evidence_ids=_unique_strings(
                    [*node.observation.evidence_ids, *pending.evidence_ids]
                ),
                source_view_ids=_unique_strings(
                    [*node.observation.source_view_ids, "global_text"]
                ),
            )
        elif pending.kind == "region_text":
            region = _region_by_observation_id(regions, str(selected))
            names = _unique_strings([*pending.names, *region.observation.name_candidates])
            region.observation = _replace_region(region.observation, raw_name=names[0] if names else region.observation.raw_name, name_candidates=names, evidence_ids=_unique_strings([*region.observation.evidence_ids, *pending.evidence_ids]))
        else:
            return False
        pending.resolved = True
        return True
    if conflict.conflict_type is _ConflictType.NODE_TYPE_CONFLICT:
        node = _node_by_observation_id(nodes, target)
        try:
            selected_type = SemanticDeviceType(str(selected))
        except ValueError:
            return False
        node.observation = _replace_node(node.observation, semantic_type=selected_type, type_candidates=_unique_types([selected_type, *node.observation.type_candidates]))
        return True
    elif conflict.conflict_type is _ConflictType.NODE_NAME_CONFLICT:
        node = _node_by_observation_id(nodes, target)
        node.observation = _replace_node(node.observation, raw_name=str(selected), name_candidates=_unique_strings([str(selected), *node.observation.name_candidates]))
        return True
    elif conflict.conflict_type is _ConflictType.REGION_MEMBERSHIP_AMBIGUITY:
        node = _node_by_observation_id(nodes, target)
        values = _unique_strings([str(selected), *node.observation.region_candidates])
        node.observation = _replace_node(node.observation, region_candidates=values)
        return True
    elif conflict.conflict_type in {
        _ConflictType.LINK_ENDPOINT_AMBIGUITY,
        _ConflictType.INTERFACE_BINDING_AMBIGUITY,
    }:
        link = _link_by_id(links, target)
        return _reorder_link_field(link, conflict.field_name, str(selected))
    elif conflict.conflict_type is _ConflictType.IP_INTERFACE_BINDING_AMBIGUITY:
        parent, interface = _interface_by_id(nodes, target)
        selected_text = str(selected)
        values = [item for item in interface.ip_candidates if str(item) == selected_text]
        if not values:
            return False
        reordered = _unique_ip_values([*values, *interface.ip_candidates])
        updated = _replace_interface(interface, ip_candidates=reordered)
        parent.observation = _replace_node(parent.observation, observed_interfaces=[updated if item.observation_id == interface.observation_id else item for item in parent.observation.observed_interfaces])
        return True
    elif conflict.conflict_type is _ConflictType.EVIDENCE_CONFLICT:
        interface_match = _optional_interface_by_id(nodes, target)
        if interface_match is not None:
            parent, interface = interface_match
            updated = _replace_interface(interface, raw_name=str(selected), name_candidates=_unique_strings([str(selected), *interface.name_candidates]))
            parent.observation = _replace_node(parent.observation, observed_interfaces=[updated if item.observation_id == interface.observation_id else item for item in parent.observation.observed_interfaces])
            return True
        region = next((item for item in regions if item.observation.observation_id == target), None)
        if region is None:
            return False
        region.observation = _replace_region(region.observation, raw_name=str(selected), name_candidates=_unique_strings([str(selected), *region.observation.name_candidates]))
        return True
    elif conflict.conflict_type is _ConflictType.CROSSING_OR_CONNECTION_AMBIGUITY:
        link = _link_by_id(links, target)
        if str(selected) not in {"connected", "crossing"}:
            return False
        link.observation = _replace_link(link.observation, crossing_uncertain=str(selected) == "crossing")
        return True
    return False


def _reorder_selection(
    conflict: _Conflict,
    selected_index: int,
    nodes: list[_NodeState],
    regions: list[_RegionState],
    links: list[_LinkState],
) -> bool:
    selected = str(conflict.candidate_options[selected_index].value)
    target = conflict.target_ids[0] if conflict.target_ids else ""
    if conflict.pending_binding is not None:
        if selected not in conflict.pending_binding.target_ids:
            return False
        conflict.pending_binding.target_ids = _unique_strings(
            [selected, *conflict.pending_binding.target_ids]
        )
        _reorder_conflict_options(conflict, selected_index)
        return True
    if conflict.conflict_type is _ConflictType.NODE_TYPE_CONFLICT:
        node = _node_by_observation_id(nodes, target)
        try:
            selected_type = SemanticDeviceType(selected)
        except ValueError:
            return False
        node.observation = _replace_node(
            node.observation,
            type_candidates=_unique_types([selected_type, *node.observation.type_candidates]),
        )
    elif conflict.conflict_type is _ConflictType.NODE_NAME_CONFLICT:
        node = _node_by_observation_id(nodes, target)
        node.observation = _replace_node(
            node.observation,
            name_candidates=_unique_strings([selected, *node.observation.name_candidates]),
        )
    elif conflict.conflict_type is _ConflictType.REGION_MEMBERSHIP_AMBIGUITY:
        node = _node_by_observation_id(nodes, target)
        node.observation = _replace_node(
            node.observation,
            region_candidates=_unique_strings([selected, *node.observation.region_candidates]),
        )
    elif conflict.conflict_type in {
        _ConflictType.LINK_ENDPOINT_AMBIGUITY,
        _ConflictType.INTERFACE_BINDING_AMBIGUITY,
    }:
        if not _reorder_link_field(_link_by_id(links, target), conflict.field_name, selected):
            return False
    elif conflict.conflict_type is _ConflictType.IP_INTERFACE_BINDING_AMBIGUITY:
        parent, interface = _interface_by_id(nodes, target)
        values = [item for item in interface.ip_candidates if str(item) == selected]
        if not values:
            return False
        updated = _replace_interface(
            interface, ip_candidates=_unique_ip_values([*values, *interface.ip_candidates])
        )
        parent.observation = _replace_node(
            parent.observation,
            observed_interfaces=[
                updated if item.observation_id == interface.observation_id else item
                for item in parent.observation.observed_interfaces
            ],
        )
    elif conflict.conflict_type is _ConflictType.EVIDENCE_CONFLICT:
        interface_match = _optional_interface_by_id(nodes, target)
        if interface_match is not None:
            parent, interface = interface_match
            updated = _replace_interface(
                interface,
                name_candidates=_unique_strings([selected, *interface.name_candidates]),
            )
            parent.observation = _replace_node(
                parent.observation,
                observed_interfaces=[
                    updated if item.observation_id == interface.observation_id else item
                    for item in parent.observation.observed_interfaces
                ],
            )
        else:
            region = next(
                (item for item in regions if item.observation.observation_id == target), None
            )
            if region is None:
                return False
            region.observation = _replace_region(
                region.observation,
                name_candidates=_unique_strings([selected, *region.observation.name_candidates]),
            )
    elif conflict.conflict_type is _ConflictType.CROSSING_OR_CONNECTION_AMBIGUITY:
        if selected not in {"connected", "crossing"}:
            return False
    else:
        return False
    _reorder_conflict_options(conflict, selected_index)
    return True


def _reorder_conflict_options(conflict: _Conflict, selected_index: int) -> None:
    option = conflict.candidate_options.pop(selected_index)
    conflict.candidate_options.insert(0, option)


def _reorder_link_field(link: _LinkState, field_name: str | None, selected: str) -> bool:
    if field_name == "sourceNodeCandidates" and selected in link.observation.source_node_candidates:
        link.observation = _replace_link(
            link.observation,
            source_node_candidates=_unique_strings([selected, *link.observation.source_node_candidates]),
        )
        return True
    if field_name == "targetNodeCandidates" and selected in link.observation.target_node_candidates:
        link.observation = _replace_link(
            link.observation,
            target_node_candidates=_unique_strings([selected, *link.observation.target_node_candidates]),
        )
        return True
    if field_name == "sourceInterfaceCandidates" and selected in link.observation.source_interface_candidates:
        link.observation = _replace_link(
            link.observation,
            source_interface_candidates=_unique_strings([selected, *link.observation.source_interface_candidates]),
        )
        return True
    if field_name == "targetInterfaceCandidates" and selected in link.observation.target_interface_candidates:
        link.observation = _replace_link(
            link.observation,
            target_interface_candidates=_unique_strings([selected, *link.observation.target_interface_candidates]),
        )
        return True
    return False


def _merge_nodes_if_allowed(
    conflict: _Conflict,
    nodes: list[_NodeState],
    links: list[_LinkState],
    regions: list[_RegionState],
    unresolved: list[UnresolvedItem],
) -> bool:
    if conflict.conflict_type is not _ConflictType.NODE_DUPLICATION_AMBIGUITY or len(conflict.target_ids) < 2:
        return False
    first = next((node for node in nodes if node.observation.observation_id == conflict.target_ids[0]), None)
    second = next((node for node in nodes if node.observation.observation_id == conflict.target_ids[1]), None)
    if first is None or second is None:
        return False
    semantic_type = first.observation.semantic_type
    if semantic_type is SemanticDeviceType.UNKNOWN:
        semantic_type = second.observation.semantic_type
    first.observation = _replace_node(
        first.observation,
        raw_name=first.observation.raw_name or second.observation.raw_name,
        name_candidates=_unique_strings(
            [*first.observation.name_candidates, *second.observation.name_candidates]
        ),
        semantic_type=semantic_type,
        type_candidates=_unique_types(
            [*first.observation.type_candidates, *second.observation.type_candidates]
        ),
        evidence_ids=_unique_strings(
            [*first.observation.evidence_ids, *second.observation.evidence_ids]
        ),
        observed_interfaces=[
            *first.observation.observed_interfaces,
            *second.observation.observed_interfaces,
        ],
        region_candidates=_unique_strings(
            [*first.observation.region_candidates, *second.observation.region_candidates]
        ),
        confidence=max(first.observation.confidence, second.observation.confidence),
        source_view_ids=_unique_strings(
            [*first.observation.source_view_ids, *second.observation.source_view_ids]
        ),
    )
    first.temporary_ids.update(second.temporary_ids)
    first.temporary_ids.add(second.observation.observation_id)
    for link in links:
        link.observation = _replace_link(link.observation, source_node_candidates=_replace_ref(link.observation.source_node_candidates, second.observation.observation_id, first.observation.observation_id), target_node_candidates=_replace_ref(link.observation.target_node_candidates, second.observation.observation_id, first.observation.observation_id))
    for region in regions:
        region.observation = _replace_region(region.observation, member_node_candidates=_replace_ref(region.observation.member_node_candidates, second.observation.observation_id, first.observation.observation_id))
    unresolved[:] = [
        UnresolvedItem(
            temp_id=item.temp_id,
            category=item.category,
            object_ids=_replace_ref(
                item.object_ids,
                second.observation.observation_id,
                first.observation.observation_id,
            ),
            field=item.field,
            candidates=_replace_ref(
                item.candidates,
                second.observation.observation_id,
                first.observation.observation_id,
            ),
            reason=item.reason,
            blocking=item.blocking,
            recommended_action=item.recommended_action,
            evidence_ids=item.evidence_ids,
        )
        for item in unresolved
    ]
    nodes.remove(second)
    return True


def _unresolved_for_remaining_conflicts(
    conflicts: Sequence[_Conflict],
) -> list[UnresolvedItem]:
    return [
        _conflict_unresolved(conflict, "no high-confidence fusion decision")
        for conflict in conflicts
        if not conflict.resolved
    ]


def _build_observation(
    task_id: str,
    bundle: ImageBundle,
    nodes: list[_NodeState],
    regions: list[_RegionState],
    links: list[_LinkState],
    evidence: dict[str, Evidence],
    unresolved: list[UnresolvedItem],
    pending_bindings: Sequence[_PendingBinding] = (),
) -> TopologyObservation:
    node_values = [node.observation for node in sorted(nodes, key=lambda item: item.observation.observation_id)]
    interface_by_id: dict[str, ObservedInterface] = {
        interface.observation_id: interface
        for node in node_values
        for interface in node.observed_interfaces
    }
    for pending in pending_bindings:
        if pending.interface is not None and not pending.resolved:
            interface_by_id.setdefault(pending.interface.observation_id, pending.interface)
    interface_values = [interface_by_id[key] for key in sorted(interface_by_id)]
    link_values = [link.observation for link in sorted(links, key=lambda item: item.observation.observation_id)]
    region_values = [region.observation for region in sorted(regions, key=lambda item: item.observation.observation_id)]
    evidence_values = [evidence[key] for key in sorted(evidence)]
    unresolved_values = _dedupe_unresolved(
        _normalize_unresolved_references(
            unresolved,
            nodes,
            regions,
            links,
            evidence,
            extra_interfaces=interface_values,
        )
    )
    interface_count = len(interface_values)
    blocking_count = sum(item.blocking for item in unresolved_values)
    observation = TopologyObservation(
        task_id=task_id,
        image=bundle.image_info,
        observed_nodes=node_values,
        observed_interfaces=interface_values,
        observed_links=link_values,
        observed_regions=region_values,
        evidence=evidence_values,
        unresolved_items=unresolved_values,
        summary={
            "nodeCount": len(node_values),
            "interfaceCount": len(interface_values),
            "linkCount": len(link_values),
            "regionCount": len(region_values),
            "evidenceCount": len(evidence_values),
            "unresolvedCount": len(unresolved_values),
            "blockingUnresolvedCount": blocking_count,
            "nodes": len(node_values),
            "interfaces": len(interface_values),
            "links": len(link_values),
            "regions": len(region_values),
            "evidence": len(evidence_values),
            "unresolvedItems": len(unresolved_values),
        },
    )
    _validate_final_observation(observation)
    return observation


def _dedupe_node_interfaces(
    nodes: Sequence[_NodeState],
    links: Sequence[_LinkState],
) -> None:
    replacements: dict[str, str] = {}
    for node in nodes:
        kept: list[ObservedInterface] = []
        by_key: dict[tuple[str, str], ObservedInterface] = {}
        for interface in node.observation.observed_interfaces:
            name = _normalized_interface_name(
                interface.raw_name
                or (interface.name_candidates[0] if interface.name_candidates else None),
                node.observation,
            )
            if name:
                key = ("name", name.casefold())
            elif interface.ip_candidates:
                key = (
                    "ip",
                    "|".join(sorted(str(value) for value in interface.ip_candidates)),
                )
            else:
                key = ("id", interface.observation_id)
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = interface
                kept.append(interface)
                continue
            merged = _merge_interfaces(existing, interface)
            by_key[key] = merged
            kept = [merged if item.observation_id == existing.observation_id else item for item in kept]
            replacements[interface.observation_id] = existing.observation_id
        node.observation = _replace_node(node.observation, observed_interfaces=kept)
    if not replacements:
        return
    for link in links:
        link.observation = _replace_link(
            link.observation,
            source_interface_candidates=[
                replacements.get(value, value)
                for value in link.observation.source_interface_candidates
            ],
            target_interface_candidates=[
                replacements.get(value, value)
                for value in link.observation.target_interface_candidates
            ],
        )


def _point_to_bbox_distance(point: Point, bbox: BoundingBox) -> float:
    dx = max(bbox.x - point.x, 0.0, point.x - (bbox.x + bbox.width))
    dy = max(bbox.y - point.y, 0.0, point.y - (bbox.y + bbox.height))
    return math.hypot(dx, dy)


def _remove_composed_path_links(
    links: list[_LinkState], nodes: Sequence[_NodeState]
) -> list[dict[str, Any]]:
    """Drop a model edge only when two direct edges reproduce its path via a node."""

    node_map = {node.observation.observation_id: node.observation for node in nodes}
    endpoints = {
        link.ref: _resolved_link_endpoints(link)
        for link in links
    }
    rejected: set[str] = set()
    audit: list[dict[str, Any]] = []
    for candidate in links:
        candidate_endpoints = endpoints[candidate.ref]
        if candidate_endpoints is None:
            continue
        source_id, target_id = candidate_endpoints
        source = node_map.get(source_id)
        target = node_map.get(target_id)
        if source is None or target is None:
            continue
        for intermediate_state in nodes:
            intermediate = intermediate_state.observation
            intermediate_id = intermediate.observation_id
            if intermediate_id in candidate_endpoints:
                continue
            if not _polyline_intersects_bbox(candidate.observation.polyline, intermediate.bbox):
                continue
            tolerance = _composed_path_tolerance(source, intermediate, target)
            source_links = _direct_links_between(
                links,
                source_id,
                intermediate_id,
                candidate.ref,
                nodes,
            )
            target_links = _direct_links_between(
                links,
                intermediate_id,
                target_id,
                candidate.ref,
                nodes,
            )
            match = next(
                (
                    (source_link, target_link)
                    for source_link in source_links
                    for target_link in target_links
                    if _polylines_describe_same_path(
                        candidate.observation.polyline,
                        source_link.observation.polyline,
                        target_link.observation.polyline,
                        tolerance,
                    )
                ),
                None,
            )
            if match is None:
                continue
            source_link, target_link = match
            rejected.add(candidate.ref)
            audit.append(
                {
                    "candidateInitialObservationId": candidate.ref,
                    "candidateTemporaryModelId": candidate.temporary_id,
                    "sourceNodeId": source_id,
                    "intermediateNodeId": intermediate_id,
                    "targetNodeId": target_id,
                    "supportingInitialObservationIds": [
                        source_link.ref,
                        target_link.ref,
                    ],
                    "supportingTemporaryModelIds": [
                        source_link.temporary_id,
                        target_link.temporary_id,
                    ],
                    "evidenceIds": candidate.observation.evidence_ids,
                    "geometryTolerancePx": round(tolerance, 3),
                }
            )
            break
    if not rejected:
        return audit
    links[:] = [link for link in links if link.ref not in rejected]
    for index, link in enumerate(links, start=1):
        observation_id = f"obs_link_{index:03d}"
        link.ref = observation_id
        link.observation = _replace_link(
            link.observation, observation_id=observation_id
        )
    return audit


def _resolved_link_endpoints(link: _LinkState) -> tuple[str, str] | None:
    source = link.observation.source_node_candidates
    target = link.observation.target_node_candidates
    if len(source) != 1 or len(target) != 1 or source[0] == target[0]:
        return None
    return source[0], target[0]


def _direct_links_between(
    links: Sequence[_LinkState],
    first: str,
    second: str,
    excluded_ref: str,
    nodes: Sequence[_NodeState],
) -> list[_LinkState]:
    expected = frozenset((first, second))
    return [
        link
        for link in links
        if link.ref != excluded_ref
        and _resolved_link_endpoints(link) is not None
        and frozenset(_resolved_link_endpoints(link) or ()) == expected
        and not _link_traverses_other_node(link, nodes)
    ]


def _link_traverses_other_node(
    link: _LinkState, nodes: Sequence[_NodeState]
) -> bool:
    endpoints = _resolved_link_endpoints(link)
    if endpoints is None:
        return True
    return any(
        node.observation.observation_id not in endpoints
        and _polyline_intersects_bbox(link.observation.polyline, node.observation.bbox)
        for node in nodes
    )


def _composed_path_tolerance(*nodes: ObservedNode) -> float:
    largest_node_side = max(
        max(node.bbox.width, node.bbox.height) for node in nodes
    )
    return min(32.0, max(8.0, largest_node_side * 0.35))


def _polyline_intersects_bbox(
    polyline: Sequence[Point], bbox: BoundingBox
) -> bool:
    return any(
        _segment_intersects_bbox(first, second, bbox)
        for first, second in zip(polyline, polyline[1:])
    )


def _segment_intersects_bbox(
    first: Point, second: Point, bbox: BoundingBox
) -> bool:
    lower_bounds = (bbox.x, bbox.y)
    upper_bounds = (bbox.x + bbox.width, bbox.y + bbox.height)
    starts = (first.x, first.y)
    deltas = (second.x - first.x, second.y - first.y)
    lower, upper = 0.0, 1.0
    for start, delta, lower_bound, upper_bound in zip(
        starts, deltas, lower_bounds, upper_bounds
    ):
        if abs(delta) < 1e-9:
            if start < lower_bound or start > upper_bound:
                return False
            continue
        first_t = (lower_bound - start) / delta
        second_t = (upper_bound - start) / delta
        lower = max(lower, min(first_t, second_t))
        upper = min(upper, max(first_t, second_t))
        if lower > upper:
            return False
    return True


def _polylines_describe_same_path(
    candidate: Sequence[Point],
    first: Sequence[Point],
    second: Sequence[Point],
    tolerance: float,
) -> bool:
    return all(
        _point_to_polyline_distance(point, candidate) <= tolerance
        for point in [*_polyline_probe_points(first), *_polyline_probe_points(second)]
    ) and all(
        min(
            _point_to_polyline_distance(point, first),
            _point_to_polyline_distance(point, second),
        )
        <= tolerance
        for point in _polyline_probe_points(candidate)
    )


def _polyline_probe_points(polyline: Sequence[Point]) -> list[Point]:
    probes: list[Point] = []
    for first, second in zip(polyline, polyline[1:]):
        probes.extend(
            [
                first,
                Point(x=(first.x + second.x) / 2, y=(first.y + second.y) / 2),
                second,
            ]
        )
    return probes


def _point_to_polyline_distance(point: Point, polyline: Sequence[Point]) -> float:
    return min(
        _point_to_segment_distance(point, first, second)
        for first, second in zip(polyline, polyline[1:])
    )


def _point_to_segment_distance(point: Point, first: Point, second: Point) -> float:
    dx, dy = second.x - first.x, second.y - first.y
    length_squared = dx * dx + dy * dy
    if length_squared <= 1e-9:
        return math.hypot(point.x - first.x, point.y - first.y)
    factor = max(
        0.0,
        min(
            1.0,
            ((point.x - first.x) * dx + (point.y - first.y) * dy)
            / length_squared,
        ),
    )
    return math.hypot(
        point.x - (first.x + factor * dx),
        point.y - (first.y + factor * dy),
    )


def _audit_link_geometry(
    links: Sequence[_LinkState],
    nodes: Sequence[_NodeState],
) -> list[UnresolvedItem]:
    unresolved: list[UnresolvedItem] = []
    node_map = {
        node.observation.observation_id: node.observation for node in nodes
    }
    for link in links:
        item = link.observation
        if len(item.polyline) < 2 or len(item.source_node_candidates) != 1 or len(item.target_node_candidates) != 1:
            continue
        source = node_map.get(item.source_node_candidates[0])
        target = node_map.get(item.target_node_candidates[0])
        if source is None or target is None:
            continue
        if source.observation_id == target.observation_id:
            unresolved.append(
                _make_binding_unresolved(
                    "LINK_GEOMETRY_INCONSISTENT",
                    [item.observation_id],
                    "link source and target resolve to the same node",
                    item.evidence_ids,
                    candidates=[item.observation_id],
                    field="sourceNodeCandidates",
                )
            )
            continue
        first, last = item.polyline[0], item.polyline[-1]
        forward = _point_to_bbox_distance(first, source.bbox) + _point_to_bbox_distance(last, target.bbox)
        reverse = _point_to_bbox_distance(last, source.bbox) + _point_to_bbox_distance(first, target.bbox)
        if reverse + 1e-6 < forward:
            item = _replace_link(item, polyline=list(reversed(item.polyline)))
            link.observation = item
            forward = reverse
        tolerance = max(
            18.0,
            max(source.bbox.width, source.bbox.height, target.bbox.width, target.bbox.height) * 1.5,
        )
        if forward > tolerance:
            link.observation = _replace_link(
                link.observation,
                confidence=max(0.0, link.observation.confidence * 0.75),
            )
            unresolved.append(
                _make_binding_unresolved(
                    "LINK_GEOMETRY_INCONSISTENT",
                    [item.observation_id],
                    "polyline endpoints are not close to the resolved node bounding boxes",
                    item.evidence_ids,
                    candidates=[f"endpointDistance:{forward:.3f}"],
                    field="polyline",
                )
            )
    return unresolved


def _validate_final_observation(observation: TopologyObservation) -> None:
    width, height = observation.image.width, observation.image.height
    allowed_views = {"global_structure", "global_links", "global_text"}
    registered_views = set(observation.image.view_ids)
    if not allowed_views.issubset(registered_views):
        raise ModelInvocationError("final imageInfo is missing a required complete view")
    evidence_ids = {item.evidence_id for item in observation.evidence}
    node_ids = {node.observation_id for node in observation.observed_nodes}
    link_ids = {link.observation_id for link in observation.observed_links}
    region_ids = {region.observation_id for region in observation.observed_regions}
    nested_interfaces = {
        interface.observation_id
        for node in observation.observed_nodes
        for interface in node.observed_interfaces
    }
    interface_values = [interface.observation_id for interface in observation.observed_interfaces]
    interface_ids = set(interface_values)
    interface_owners = {
        interface.observation_id: set(interface.node_candidates)
        for interface in observation.observed_interfaces
    }
    interface_keys: set[tuple[str, str]] = set()
    if len(node_ids) != len(observation.observed_nodes) or len(link_ids) != len(observation.observed_links) or len(region_ids) != len(observation.observed_regions) or len(evidence_ids) != len(observation.evidence):
        raise ModelInvocationError("final observation contains duplicate IDs")
    if len(interface_values) != len(set(interface_values)):
        raise ModelInvocationError("final observation contains duplicate interface IDs")
    if not nested_interfaces.issubset(interface_ids):
        raise ModelInvocationError("node contains an interface missing from observedInterfaces")
    for item in observation.evidence:
        if item.source_view_id not in allowed_views:
            raise ModelInvocationError("final evidence references an unapproved view")
        if item.source_type is EvidenceSourceType.NETWORK_DERIVATION:
            raise ModelInvocationError("visual recognition cannot emit network-derived evidence")
        if item.bbox is not None:
            _validate_original_bbox(item.bbox, width, height)
    for node in observation.observed_nodes:
        _validate_original_bbox(node.bbox, width, height)
        _validate_point_in_bbox(node.center, node.bbox)
        _validate_references(node.evidence_ids, evidence_ids)
        if any(view not in allowed_views for view in node.source_view_ids):
            raise ModelInvocationError("node references an unapproved view")
        if any(region_id not in region_ids for region_id in node.region_candidates):
            raise ModelInvocationError("node references an unknown region")
        for interface in node.observed_interfaces:
            if interface.observation_id not in interface_ids:
                raise ModelInvocationError(
                    "node contains an interface missing from observedInterfaces"
                )
            if node.observation_id not in interface.node_candidates:
                raise ModelInvocationError(
                    "node-owned interface does not reference its owner"
                )
    for interface in observation.observed_interfaces:
        if any(node_id not in node_ids for node_id in interface.node_candidates):
            raise ModelInvocationError("interface references an unknown node")
        owner_node = next(
            (node for node in observation.observed_nodes if node.observation_id in interface.node_candidates),
            None,
        )
        interface_name = _normalized_interface_name(
            interface.raw_name
            or (interface.name_candidates[0] if interface.name_candidates else None),
            owner_node,
        )
        if interface_name:
            for node_id in interface.node_candidates:
                key = (node_id, interface_name.casefold())
                if key in interface_keys:
                    raise ModelInvocationError("same node contains duplicate interface names")
                interface_keys.add(key)
        _validate_references(interface.evidence_ids, evidence_ids)
        if interface.label_bbox is not None:
            _validate_original_bbox(interface.label_bbox, width, height)
        if interface.ip_bbox is not None:
            _validate_original_bbox(interface.ip_bbox, width, height)
        if any(link_id not in link_ids for link_id in interface.nearby_link_ids):
            raise ModelInvocationError("interface references an unknown link")
    for link in observation.observed_links:
        _validate_references(link.evidence_ids, evidence_ids)
        if len(link.polyline) < 2:
            raise ModelInvocationError("link polyline must contain at least two points")
        if len({(point.x, point.y) for point in link.polyline}) < 2:
            raise ModelInvocationError("link polyline must contain distinct endpoints")
        if not link.source_node_candidates or not link.target_node_candidates:
            raise ModelInvocationError("link must retain source and target node candidates")
        if any(node_id not in node_ids for node_id in [*link.source_node_candidates, *link.target_node_candidates]):
            raise ModelInvocationError("link references an unknown node")
        if (
            link.source_node_candidates
            and link.target_node_candidates
            and set(link.source_node_candidates) == set(link.target_node_candidates) == {link.source_node_candidates[0]}
        ):
            raise ModelInvocationError("link source and target cannot be the same node")
        if any(
            interface_id not in interface_ids
            for interface_id in [
                *link.source_interface_candidates,
                *link.target_interface_candidates,
            ]
        ):
            raise ModelInvocationError("link references an unknown interface")
        if (
            link.source_node_candidates
            and any(
                not interface_owners[interface_id].intersection(link.source_node_candidates)
                for interface_id in link.source_interface_candidates
            )
        ):
            raise ModelInvocationError(
                "source interface does not belong to a source node candidate"
            )
        if (
            link.target_node_candidates
            and any(
                not interface_owners[interface_id].intersection(link.target_node_candidates)
                for interface_id in link.target_interface_candidates
            )
        ):
            raise ModelInvocationError(
                "target interface does not belong to a target node candidate"
            )
        for point in link.polyline:
            if not (0 <= point.x < width and 0 <= point.y < height):
                raise ModelInvocationError("link point is outside the original image")
    for region in observation.observed_regions:
        _validate_original_bbox(region.bbox, width, height)
        _validate_references(region.evidence_ids, evidence_ids)
        if any(node_id not in node_ids for node_id in region.member_node_candidates):
            raise ModelInvocationError("region references an unknown node")
    unresolved_ids = [item.temp_id for item in observation.unresolved_items]
    if len(unresolved_ids) != len(set(unresolved_ids)):
        raise ModelInvocationError("final observation contains duplicate unresolved IDs")
    for item in observation.unresolved_items:
        _validate_references(item.evidence_ids, evidence_ids)
        if any(object_id not in node_ids | link_ids | region_ids | interface_ids for object_id in item.object_ids):
            raise ModelInvocationError("unresolved item references an unknown object")
    interface_count = len(interface_values)
    blocking_count = sum(item.blocking for item in observation.unresolved_items)
    expected_counts = {
        "nodeCount": len(observation.observed_nodes),
        "interfaceCount": interface_count,
        "linkCount": len(observation.observed_links),
        "regionCount": len(observation.observed_regions),
        "evidenceCount": len(observation.evidence),
        "unresolvedCount": len(observation.unresolved_items),
        "blockingUnresolvedCount": blocking_count,
    }
    for key, value in expected_counts.items():
        if observation.summary.get(key) != value:
            raise ModelInvocationError(f"summary field {key} does not match final observation")


def _stage_header(task_id: str, stage: str, view: ImageView, bundle: ImageBundle) -> str:
    return (
        f"taskId: {task_id}\ncurrentStage: {stage}\ncurrentViewId: {view.view_id}\n"
        f"currentViewSize: {view.width} x {view.height}\noriginalImageSize: {bundle.image_info.width} x {bundle.image_info.height}\n"
        "Model geometry must use current-view pixels with the image center as origin (0,0), x rightward, and y downward. "
        "Use at most two decimal places for x/y/width/height and do not normalize coordinates. The program only "
        "translates the origin before applying the required view-to-original mapping."
    )


def _visual_request_context(
    task_id: str,
    stage: Literal["structure", "links", "text"],
    view: ImageView,
    bundle: ImageBundle,
) -> dict[str, Any]:
    return {
        "taskId": task_id,
        "currentStage": stage,
        "image": {
            "width": bundle.image_info.width,
            "height": bundle.image_info.height,
            "format": bundle.image_info.format,
        },
        "view": {
            "viewId": view.view_id,
            "width": view.width,
            "height": view.height,
            "coversCompleteImage": True,
            "coordinateMapping": {
                "modelSource": "current-view pixels centered at image center (0,0), x right, y down, at most two decimals",
                "target": "EXIF-corrected original pixels",
                "viewCenterX": view.width / 2.0,
                "viewCenterY": view.height / 2.0,
                "scaleX": view.scale_x,
                "scaleY": view.scale_y,
                "offsetX": view.original_bounds.x,
                "offsetY": view.original_bounds.y,
            },
        },
    }


def _checked_stage(stage: str) -> str:
    if stage not in {"structure", "links", "text", "fusion"}:
        raise InputError(f"unknown recognition stage {stage!r}")
    return stage


def _checked_visual_stage(stage: str) -> str:
    if stage not in {"structure", "links", "text"}:
        raise InputError(f"unknown visual recognition stage {stage!r}")
    return stage


def _next_attempt_directory(task_dir: Path) -> Path:
    for index in range(1, 10_000):
        candidate = task_dir / f"attempt_{index:03d}"
        try:
            candidate.mkdir(exist_ok=False)
        except FileExistsError:
            continue
        return candidate
    raise OSError("recognition attempt limit exceeded")


def _ensure_four_call_budget(client: OpenAICompatibleModelClient) -> None:
    method = getattr(client, "ensure_model_call_budget", None)
    if callable(method):
        method(4)
        return
    remaining = getattr(client, "remaining_model_calls", None)
    if isinstance(remaining, int) and remaining < 4:
        raise ModelInvocationError("at least four logical model calls are required")


def _validate_view_candidate_geometry(bbox: BoundingBox, center: Point, view: ImageView) -> None:
    _validate_view_bbox(bbox, view)
    if not (bbox.x - 0.5 <= center.x <= bbox.x + bbox.width + 0.5 and bbox.y - 0.5 <= center.y <= bbox.y + bbox.height + 0.5):
        raise ModelInvocationError(f"center is outside bbox in {view.view_id}")
    if not (0 <= center.x < view.width and 0 <= center.y < view.height):
        raise ModelInvocationError(f"center is outside view {view.view_id}")


def _fit_view_bbox_to_bounds(bbox: BoundingBox, view: ImageView) -> BoundingBox:
    """Clip a partially visible boundary object while rejecting bad coordinates."""

    right = bbox.x + bbox.width
    bottom = bbox.y + bbox.height
    left = max(0.0, bbox.x)
    top = max(0.0, bbox.y)
    clipped_right = min(float(view.width), right)
    clipped_bottom = min(float(view.height), bottom)
    if clipped_right <= left or clipped_bottom <= top:
        _validate_view_bbox(bbox, view)
        return bbox
    visible_ratio = ((clipped_right - left) * (clipped_bottom - top)) / (
        bbox.width * bbox.height
    )
    if visible_ratio < 0.25:
        raise ModelInvocationError(
            f"bbox is mostly outside view {view.view_id}"
        )
    return BoundingBox(
        x=left,
        y=top,
        width=clipped_right - left,
        height=clipped_bottom - top,
    )


def _validate_view_bbox(bbox: BoundingBox, view: ImageView) -> None:
    if (
        bbox.x < 0
        or bbox.y < 0
        or bbox.x + bbox.width > view.width + 1e-6
        or bbox.y + bbox.height > view.height + 1e-6
    ):
        raise ModelInvocationError(f"bbox is outside view {view.view_id}")


def _validate_view_polyline(polyline: Sequence[Point], view: ImageView) -> None:
    for point in polyline:
        if not (0 <= point.x < view.width and 0 <= point.y < view.height):
            raise ModelInvocationError(f"polyline point is outside view {view.view_id}")


def _validate_original_bbox(bbox: BoundingBox, width: int, height: int) -> None:
    if bbox.x < 0 or bbox.y < 0 or bbox.x + bbox.width > width or bbox.y + bbox.height > height:
        raise ModelInvocationError("bbox is outside original image")


def _validate_point_in_bbox(point: Point, bbox: BoundingBox) -> None:
    tolerance = 1.0
    if not (
        bbox.x - tolerance <= point.x <= bbox.x + bbox.width + tolerance
        and bbox.y - tolerance <= point.y <= bbox.y + bbox.height + tolerance
    ):
        raise ModelInvocationError("center is outside node bbox")


def _register_pass_evidence(
    registry: dict[str, Evidence],
    view: ImageView,
    values: Sequence[_PassEvidence],
    fallback_bbox: BoundingBox | None,
) -> list[str]:
    result: list[str] = []
    for value in values:
        if value.source_type is EvidenceSourceType.NETWORK_DERIVATION:
            raise ModelInvocationError("visual recognition cannot register network-derived evidence")
        bbox = value.bbox or fallback_bbox
        if bbox is not None:
            bbox = _fit_view_bbox_to_bounds(bbox, view)
        mapped_bbox = view_bbox_to_original(view, bbox) if bbox is not None else None
        key = json.dumps(
            {
                "view": view.view_id,
                "type": value.source_type.value,
                "bbox": mapped_bbox.model_dump() if mapped_bbox else None,
                "raw": value.raw_text,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        existing = next((key_id for key_id, item in registry.items() if _evidence_key(item) == key), None)
        if existing is not None:
            result.append(existing)
            continue
        evidence_id = f"evidence_{len(registry) + 1:03d}"
        registry[evidence_id] = Evidence(
            evidence_id=evidence_id,
            source_type=value.source_type,
            source_view_id=view.view_id,
            bbox=mapped_bbox,
            raw_text=value.raw_text,
            description=(value.description.strip() or "visual observation"),
            confidence=value.confidence,
        )
        result.append(evidence_id)
    return _unique_strings(result)


def _register_text_evidence(
    registry: dict[str, Evidence],
    view: ImageView,
    values: Sequence[_PassEvidence],
    *,
    raw_text: str | None,
    fallback_bbox: BoundingBox | None,
    confidence: float,
) -> list[str]:
    text_values = list(values)
    if (
        isinstance(raw_text, str)
        and raw_text.strip()
        and not any(item.raw_text == raw_text for item in text_values)
    ):
        model_bbox = (
            _view_bbox_to_model(view, fallback_bbox)
            if fallback_bbox is not None
            else None
        )
        generated = _PassEvidence(
            source_type=EvidenceSourceType.VISUAL_TEXT,
            bbox=model_bbox,
            raw_text=raw_text,
            description="visible text",
            confidence=confidence,
        )
        text_values.append(generated.model_copy(update={"bbox": fallback_bbox}))
    return _register_pass_evidence(registry, view, text_values, fallback_bbox)


def _evidence_key(item: Evidence) -> str:
    return json.dumps(
        {
            "view": item.source_view_id,
            "type": item.source_type.value,
            "bbox": item.bbox.model_dump() if item.bbox else None,
            "raw": item.raw_text,
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def _convert_pass_unresolved(values: Sequence[_PassUnresolved], view: ImageView, evidence: dict[str, Evidence], stage: str) -> list[UnresolvedItem]:
    result: list[UnresolvedItem] = []
    nonblocking_categories = {
        UnresolvedCategory.UNKNOWN_PREFIX,
        UnresolvedCategory.IP_INTERFACE_BINDING_AMBIGUITY,
        UnresolvedCategory.INTERFACE_NODE_BINDING_AMBIGUITY,
        UnresolvedCategory.INTERFACE_BINDING_AMBIGUITY,
        UnresolvedCategory.INTERFACE_IP_MISSING,
        UnresolvedCategory.INTERFACE_NAME_MISSING,
        UnresolvedCategory.TEXT_COVERAGE_INCOMPLETE,
        UnresolvedCategory.LINK_GEOMETRY_INCONSISTENT,
    }
    for value in values:
        ids = _register_pass_evidence(evidence, view, value.evidence, None)
        category = _unresolved_category(value.category)
        blocking = False if category in nonblocking_categories else value.blocking or _is_blocking_unresolved(category)
        result.append(UnresolvedItem(temp_id=f"unresolved_{len(result) + 1:03d}", category=category, object_ids=_unique_strings(value.object_ids), field=value.field, candidates=_unique_strings(value.candidates), reason=value.reason.strip() or f"{stage} ambiguity", blocking=blocking, recommended_action=value.recommended_action.strip() or "preserve candidates", evidence_ids=ids))
    return result


def _unresolved_category(value: str) -> UnresolvedCategory:
    try:
        return UnresolvedCategory(value)
    except ValueError:
        return UnresolvedCategory.UNSUPPORTED_TOPOLOGY_PATTERN


def _make_binding_unresolved(
    category: str,
    object_ids: Sequence[str],
    reason: str,
    evidence_ids: Sequence[str],
    candidates: Sequence[str] | None = None,
    *,
    field: str | None = None,
) -> UnresolvedItem:
    unresolved_category = _unresolved_category(category)
    return UnresolvedItem(
        temp_id="unresolved_pending",
        category=unresolved_category,
        object_ids=_unique_strings(object_ids),
        field=field,
        candidates=_unique_strings(candidates if candidates is not None else object_ids),
        reason=reason,
        blocking=_is_blocking_unresolved(unresolved_category),
        recommended_action="use Fusion Pass or retain unresolved",
        evidence_ids=_unique_strings(evidence_ids),
    )


def _conflict_unresolved(conflict: _Conflict, reason: str) -> UnresolvedItem:
    category = _unresolved_category(conflict.conflict_type.value)
    return UnresolvedItem(temp_id=f"unresolved_{conflict.conflict_id}", category=category, object_ids=_unique_strings(conflict.target_ids), field=conflict.field_name, candidates=[str(option.value) for option in conflict.candidate_options], reason=reason, blocking=_is_blocking_unresolved(category), recommended_action="retain candidates and evidence", evidence_ids=_unique_strings(conflict.supporting_evidence_ids))


def _invalid_fusion_unresolved(conflict_id: str) -> UnresolvedItem:
    return UnresolvedItem(
        temp_id="unresolved_invalid_fusion",
        category=UnresolvedCategory.UNSUPPORTED_TOPOLOGY_PATTERN,
        object_ids=[],
        candidates=[conflict_id],
        reason="fusion decision referenced an unknown conflict or option",
        blocking=True,
        recommended_action="reject decision and retain input observations",
        evidence_ids=[],
    )


def _is_blocking_unresolved(category: UnresolvedCategory) -> bool:
    return category in {
        UnresolvedCategory.AMBIGUOUS_IP,
        UnresolvedCategory.AMBIGUOUS_LINK_ENDPOINT,
        UnresolvedCategory.LINK_ENDPOINT_AMBIGUITY,
        UnresolvedCategory.CROSSING_UNCERTAIN,
        UnresolvedCategory.CROSSING_OR_CONNECTION_AMBIGUITY,
        UnresolvedCategory.NODE_DUPLICATION_AMBIGUITY,
    }


def _dedupe_unresolved(values: Sequence[UnresolvedItem]) -> list[UnresolvedItem]:
    result: list[UnresolvedItem] = []
    seen: set[str] = set()
    for item in values:
        signature = json.dumps(
            {
                "category": item.category.value,
                "objectIds": item.object_ids,
                "field": item.field,
                "candidates": item.candidates,
                "evidenceIds": item.evidence_ids,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        if signature in seen:
            continue
        seen.add(signature)
        result.append(UnresolvedItem(temp_id=f"unresolved_{len(result) + 1:03d}", category=item.category, object_ids=item.object_ids, field=item.field, candidates=item.candidates, reason=item.reason, blocking=item.blocking, recommended_action=item.recommended_action, evidence_ids=item.evidence_ids))
    return result


def _renumber_unresolved(
    values: Sequence[UnresolvedItem],
) -> list[UnresolvedItem]:
    return [
        UnresolvedItem(
            temp_id=f"unresolved_{index:03d}",
            category=item.category,
            object_ids=item.object_ids,
            field=item.field,
            candidates=item.candidates,
            reason=item.reason,
            blocking=item.blocking,
            recommended_action=item.recommended_action,
            evidence_ids=item.evidence_ids,
        )
        for index, item in enumerate(values, start=1)
    ]


def _normalize_unresolved_references(
    values: Sequence[UnresolvedItem],
    nodes: Sequence[_NodeState],
    regions: Sequence[_RegionState],
    links: Sequence[_LinkState],
    evidence: dict[str, Evidence],
    extra_interfaces: Sequence[ObservedInterface] = (),
) -> list[UnresolvedItem]:
    aliases: dict[str, str] = {}
    valid: set[str] = set()
    for item in nodes:
        valid.add(item.observation.observation_id)
        aliases[item.ref] = item.observation.observation_id
        aliases.update({temporary: item.observation.observation_id for temporary in item.temporary_ids})
        for interface in item.observation.observed_interfaces:
            valid.add(interface.observation_id)
            aliases[interface.observation_id] = interface.observation_id
    for item in regions:
        valid.add(item.observation.observation_id)
        aliases[item.ref] = item.observation.observation_id
        aliases.update({temporary: item.observation.observation_id for temporary in item.temporary_ids})
    for item in links:
        valid.add(item.observation.observation_id)
        aliases[item.ref] = item.observation.observation_id
        aliases[item.temporary_id] = item.observation.observation_id
    for interface in extra_interfaces:
        valid.add(interface.observation_id)
        aliases[interface.observation_id] = interface.observation_id
    evidence_ids = set(evidence)
    result: list[UnresolvedItem] = []
    for item in values:
        resolved_object_ids = [aliases.get(value, value) for value in item.object_ids]
        object_ids = [value for value in resolved_object_ids if value in valid]
        unknown_objects = [
            original
            for original, resolved in zip(item.object_ids, resolved_object_ids)
            if resolved not in valid
        ]
        item_evidence = [value for value in item.evidence_ids if value in evidence_ids]
        unknown_evidence = [
            value for value in item.evidence_ids if value not in evidence_ids
        ]
        rejected_references = [
            *[f"unknownObject:{value}" for value in unknown_objects],
            *[f"unknownEvidence:{value}" for value in unknown_evidence],
        ]
        reason = item.reason
        if rejected_references:
            reason = (
                f"{reason}; unrecognized references were retained as candidates"
            )
        result.append(
            UnresolvedItem(
                temp_id=item.temp_id,
                category=item.category,
                object_ids=_unique_strings(object_ids),
                field=item.field,
                candidates=_unique_strings(
                    [*item.candidates, *rejected_references]
                ),
                reason=reason,
                blocking=item.blocking,
                recommended_action=item.recommended_action,
                evidence_ids=_unique_strings(item_evidence),
            )
        )
    return result


def _normalize_link_interface_candidates(
    links: Sequence[_LinkState],
    nodes: Sequence[_NodeState],
    *,
    emit_unresolved: bool = True,
) -> list[UnresolvedItem]:
    interface_owner: dict[str, str] = {}
    name_to_ids: dict[str, list[str]] = {}
    for node in nodes:
        for interface in node.observation.observed_interfaces:
            interface_owner[interface.observation_id] = node.observation.observation_id
            for name in [interface.raw_name, *interface.name_candidates]:
                if isinstance(name, str) and name.strip():
                    normalized = _normalized_interface_name(name, node.observation) or name.strip()
                    name_to_ids.setdefault(normalized.casefold(), []).append(interface.observation_id)
    unresolved: list[UnresolvedItem] = []
    for link in links:
        for field_name, raw_values, current_values, endpoint_nodes in (
            (
                "sourceInterfaceCandidates",
                link.raw_source_interface_candidates,
                link.observation.source_interface_candidates,
                link.observation.source_node_candidates,
            ),
            (
                "targetInterfaceCandidates",
                link.raw_target_interface_candidates,
                link.observation.target_interface_candidates,
                link.observation.target_node_candidates,
            ),
        ):
            allowed_nodes = set(endpoint_nodes)
            values = raw_values or current_values
            if not values and allowed_nodes:
                # Text observations may identify the interface beside a link
                # without repeating the interface name in the links pass.
                # Use the explicit nearby-link relation as a deterministic
                # fallback, scoped to the endpoint node candidates.
                nearby_ids = [
                    interface.observation_id
                    for node in nodes
                    if node.observation.observation_id in allowed_nodes
                    for interface in node.observation.observed_interfaces
                    if link.observation.observation_id in interface.nearby_link_ids
                ]
                values = _unique_strings(nearby_ids)
            resolved: list[str] = []
            unknown: list[str] = []
            for value in values:
                endpoint_node = None
                if len(endpoint_nodes) == 1:
                    try:
                        endpoint_node = _node_by_observation_id(nodes, endpoint_nodes[0]).observation
                    except ModelInvocationError:
                        endpoint_node = None
                matches = (
                    [value]
                    if value in interface_owner
                    else _unique_strings(
                        name_to_ids.get(
                            (
                                _normalized_interface_name(
                                    value,
                                    endpoint_node,
                                )
                                or value.strip()
                            ).casefold(),
                            [],
                        )
                    )
                )
                if allowed_nodes:
                    matches = [
                        interface_id
                        for interface_id in matches
                        if interface_owner.get(interface_id) in allowed_nodes
                    ]
                if matches:
                    resolved.extend(matches)
                else:
                    unknown.append(value)
            resolved = _unique_strings(resolved)
            # Preserve a lawful Fusion reordering while rebuilding raw labels.
            resolved = _unique_strings(
                [
                    *[
                        value for value in current_values if value in resolved
                    ],
                    *resolved,
                ]
            )
            if field_name == "sourceInterfaceCandidates":
                link.observation = _replace_link(
                    link.observation,
                    source_interface_candidates=resolved,
                )
            else:
                link.observation = _replace_link(
                    link.observation,
                    target_interface_candidates=resolved,
                )
            if emit_unresolved and endpoint_nodes and (unknown or len(resolved) > 1):
                unresolved.append(
                    _make_binding_unresolved(
                        "INTERFACE_BINDING_AMBIGUITY",
                        [link.observation.observation_id],
                        f"{field_name} has multiple or unbound interface candidates",
                        link.observation.evidence_ids,
                        candidates=_unique_strings([*resolved, *unknown]),
                        field=field_name,
                    )
                )
    return unresolved


def _materialize_link_interfaces(
    links: Sequence[_LinkState],
    nodes: list[_NodeState],
    pending_bindings: Sequence[_PendingBinding] = (),
) -> list[UnresolvedItem]:
    """Turn link-stage interface labels into node-owned observations before binding links."""

    reserved_interfaces = [
        item.interface
        for item in pending_bindings
        if item.interface is not None
    ]
    next_interface = _next_interface_number(nodes, reserved_interfaces)
    visible_names_by_node: dict[str, set[str]] = {}
    for link in links:
        for raw_values, endpoint_nodes in (
            (link.raw_source_interface_candidates, link.observation.source_node_candidates),
            (link.raw_target_interface_candidates, link.observation.target_node_candidates),
        ):
            if len(endpoint_nodes) != 1:
                continue
            try:
                node = _node_by_observation_id(nodes, endpoint_nodes[0])
            except ModelInvocationError:
                continue
            for raw_value in _unique_strings(raw_values):
                name = _normalized_interface_name(raw_value, node.observation)
                if name:
                    visible_names_by_node.setdefault(node.observation.observation_id, set()).add(
                        name.casefold()
                    )
    for link in links:
        for raw_values, endpoint_nodes in (
            (
                link.raw_source_interface_candidates,
                link.observation.source_node_candidates,
            ),
            (
                link.raw_target_interface_candidates,
                link.observation.target_node_candidates,
            ),
        ):
            if len(endpoint_nodes) != 1:
                continue
            try:
                node = _node_by_observation_id(nodes, endpoint_nodes[0])
            except ModelInvocationError:
                continue
            for raw_value in _unique_strings(raw_values):
                if raw_value in {
                    interface.observation_id
                    for item in nodes
                    for interface in item.observation.observed_interfaces
                }:
                    continue
                interface_name = _normalized_interface_name(
                    raw_value,
                    node.observation,
                )
                if not interface_name:
                    continue
                unnamed_ip_interfaces = [
                    interface
                    for interface in node.observation.observed_interfaces
                    if not interface.raw_name
                    and not interface.name_candidates
                    and interface.ip_candidates
                ]
                if (
                    len(visible_names_by_node.get(node.observation.observation_id, set())) == 1
                    and len(unnamed_ip_interfaces) == 1
                ):
                    existing = unnamed_ip_interfaces[0]
                    updated = _replace_interface(
                        existing,
                        raw_name=interface_name,
                        name_candidates=[interface_name],
                        nearby_link_ids=_unique_strings(
                            [
                                *existing.nearby_link_ids,
                                link.observation.observation_id,
                            ]
                        ),
                        confidence=max(existing.confidence, link.observation.confidence),
                        evidence_ids=_unique_strings(
                            [*existing.evidence_ids, *link.observation.evidence_ids]
                        ),
                    )
                    node.observation = _replace_node(
                        node.observation,
                        observed_interfaces=[
                            updated
                            if item.observation_id == existing.observation_id
                            else item
                            for item in node.observation.observed_interfaces
                        ],
                    )
                    continue
                candidate = ObservedInterface(
                    observation_id=f"obs_if_{next_interface:03d}",
                    node_candidates=[node.observation.observation_id],
                    raw_name=interface_name,
                    name_candidates=[interface_name],
                    raw_ip_text=None,
                    ip_candidates=[],
                    nearby_link_ids=[link.observation.observation_id],
                    confidence=link.observation.confidence,
                    evidence_ids=_unique_strings(link.observation.evidence_ids),
                )
                before_ids = {
                    item.observation_id
                    for item in node.observation.observed_interfaces
                }
                merged = _upsert_node_interface(node, candidate)
                if merged.observation_id not in before_ids:
                    next_interface += 1

    # A visible link label without an IP is a complete visual observation, not an
    # ambiguity. M5 decides whether a device role later requires an address.
    return []


def _interface_candidate_values(
    interface: ObservedInterface, invalid_values: Sequence[str]
) -> list[str]:
    return _unique_strings(
        [
            *interface.name_candidates,
            *[str(value) for value in interface.ip_candidates],
            *invalid_values,
            *([interface.raw_ip_text] if interface.raw_ip_text else []),
        ]
    )


def _ip_candidate_values(
    interface: ObservedInterface, invalid_values: Sequence[str]
) -> list[str]:
    return _unique_strings(
        [
            *[str(value) for value in interface.ip_candidates],
            *invalid_values,
            *([interface.raw_ip_text] if interface.raw_ip_text else []),
        ]
    )


def _require_unique_temporary_ids(values: Sequence[str], label: str) -> None:
    checked = [value for value in values if isinstance(value, str) and value.strip()]
    if len(checked) != len(set(checked)):
        raise ModelInvocationError(f"{label} contain duplicate temporary IDs")


def _unique_option_values(values: Sequence[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        marker = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
        if marker not in seen:
            seen.add(marker)
            result.append(value)
    return result


def _conflict_signature(
    kind: _ConflictType,
    targets: Sequence[str],
    values: Sequence[Any],
    field_name: str | None,
) -> str:
    return json.dumps(
        {
            "kind": kind.value,
            "targets": list(targets),
            "values": list(values),
            "field": field_name,
        },
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )


def _candidate_option(
    value: Any,
    source_stage: str,
    source_view_id: str,
    confidence: float,
    evidence_ids: Sequence[str],
    evidence: dict[str, Evidence],
) -> _CandidateOption:
    text = str(value)
    matching = [
        evidence_id
        for evidence_id in evidence_ids
        if evidence_id in evidence and evidence[evidence_id].raw_text == text
    ]
    selected_ids = matching or list(evidence_ids)
    selected_view = source_view_id
    selected_confidence = confidence
    if matching:
        selected_view = evidence[matching[0]].source_view_id or source_view_id
        selected_confidence = max(evidence[item].confidence for item in matching)
        source_stage = _stage_for_view(selected_view)
    elif source_view_id in {"global_structure", "global_links", "global_text"}:
        source_stage = _stage_for_view(source_view_id)
    return _CandidateOption(
        value=value,
        source_stage=source_stage,
        source_view_id=selected_view,
        confidence=selected_confidence,
        evidence_ids=_unique_strings(selected_ids),
    )


def _stage_for_view(view_id: str) -> str:
    return {
        "global_structure": "structure",
        "global_links": "links",
        "global_text": "text",
    }.get(view_id, "fusion")


def _default_view_for_conflict(kind: _ConflictType) -> str:
    if kind in {
        _ConflictType.NODE_TYPE_CONFLICT,
        _ConflictType.NODE_DUPLICATION_AMBIGUITY,
        _ConflictType.REGION_MEMBERSHIP_AMBIGUITY,
    }:
        return "global_structure"
    if kind in {
        _ConflictType.LINK_ENDPOINT_AMBIGUITY,
        _ConflictType.CROSSING_OR_CONNECTION_AMBIGUITY,
    }:
        return "global_links"
    return "global_text"


def _pending_spatial_context(item: _PendingBinding) -> dict[str, Any]:
    if item.interface is None:
        return {}
    return {
        "labelBBox": item.interface.label_bbox.model_dump(by_alias=True)
        if item.interface.label_bbox
        else None,
        "ipBBox": item.interface.ip_bbox.model_dump(by_alias=True)
        if item.interface.ip_bbox
        else None,
    }


def _parse_ip_candidates(values: Sequence[str]) -> tuple[list[IPv4Address | IPv4Interface], list[str]]:
    parsed: list[IPv4Address | IPv4Interface] = []
    invalid: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        raw = value.strip()
        try:
            parsed_value: IPv4Address | IPv4Interface
            parsed_value = ip_interface(raw) if "/" in raw else ip_address(raw)
            if not isinstance(parsed_value, (IPv4Address, IPv4Interface)):
                invalid.append(raw)
            elif parsed_value not in parsed:
                parsed.append(parsed_value)
        except ValueError:
            invalid.append(raw)
    return parsed, invalid


def _is_bare_ipv4(value: IPv4Address | IPv4Interface) -> bool:
    return isinstance(value, IPv4Address) and not isinstance(value, IPv4Interface)


def _make_observed_interface(
    item: _InterfaceTextObservation,
    view: ImageView,
    links: Sequence[_LinkState],
    evidence_ids: Sequence[str],
    interface_id: str,
) -> tuple[ObservedInterface, list[str], list[str]]:
    interface_names, raw_ip_values, raw_prefixes = _split_interface_text_values(item)
    explicit_ip_values = [
        value
        for value in item.ipv4_candidates
        if isinstance(value, str)
        and "\n" not in value
        and "\r" not in value
        and "." in value
    ]
    ip_values, invalid_values = _parse_ip_candidates(
        _unique_strings([*raw_ip_values, *explicit_ip_values])
    )
    combined_values, combined_invalid = _combine_prefix_candidates(
        _unique_strings([*raw_ip_values, *explicit_ip_values]),
        _unique_ints([*item.prefix_length_candidates, *raw_prefixes]),
    )
    values = _unique_ip_values([*ip_values, *combined_values])
    if len(_unique_strings(item.ipv4_candidates)) == 1 and len(
        _unique_ints(item.prefix_length_candidates)
    ) == 1:
        values = _drop_redundant_bare_ip(values)
    invalid = _unique_strings([*invalid_values, *combined_invalid])
    nearby_link_ids, unknown_link_ids = _resolve_link_ids(
        item.nearby_link_ids, links
    )
    interface = ObservedInterface(
        observation_id=interface_id,
        node_candidates=[],
        raw_name=interface_names[0] if interface_names else None,
        name_candidates=interface_names,
        raw_ip_text=_merge_raw_ip_text(*raw_ip_values),
        ip_candidates=values,
        label_bbox=(
            _text_part_bbox(view, item.label_bbox, item.raw_text, "label")
            if item.label_bbox is not None
            and item.ip_bbox is None
            and interface_names
            and raw_ip_values
            else _map_optional_bbox(view, item.label_bbox)
        ),
        ip_bbox=(
            _map_optional_bbox(view, item.ip_bbox)
            if item.ip_bbox is not None
            else (
                _text_part_bbox(view, item.label_bbox, item.raw_text, "ip")
                if raw_ip_values
                else None
            )
        ),
        nearby_link_ids=nearby_link_ids,
        confidence=item.confidence,
        evidence_ids=_unique_strings(evidence_ids),
    )
    return interface, invalid, unknown_link_ids


def _expand_grouped_interface_text_observation(
    item: _InterfaceTextObservation,
) -> list[_InterfaceTextObservation]:
    """Split a compact per-node text block into one observation per visible fact."""

    if not isinstance(item.raw_text, str) or "\n" not in item.raw_text:
        return [item]

    parsed: list[tuple[str, list[str], list[str], list[int]]] = []
    for raw_line in item.raw_text.splitlines():
        line = " ".join(raw_line.split()).strip()
        if not line:
            continue
        names, ip_values, prefixes = _interface_line_values(line)
        if names or ip_values:
            parsed.append((line, names, ip_values, prefixes))
    if len(parsed) <= 1:
        return [item]

    grouped: list[tuple[str, list[str], list[str], list[int]]] = []
    index = 0
    while index < len(parsed):
        raw_line, names, ip_values, prefixes = parsed[index]
        if (
            names
            and not ip_values
            and index + 1 < len(parsed)
            and not parsed[index + 1][1]
            and parsed[index + 1][2]
        ):
            next_line, _, next_ips, next_prefixes = parsed[index + 1]
            grouped.append(
                (
                    f"{raw_line}\n{next_line}",
                    names,
                    next_ips,
                    _unique_ints([*prefixes, *next_prefixes]),
                )
            )
            index += 2
            continue
        grouped.append((raw_line, names, ip_values, prefixes))
        index += 1

    result: list[_InterfaceTextObservation] = []
    for raw_line, names, ip_values, prefixes in grouped:
        if len(grouped) == 1 and not names and len(item.interface_name_candidates) == 1:
            names = list(item.interface_name_candidates)
        # Rebuild with model_copy, not the constructor: after _text_response_in_view
        # the bbox fields hold view-space BoundingBox values that the validating
        # constructor would reject against their _ModelBoundingBox annotation.
        result.append(
            item.model_copy(
                update={
                    "raw_text": raw_line,
                    "interface_name_candidates": names,
                    "ipv4_candidates": ip_values,
                    "prefix_length_candidates": prefixes,
                }
            )
        )
    return result or [item]


def _interface_line_values(value: str) -> tuple[list[str], list[str], list[int]]:
    ip_values = _extract_ip_tokens([value])
    prefixes: list[int] = []
    for token in ip_values:
        if "/" not in token:
            continue
        try:
            prefixes.append(int(token.rsplit("/", 1)[1]))
        except ValueError:
            continue
    names: list[str] = []
    for part in re.split(r"[;,|]", value):
        cleaned = _IP_TOKEN_RE.sub("", part).strip(" ,;:-")
        cleaned = " ".join(cleaned.split())
        if not cleaned:
            continue
        if _INTERFACE_NAME_RE.fullmatch(cleaned):
            names.append(cleaned)
            continue
        _, suffix = _split_interface_label(cleaned)
        if suffix is not None:
            names.append(suffix)
    return _unique_strings(names), _unique_strings(ip_values), _unique_ints(prefixes)


_IP_TOKEN_RE = re.compile(
    r"(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?:/[0-9]{1,2})?(?![0-9.])"
)
_INTERFACE_NAME_RE = re.compile(
    r"^(?:eth|ens|eno|enp|em|ge|gi|gigabitethernet|fa|fe|te|xe|xge|"
    r"ten-gigabitethernet|port-channel|po|lo|bond|e)\s*[-/:.]?[a-z0-9./:-]*$",
    re.IGNORECASE,
)


def _first_ip_text(raw_text: str | None, candidates: Sequence[str]) -> str | None:
    values = _extract_ip_tokens([raw_text] if raw_text else [])
    values.extend(
        token
        for token in _extract_ip_tokens(candidates)
        if token not in values
    )
    return values[0] if values else None


def _split_interface_text_values(
    item: _InterfaceTextObservation,
) -> tuple[list[str], list[str], list[int]]:
    """Separate mixed interface text into names, IPv4 tokens, and prefixes."""

    values = [
        value
        for value in [item.raw_text, *item.interface_name_candidates, *item.ipv4_candidates]
        if isinstance(value, str) and value.strip()
    ]
    ip_values = _extract_ip_tokens(values)
    prefixes = _unique_ints(item.prefix_length_candidates)
    for token in ip_values:
        if "/" not in token:
            continue
        try:
            prefixes.append(int(token.rsplit("/", 1)[1]))
        except ValueError:
            continue
    names: list[str] = []
    for value in values:
        for line in value.splitlines():
            cleaned = _IP_TOKEN_RE.sub("", line).strip(" ,;:-")
            cleaned = " ".join(cleaned.split())
            if not cleaned:
                continue
            if _INTERFACE_NAME_RE.fullmatch(cleaned):
                names.append(cleaned)
                continue
            _, suffix = _split_interface_label(cleaned)
            if suffix is not None:
                names.append(suffix)
    return _unique_strings(names), _unique_strings(ip_values), _unique_ints(prefixes)


def _extract_ip_tokens(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        for match in _IP_TOKEN_RE.finditer(value):
            token = match.group(0)
            try:
                parsed = ip_interface(token) if "/" in token else ip_address(token)
            except ValueError:
                continue
            if isinstance(parsed, IPv4Address | IPv4Interface) and token not in result:
                result.append(token)
    return result


def _text_coverage_node_refs(nodes: Sequence[_NodeState]) -> list[str]:
    # Text is allowed to be absent, but the pass must explicitly inspect every
    # structure node so that all L3 devices and every visible-IP endpoint are
    # necessarily covered.
    return [node.ref for node in nodes]


def _split_interface_label(
    value: str,
    node_names: Sequence[str] = (),
) -> tuple[str | None, str | None]:
    cleaned = " ".join(value.split()).strip(" ,;:")
    if not cleaned:
        return None, None
    if _INTERFACE_NAME_RE.fullmatch(cleaned):
        return None, cleaned
    ordered_names = sorted(
        _unique_strings(node_names), key=lambda item: len(item), reverse=True
    )
    folded = cleaned.casefold()
    for node_name in ordered_names:
        prefix = node_name.strip()
        for separator in ("-", "/", " "):
            candidate_prefix = f"{prefix}{separator}"
            if folded.startswith(candidate_prefix.casefold()):
                suffix = cleaned[len(candidate_prefix) :].strip()
                if _INTERFACE_NAME_RE.fullmatch(suffix):
                    return prefix, suffix
    match = re.match(
        r"^(?P<node>.+?)[-_](?P<interface>(?:eth|ens|eno|enp|em|ge|gi|"
        r"gigabitethernet|fa|fe|te|xe|xge|port-channel|po|lo|bond|e)"
        r"\s*[-/:.]?[a-z0-9./:-]*)$",
        cleaned,
        re.IGNORECASE,
    )
    if match and _INTERFACE_NAME_RE.fullmatch(match.group("interface")):
        return match.group("node").strip(), match.group("interface").strip()
    return None, None


def _split_node_text_observation(
    item: _NodeTextObservation,
    nodes: Sequence[_NodeState],
) -> tuple[list[str], list[str], list[str]]:
    known_names = [
        name
        for node in nodes
        for name in node.observation.name_candidates
        if isinstance(name, str) and name.strip()
    ]
    names: list[str] = []
    ip_texts: list[str] = []
    interface_names: list[str] = []
    values = [*item.name_candidates, *([item.raw_text] if item.raw_text else [])]
    for value in values:
        if not isinstance(value, str):
            continue
        for line in value.splitlines():
            line = " ".join(line.split()).strip()
            if not line:
                continue
            tokens = _extract_ip_tokens([line])
            ip_texts.extend(token for token in tokens if token not in ip_texts)
            remainder = _IP_TOKEN_RE.sub("", line).strip(" ,;:-")
            if not remainder:
                continue
            node_part, interface_part = _split_interface_label(
                remainder,
                [*known_names, *names],
            )
            if interface_part is not None:
                interface_names.append(interface_part)
                if node_part:
                    names.append(node_part)
            elif not _INTERFACE_NAME_RE.fullmatch(remainder):
                names.append(remainder)
            else:
                interface_names.append(remainder)
    return _unique_strings(names), _unique_strings(ip_texts), _unique_strings(interface_names)


def _text_part_bbox(
    view: ImageView,
    bbox: BoundingBox | None,
    raw_text: str | None,
    part: Literal["label", "ip"],
) -> BoundingBox | None:
    if bbox is None:
        return None
    lines = [line.strip() for line in (raw_text or "").splitlines() if line.strip()]
    if len(lines) < 2:
        return view_bbox_to_original(view, bbox)
    line_height = bbox.height / len(lines)
    if part == "label":
        height = max(0.001, line_height * max(1, len(lines) - 1))
        return view_bbox_to_original(
            view,
            BoundingBox(x=bbox.x, y=bbox.y, width=bbox.width, height=height),
        )
    return view_bbox_to_original(
        view,
        BoundingBox(
            x=bbox.x,
            y=bbox.y + line_height * max(0, len(lines) - 1),
            width=bbox.width,
            height=max(0.001, line_height),
        ),
    )


def _normalized_interface_name(value: str | None, node: ObservedNode | None = None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    names = [] if node is None else [*node.name_candidates, *([node.raw_name] if node.raw_name else [])]
    _, suffix = _split_interface_label(value, names)
    if suffix is not None:
        return suffix
    cleaned = " ".join(value.split()).strip(" ,;:")
    return cleaned if _INTERFACE_NAME_RE.fullmatch(cleaned) else None


def _interface_key(interface: ObservedInterface) -> tuple[str, str]:
    name = interface.raw_name or (interface.name_candidates[0] if interface.name_candidates else "")
    return (name.casefold().strip(), (interface.raw_ip_text or "").casefold().strip())


def _merge_interfaces(left: ObservedInterface, right: ObservedInterface) -> ObservedInterface:
    return _replace_interface(
        left,
        node_candidates=_unique_strings([*left.node_candidates, *right.node_candidates]),
        raw_name=left.raw_name or right.raw_name,
        name_candidates=_unique_strings([*left.name_candidates, *right.name_candidates]),
        raw_ip_text=_merge_raw_ip_text(left.raw_ip_text, right.raw_ip_text),
        ip_candidates=_unique_ip_values([*left.ip_candidates, *right.ip_candidates]),
        label_bbox=left.label_bbox or right.label_bbox,
        ip_bbox=left.ip_bbox or right.ip_bbox,
        nearby_link_ids=_unique_strings([*left.nearby_link_ids, *right.nearby_link_ids]),
        confidence=max(left.confidence, right.confidence),
        evidence_ids=_unique_strings([*left.evidence_ids, *right.evidence_ids]),
    )


def _merge_raw_ip_text(*values: str | None) -> str | None:
    """Keep each visible IP token once, preferring a token with a prefix."""

    tokens: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        for token in _extract_ip_tokens([value]):
            if token not in tokens:
                tokens.append(token)
    prefixed_ips = {
        token.split("/", 1)[0]
        for token in tokens
        if "/" in token
    }
    return "\n".join(
        token for token in tokens if "/" in token or token not in prefixed_ips
    ) or None


def _upsert_node_interface(node: _NodeState, interface: ObservedInterface) -> ObservedInterface:
    normalized = _normalized_interface_name(
        interface.raw_name or (interface.name_candidates[0] if interface.name_candidates else None),
        node.observation,
    )
    candidate = _replace_interface(
        interface,
        raw_name=normalized or interface.raw_name,
        name_candidates=_unique_strings([normalized, *interface.name_candidates]) if normalized else interface.name_candidates,
        node_candidates=_unique_strings([node.observation.observation_id, *interface.node_candidates]),
    )
    for index, existing in enumerate(node.observation.observed_interfaces):
        existing_name = _normalized_interface_name(
            existing.raw_name or (existing.name_candidates[0] if existing.name_candidates else None),
            node.observation,
        )
        same_name = normalized is not None and existing_name is not None and normalized.casefold() == existing_name.casefold()
        same_unnamed_ip = normalized is None and existing_name is None and bool(
            set(str(value) for value in existing.ip_candidates).intersection(
                str(value) for value in candidate.ip_candidates
            )
        )
        if same_name or same_unnamed_ip:
            merged = _merge_interfaces(existing, candidate)
            node.observation = _replace_node(
                node.observation,
                observed_interfaces=[
                    merged if item.observation_id == existing.observation_id else item
                    for item in node.observation.observed_interfaces
                ],
            )
            return merged
    node.observation = _replace_node(
        node.observation,
        observed_interfaces=[*node.observation.observed_interfaces, candidate],
    )
    return candidate


def _attach_text_interface_facts(
    node: _NodeState,
    *,
    ip_texts: Sequence[str],
    interface_names: Sequence[str],
    raw_text: str | None,
    label_bbox: BoundingBox | None,
    ip_bbox: BoundingBox | None,
    evidence_ids: Sequence[str],
    confidence: float,
    view: ImageView,
    links: Sequence[_LinkState],
    unresolved: list[UnresolvedItem],
    all_nodes: Sequence[_NodeState],
) -> None:
    if not ip_texts and not interface_names:
        return
    names = list(interface_names)
    existing_named = [
        item
        for item in node.observation.observed_interfaces
        if item.raw_name or item.name_candidates
    ]
    if not names and len(existing_named) == 1:
        names = [existing_named[0].raw_name or existing_named[0].name_candidates[0]]
    if not names:
        names = [""]
    next_interface = _next_interface_number(all_nodes)
    for index, ip_text in enumerate(ip_texts):
        parsed, invalid = _parse_ip_candidates([ip_text])
        name = names[index] if index < len(names) and names[index] else None
        interface_id = f"obs_if_{next_interface:03d}"
        next_interface += 1
        interface = ObservedInterface(
            observation_id=interface_id,
            node_candidates=[node.observation.observation_id],
            raw_name=name,
            name_candidates=_unique_strings([name] if name else []),
            raw_ip_text=ip_text,
            ip_candidates=parsed,
            label_bbox=(
                _map_optional_bbox(view, label_bbox)
                if ip_bbox is not None
                else _text_part_bbox(view, label_bbox, raw_text, "label")
            )
            if name
            else None,
            ip_bbox=(
                _map_optional_bbox(view, ip_bbox)
                if ip_bbox is not None
                else _text_part_bbox(view, label_bbox, raw_text, "ip")
            ),
            nearby_link_ids=_nearby_link_ids_for_inline_interface(
                node,
                name,
                links,
            ),
            confidence=confidence,
            evidence_ids=_unique_strings(evidence_ids),
        )
        interface = _upsert_node_interface(node, interface)
        if invalid:
            unresolved.append(
                _make_binding_unresolved(
                    "IP_INTERFACE_BINDING_AMBIGUITY",
                    [interface.observation_id],
                    "preserved unreadable IPv4 candidate",
                    evidence_ids,
                    candidates=invalid,
                    field="ipCandidates",
                )
            )
        if not name:
            unresolved.append(
                _make_binding_unresolved(
                    "INTERFACE_NAME_MISSING",
                    [interface.observation_id],
                    "IPv4 was visible but no interface name was visible in the same text observation",
                    evidence_ids,
                    candidates=[ip_text],
                    field="rawName",
                )
            )
        if parsed and all(_is_bare_ipv4(value) for value in parsed):
            unresolved.append(
                _make_binding_unresolved(
                    "UNKNOWN_PREFIX",
                    [interface.observation_id],
                    "IPv4 text has no visible prefix candidate",
                    evidence_ids,
                    candidates=[ip_text],
                    field="ipCandidates",
                )
            )
    for name in names[len(ip_texts) :]:
        if not name:
            continue
        interface_id = f"obs_if_{next_interface:03d}"
        next_interface += 1
        _upsert_node_interface(
            node,
            ObservedInterface(
                observation_id=interface_id,
                node_candidates=[node.observation.observation_id],
                raw_name=name,
                name_candidates=_unique_strings([name]),
                raw_ip_text=None,
                ip_candidates=[],
                nearby_link_ids=_nearby_link_ids_for_inline_interface(
                    node,
                    name,
                    links,
                ),
                confidence=confidence,
                evidence_ids=_unique_strings(evidence_ids),
            ),
        )


def _nearby_link_ids_for_inline_interface(
    node: _NodeState,
    interface_name: str | None,
    links: Sequence[_LinkState],
) -> list[str]:
    """Bind inline text only when a link label or unique adjacency supports it."""

    node_id = node.observation.observation_id
    normalized = _normalized_interface_name(interface_name, node.observation)
    adjacent: list[str] = []
    matched: list[str] = []
    for link in links:
        if node_id in link.observation.source_node_candidates:
            raw_values = link.raw_source_interface_candidates
        elif node_id in link.observation.target_node_candidates:
            raw_values = link.raw_target_interface_candidates
        else:
            continue
        adjacent.append(link.observation.observation_id)
        if normalized is not None and any(
            _normalized_interface_name(value, node.observation) == normalized
            for value in raw_values
        ):
            matched.append(link.observation.observation_id)
    if matched:
        return _unique_strings(matched)
    return _unique_strings(adjacent) if len(adjacent) == 1 else []


def _combine_prefix_candidates(
    values: Sequence[str], prefixes: Sequence[int]
) -> tuple[list[IPv4Address | IPv4Interface], list[str]]:
    if not prefixes:
        return [], []
    parsed: list[IPv4Address | IPv4Interface] = []
    invalid: list[str] = []
    valid_prefixes = [prefix for prefix in prefixes if isinstance(prefix, int) and not isinstance(prefix, bool) and 0 <= prefix <= 32]
    for value in values:
        if not isinstance(value, str) or "/" in value:
            continue
        raw = value.strip()
        try:
            address = ip_address(raw)
        except ValueError:
            continue
        if not isinstance(address, IPv4Address):
            invalid.append(raw)
            continue
        for prefix in valid_prefixes:
            candidate = f"{address}/{prefix}"
            try:
                parsed_value = ip_interface(candidate)
            except ValueError:
                invalid.append(candidate)
                continue
            if parsed_value not in parsed:
                parsed.append(parsed_value)
    return parsed, invalid


def _unique_ip_values(values: Sequence[IPv4Address | IPv4Interface]) -> list[IPv4Address | IPv4Interface]:
    result: list[IPv4Address | IPv4Interface] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _drop_redundant_bare_ip(
    values: Sequence[IPv4Address | IPv4Interface],
) -> list[IPv4Address | IPv4Interface]:
    prefixed_addresses = {
        value.ip for value in values if isinstance(value, IPv4Interface)
    }
    return [
        value
        for value in values
        if not (
            isinstance(value, IPv4Address)
            and value in prefixed_addresses
        )
    ]


def _unique_ints(values: Sequence[int]) -> list[int]:
    result: list[int] = []
    for value in values:
        if isinstance(value, int) and not isinstance(value, bool) and value not in result:
            result.append(value)
    return result


def _map_optional_bbox(view: ImageView, bbox: BoundingBox | None) -> BoundingBox | None:
    return view_bbox_to_original(view, bbox) if bbox is not None else None


def _obvious_duplicate(bbox: BoundingBox, center: Point, nodes: Sequence[_NodeState], view: ImageView) -> _NodeState | None:
    for node in nodes:
        existing = node.view_bbox
        if _bbox_iou(existing, bbox) >= 0.65:
            return node
        dx = abs(node.view_center.x - center.x)
        dy = abs(node.view_center.y - center.y)
        if dx <= max(existing.width, bbox.width) * 0.25 and dy <= max(existing.height, bbox.height) * 0.25:
            return node
    return None


def _bbox_iou(left: BoundingBox, right: BoundingBox) -> float:
    x1, y1 = max(left.x, right.x), max(left.y, right.y)
    x2, y2 = min(left.x + left.width, right.x + right.width), min(left.y + left.height, right.y + right.height)
    if x2 <= x1 or y2 <= y1:
        return 0.0
    intersection = (x2 - x1) * (y2 - y1)
    union = left.width * left.height + right.width * right.height - intersection
    return intersection / union if union > 0 else 0.0


def _point_in_bbox(point: Point, bbox: BoundingBox) -> bool:
    return (
        bbox.x <= point.x <= bbox.x + bbox.width
        and bbox.y <= point.y <= bbox.y + bbox.height
    )


def _polyline_bbox(
    polyline: Sequence[Point], view: ImageView
) -> BoundingBox | None:
    if not polyline:
        return None
    xs = [point.x for point in polyline]
    ys = [point.y for point in polyline]
    left, right = min(xs), max(xs)
    top, bottom = min(ys), max(ys)
    if right <= left:
        left = max(0.0, left - 0.5)
        right = min(float(view.width), right + 0.5)
    if bottom <= top:
        top = max(0.0, top - 0.5)
        bottom = min(float(view.height), bottom + 0.5)
    if right <= left or bottom <= top:
        return None
    return BoundingBox(x=left, y=top, width=right - left, height=bottom - top)


def _unique_strings(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip() and value.strip() not in result:
            result.append(value.strip())
    return result


def _normalized_name_value(value: str) -> str:
    return " ".join(value.split()).strip().casefold()


def _name_without_ip(value: str) -> str:
    lines: list[str] = []
    for line in value.splitlines():
        remainder = _IP_TOKEN_RE.sub("", line).strip(" ,;:-")
        if remainder:
            lines.append(" ".join(remainder.split()))
    return " ".join(lines).strip()


def _clean_node_name_candidates(values: Sequence[str]) -> list[str]:
    return _unique_strings(
        [cleaned for value in values if isinstance(value, str) and (cleaned := _name_without_ip(value))]
    )


def _sanitize_node_names(nodes: Sequence[_NodeState]) -> None:
    for node in nodes:
        names = _clean_node_name_candidates(node.observation.name_candidates)
        node.observation = _replace_node(
            node.observation,
            raw_name=names[0] if names else None,
            name_candidates=names,
        )


def _candidate_relation(left: str, right: str) -> str:
    left_normalized = _normalized_name_value(left)
    right_normalized = _normalized_name_value(right)
    if left_normalized == right_normalized:
        return "EQUAL"
    if _name_without_ip(left).casefold() == _name_without_ip(right).casefold():
        return "NAME_PLUS_IP"
    if left_normalized in right_normalized or right_normalized in left_normalized:
        return "CONTAINS"
    if _name_without_ip(left).casefold() == left_normalized and _name_without_ip(right).casefold() == right_normalized:
        return "TRUE_CONFLICT"
    return "TRUE_CONFLICT"


def _name_conflict_values(values: Sequence[str]) -> list[str]:
    cleaned = _unique_strings(
        [_name_without_ip(value) for value in values if isinstance(value, str)]
    )
    normalized: list[str] = []
    representatives: list[str] = []
    for value in cleaned:
        marker = _normalized_name_value(value)
        if marker not in normalized:
            normalized.append(marker)
            representatives.append(value)
    for index, left in enumerate(representatives):
        for right in representatives[index + 1 :]:
            if _candidate_relation(left, right) == "TRUE_CONFLICT":
                return representatives
    return []


def _unique_types(values: Sequence[SemanticDeviceType]) -> list[SemanticDeviceType]:
    result: list[SemanticDeviceType] = []
    for value in values:
        if isinstance(value, SemanticDeviceType) and value not in result:
            result.append(value)
    return result


def _resolve_ref(value: str, aliases: dict[str, str], valid: set[str]) -> str | None:
    if value in valid:
        return value
    return aliases.get(value)


def _resolve_refs(values: Sequence[str], aliases: dict[str, str], valid: set[str]) -> list[str]:
    return _unique_strings([resolved for value in values if (resolved := _resolve_ref(value, aliases, valid)) is not None])


def _all_temp_refs(values: Sequence[_NodeState] | Sequence[_RegionState]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for value in values:
        for temporary in value.temporary_ids:
            aliases[temporary] = value.ref
        aliases[value.ref] = value.ref
        aliases[value.observation.observation_id] = value.ref
    return aliases


def _node_by_ref(nodes: Sequence[_NodeState], ref: str) -> _NodeState:
    for node in nodes:
        if node.ref == ref:
            return node
    raise ModelInvocationError(f"unknown node reference {ref}")


def _region_by_ref(regions: Sequence[_RegionState], ref: str) -> _RegionState:
    for region in regions:
        if region.ref == ref:
            return region
    raise ModelInvocationError(f"unknown region reference {ref}")


def _region_by_observation_id(
    regions: Sequence[_RegionState], value: str
) -> _RegionState:
    for region in regions:
        if region.observation.observation_id == value:
            return region
    raise ModelInvocationError(f"unknown region observation {value}")


def _node_by_observation_id(nodes: Sequence[_NodeState], value: str) -> _NodeState:
    for node in nodes:
        if node.observation.observation_id == value:
            return node
    raise ModelInvocationError(f"unknown node observation {value}")


def _link_by_id(links: Sequence[_LinkState], value: str) -> _LinkState:
    for link in links:
        if link.observation.observation_id == value:
            return link
    raise ModelInvocationError(f"unknown link observation {value}")


def _optional_interface_by_id(
    nodes: Sequence[_NodeState], value: str
) -> tuple[_NodeState, ObservedInterface] | None:
    for node in nodes:
        for interface in node.observation.observed_interfaces:
            if interface.observation_id == value:
                return node, interface
    return None


def _interface_by_id(
    nodes: Sequence[_NodeState], value: str
) -> tuple[_NodeState, ObservedInterface]:
    result = _optional_interface_by_id(nodes, value)
    if result is None:
        raise ModelInvocationError(f"unknown interface observation {value}")
    return result


def _node_obs_id(nodes: Sequence[_NodeState], ref: str) -> str:
    return _node_by_ref(nodes, ref).observation.observation_id


def _resolve_link_ids(
    values: Sequence[str], links: Sequence[_LinkState]
) -> tuple[list[str], list[str]]:
    valid = {link.ref: link.observation.observation_id for link in links}
    valid.update({link.temporary_id: link.observation.observation_id for link in links})
    valid.update({link.observation.observation_id: link.observation.observation_id for link in links})
    return (
        _unique_strings([valid[value] for value in values if value in valid]),
        _unique_strings([value for value in values if value not in valid]),
    )


def _replace_ref(values: Sequence[str], old: str, new: str) -> list[str]:
    return _unique_strings([new if value == old else value for value in values])


def _next_interface_number(
    nodes: Sequence[_NodeState],
    extra_interfaces: Sequence[ObservedInterface] = (),
) -> int:
    values = [
        int(interface.observation_id.rsplit("_", 1)[-1])
        for interface in [
            *(interface for node in nodes for interface in node.observation.observed_interfaces),
            *extra_interfaces,
        ]
        if interface.observation_id.startswith("obs_if_")
        and interface.observation_id.rsplit("_", 1)[-1].isdigit()
    ]
    return max(values, default=0) + 1


def _replace_node(node: ObservedNode, **changes: Any) -> ObservedNode:
    values = node.model_dump()
    values.update(changes)
    return ObservedNode.model_validate(values)


def _replace_region(region: ObservedRegion, **changes: Any) -> ObservedRegion:
    values = region.model_dump()
    values.update(changes)
    return ObservedRegion.model_validate(values)


def _replace_link(link: ObservedLink, **changes: Any) -> ObservedLink:
    values = link.model_dump()
    values.update(changes)
    return ObservedLink.model_validate(values)


def _replace_interface(
    interface: ObservedInterface, **changes: Any
) -> ObservedInterface:
    values = interface.model_dump()
    values.update(changes)
    return ObservedInterface.model_validate(values)


def _validate_references(values: Sequence[str], valid: set[str]) -> None:
    if any(value not in valid for value in values):
        raise ModelInvocationError("observation references unknown evidence")


def _fusion_node(node: _NodeState) -> dict[str, Any]:
    return {"id": node.observation.observation_id, "stableNumber": node.ref, "bbox": node.observation.bbox.model_dump(by_alias=True), "center": node.observation.center.model_dump(by_alias=True), "nameCandidates": node.observation.name_candidates, "typeCandidates": [item.value for item in node.observation.type_candidates], "evidenceIds": node.observation.evidence_ids}


def _fusion_interface(node: _NodeState, interface: ObservedInterface) -> dict[str, Any]:
    return {"id": interface.observation_id, "nodeId": node.observation.observation_id, "nodeCandidates": interface.node_candidates, "rawName": interface.raw_name, "nameCandidates": interface.name_candidates, "rawIpText": interface.raw_ip_text, "ipCandidates": [str(item) for item in interface.ip_candidates], "evidenceIds": interface.evidence_ids}


def _fusion_pending_binding(item: _PendingBinding) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": item.binding_id,
        "kind": item.kind,
        "targetCandidates": item.target_ids,
        "rawText": item.raw_text,
        "nameCandidates": item.names,
        "candidateValues": item.candidate_values,
        "confidence": item.confidence,
        "evidenceIds": item.evidence_ids,
    }
    if item.interface is not None:
        value.update(
            {
                "interfaceId": item.interface.observation_id,
                "ipCandidates": [str(candidate) for candidate in item.interface.ip_candidates],
                "labelBBox": item.interface.label_bbox.model_dump(by_alias=True)
                if item.interface.label_bbox
                else None,
                "ipBBox": item.interface.ip_bbox.model_dump(by_alias=True)
                if item.interface.ip_bbox
                else None,
            }
        )
    return value


def _fusion_link(link: _LinkState) -> dict[str, Any]:
    return {"id": link.observation.observation_id, "sourceNodeCandidates": link.observation.source_node_candidates, "targetNodeCandidates": link.observation.target_node_candidates, "sourceInterfaceCandidates": link.observation.source_interface_candidates, "targetInterfaceCandidates": link.observation.target_interface_candidates, "polyline": [point.model_dump(by_alias=True) for point in link.observation.polyline], "confidence": link.observation.confidence, "evidenceIds": link.observation.evidence_ids}


def _fusion_region(region: _RegionState) -> dict[str, Any]:
    return {"id": region.observation.observation_id, "stableNumber": region.ref, "bbox": region.observation.bbox.model_dump(by_alias=True), "nameCandidates": region.observation.name_candidates, "memberNodeCandidates": region.observation.member_node_candidates, "evidenceIds": region.observation.evidence_ids}


def _fusion_conflict(
    conflict: _Conflict, evidence: dict[str, Evidence]
) -> dict[str, Any]:
    return {
        "conflictId": conflict.conflict_id,
        "conflictType": conflict.conflict_type.value,
        "targetIds": conflict.target_ids,
        "field": conflict.field_name,
        "candidateOptions": [
            {
                "candidateIndex": index,
                "value": option.value,
                "sourceStage": option.source_stage,
                "sourceViewId": option.source_view_id,
                "confidence": option.confidence,
                "evidenceIds": option.evidence_ids,
                "evidence": [
                    _fusion_evidence(evidence_id, evidence[evidence_id])
                    for evidence_id in option.evidence_ids
                    if evidence_id in evidence
                ],
            }
            for index, option in enumerate(conflict.candidate_options)
        ],
        "supportingEvidenceIds": conflict.supporting_evidence_ids,
        "spatialContext": conflict.spatial_context,
    }


def _fusion_evidence(evidence_id: str, item: Evidence) -> dict[str, Any]:
    return {
        "id": evidence_id,
        "sourceType": item.source_type.value,
        "sourceViewId": item.source_view_id,
        "bbox": item.bbox.model_dump(by_alias=True) if item.bbox else None,
        "rawText": item.raw_text,
        "description": item.description,
        "confidence": item.confidence,
    }


def _write_new_http_attempts(
    artifacts: _RecognitionRunArtifacts,
    client: OpenAICompatibleModelClient,
    start_index: int,
) -> None:
    attempts = client.http_attempts
    if start_index < len(attempts):
        artifacts.write_http_attempts(attempts[start_index:])


def _last_stage_usage(client: OpenAICompatibleModelClient, stage: str) -> ModelUsage:
    values = getattr(client, "last_stage_usages", {})
    usage = values.get(stage) if isinstance(values, dict) else None
    return usage if isinstance(usage, ModelUsage) else ModelUsage()


def _client_logical_call_count(client: OpenAICompatibleModelClient) -> int:
    usage = getattr(client, "usage", None)
    if isinstance(usage, ModelUsage):
        return usage.logical_call_count
    raise ModelInvocationError("model client does not expose logical-call usage")


def _model_usage_delta(after: ModelUsage, before: ModelUsage) -> ModelUsage:
    return ModelUsage(
        request_count=max(0, after.request_count - before.request_count),
        prompt_tokens=max(0, after.prompt_tokens - before.prompt_tokens),
        completion_tokens=max(
            0, after.completion_tokens - before.completion_tokens
        ),
        total_tokens=max(0, after.total_tokens - before.total_tokens),
        cached_tokens=max(0, after.cached_tokens - before.cached_tokens),
        logical_call_count=max(
            0, after.logical_call_count - before.logical_call_count
        ),
    )


def _record_stage_usage(
    client: OpenAICompatibleModelClient, stage: str, usage: ModelUsage
) -> None:
    values = getattr(client, "last_stage_usages", None)
    if not isinstance(values, dict):
        values = {}
        client.last_stage_usages = values
    values[stage] = usage


def _make_statistics(
    structure: ModelUsage,
    links: ModelUsage,
    text: ModelUsage,
    fusion: ModelUsage,
    node_count: int,
    additional_count: int,
    text_count: int,
    conflict_count: int,
    decision_count: int,
    rejected_count: int,
    unresolved_count: int,
    workflow_logical_calls: int,
) -> RecognitionStatistics:
    stage_logical_calls = (
        structure.logical_call_count,
        links.logical_call_count,
        text.logical_call_count,
        fusion.logical_call_count,
    )
    if stage_logical_calls != (1, 1, 1, 1):
        raise ModelInvocationError("each recognition stage must consume exactly one logical model call")
    if workflow_logical_calls != 4:
        raise ModelInvocationError("recognition workflow must consume exactly four logical model calls")
    return RecognitionStatistics(
        structure_logical_calls=structure.logical_call_count,
        links_logical_calls=links.logical_call_count,
        text_logical_calls=text.logical_call_count,
        fusion_logical_calls=fusion.logical_call_count,
        structure_http_requests=structure.request_count,
        links_http_requests=links.request_count,
        text_http_requests=text.request_count,
        fusion_http_requests=fusion.request_count,
        structure_input_tokens=structure.prompt_tokens,
        structure_output_tokens=structure.completion_tokens,
        links_input_tokens=links.prompt_tokens,
        links_output_tokens=links.completion_tokens,
        text_input_tokens=text.prompt_tokens,
        text_output_tokens=text.completion_tokens,
        fusion_input_tokens=fusion.prompt_tokens,
        fusion_output_tokens=fusion.completion_tokens,
        node_count_after_pass1=node_count,
        additional_node_count_after_pass2=additional_count,
        text_observation_count=text_count,
        fusion_conflict_count=conflict_count,
        fusion_decision_count=decision_count,
        rejected_fusion_decision_count=rejected_count,
        final_unresolved_count=unresolved_count,
    )
