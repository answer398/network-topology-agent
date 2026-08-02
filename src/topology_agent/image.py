"""Image normalization, complete-image views, and coordinate conversion."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Sequence

from PIL import Image, ImageDraw, ImageEnhance, ImageOps, UnidentifiedImageError

from .config import ImageProcessingConfig
from .models import BoundingBox, ImageInfo, InputError, Point


_ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg"}
_ALLOWED_FORMATS = {"PNG", "JPEG"}
_VIEW_IDS = {"original", "global_structure", "global_links", "global_text"}
_VIEW_KINDS = {
    "original": "ORIGINAL",
    "global_structure": "STRUCTURE",
    "global_links": "LINKS",
    "global_text": "TEXT",
}
_GEOMETRY_EPSILON = 1e-6


@dataclass(slots=True)
class ImageView:
    """An RGB complete-image view with a linear original-image mapping."""

    view_id: str
    kind: Literal["ORIGINAL", "STRUCTURE", "LINKS", "TEXT"]
    image: Image.Image
    original_bounds: BoundingBox
    width: int = field(init=False)
    height: int = field(init=False)
    scale_x: float = field(init=False)
    scale_y: float = field(init=False)

    def __post_init__(self) -> None:
        if self.view_id not in _VIEW_IDS:
            raise InputError(f"invalid image view ID: {self.view_id!r}")
        if self.kind not in {"ORIGINAL", "STRUCTURE", "LINKS", "TEXT"}:
            raise InputError(f"invalid image view kind: {self.kind!r}")
        if _VIEW_KINDS[self.view_id] != self.kind:
            raise InputError(
                f"image view {self.view_id!r} has incompatible kind {self.kind!r}"
            )
        if not isinstance(self.image, Image.Image) or self.image.mode != "RGB":
            raise InputError(f"view {self.view_id!r} must contain an RGB image")
        self.width, self.height = self.image.size
        if self.width <= 0 or self.height <= 0:
            raise InputError(f"view {self.view_id!r} has an invalid image size")
        _validate_bbox(self.original_bounds, f"view {self.view_id!r} bounds")
        self.scale_x = self.original_bounds.width / self.width
        self.scale_y = self.original_bounds.height / self.height


@dataclass(slots=True)
class ImageBundle:
    """The normalized source and the three complete-image runtime views."""

    source_path: Path
    image_info: ImageInfo
    sha256: str
    original_view: ImageView
    structure_view: ImageView
    text_enhanced_view: ImageView
    _normalized_image: Image.Image = field(repr=False)
    _links_view: ImageView | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _validate_bundle_consistency(self)

    @property
    def original_image(self) -> Image.Image:
        """Return the retained EXIF-corrected RGB source image."""

        return self._normalized_image

    def get_view(self, view_id: str) -> ImageView:
        """Return a registered view by its deterministic ID."""

        if not isinstance(view_id, str) or view_id not in _VIEW_IDS:
            raise InputError(f"unknown image view ID: {view_id!r}")
        if view_id == self.original_view.view_id:
            return self.original_view
        if view_id == self.structure_view.view_id:
            return self.structure_view
        if view_id == self.text_enhanced_view.view_id:
            return self.text_enhanced_view
        if self._links_view is not None and view_id == self._links_view.view_id:
            return self._links_view
        raise InputError(f"image view {view_id!r} has not been registered")

    @property
    def links_view(self) -> ImageView | None:
        """Return the numbered complete-image view after it is registered."""

        return self._links_view

    def register_links_view(
        self,
        node_annotations: Sequence[tuple[str, BoundingBox, Point]],
        region_annotations: Sequence[tuple[str, BoundingBox]],
    ) -> ImageView:
        """Draw deterministic node and region labels onto a complete image."""

        if self._links_view is not None:
            raise InputError("global_links is already registered")
        canvas = self.structure_view.image.copy()
        draw = ImageDraw.Draw(canvas)
        annotations = [
            (label, bbox, center, (20, 92, 170))
            for label, bbox, center in node_annotations
        ]
        annotations.extend(
            (label, bbox, None, (150, 90, 20))
            for label, bbox in region_annotations
        )
        occupied_labels: list[tuple[float, float, float, float]] = []
        node_bboxes = [bbox for _, bbox, _ in node_annotations]
        for label, bbox, center, color in annotations:
            _draw_annotation(
                draw,
                bbox,
                label,
                color,
                center,
                occupied_labels=occupied_labels,
                blocked_bboxes=[item for item in node_bboxes if item is not bbox],
            )
        self._links_view = ImageView(
            "global_links", "LINKS", canvas, self.structure_view.original_bounds
        )
        ids = [
            "original",
            "global_structure",
            "global_links",
            "global_text",
        ]
        self.image_info = ImageInfo(
            width=self.image_info.width,
            height=self.image_info.height,
            format=self.image_info.format,
            view_ids=ids,
        )
        _validate_bundle_consistency(self)
        return self._links_view


def load_image_bundle(
    image_path: str | Path,
    image_config: ImageProcessingConfig,
) -> ImageBundle:
    """Decode, orient, normalize, resize, and register the base complete views."""

    source_path = _validate_source_path(image_path)
    if not isinstance(image_config, ImageProcessingConfig):
        raise InputError("imageConfig must be an ImageProcessingConfig")
    max_long_edge = image_config.max_long_edge
    if isinstance(max_long_edge, bool) or not isinstance(max_long_edge, int) or max_long_edge <= 0:
        raise InputError("invalid image.maxLongEdge: expected a positive integer")

    normalized, detected_format = _load_normalized_image(source_path)
    width, height = normalized.size
    if width <= 0 or height <= 0:
        normalized.close()
        raise InputError(f"decoded image has invalid dimensions: {source_path}")

    image_hash = _normalized_image_hash(normalized)
    bounds = BoundingBox(x=0, y=0, width=width, height=height)
    original_view = ImageView("original", "ORIGINAL", normalized, bounds)
    structure_image = _resize_complete(normalized, max_long_edge)
    structure_view = ImageView(
        "global_structure", "STRUCTURE", structure_image, bounds
    )
    text_image = _enhance_text_once(structure_image)
    text_view = ImageView("global_text", "TEXT", text_image, bounds)
    info = ImageInfo(
        width=width,
        height=height,
        format=detected_format,
        view_ids=["original", "global_structure", "global_text"],
    )
    return ImageBundle(
        source_path=source_path,
        image_info=info,
        sha256=image_hash,
        original_view=original_view,
        structure_view=structure_view,
        text_enhanced_view=text_view,
        _normalized_image=normalized,
    )


def view_point_to_original(view: ImageView, point: Point) -> Point:
    """Map a point from a complete view to EXIF-corrected original pixels."""

    _validate_view(view)
    x, y = _point_values(point, "view point")
    if not (0.0 <= x < view.width):
        raise InputError(f"point is outside view {view.view_id!r}")
    if not (0.0 <= y < view.height):
        raise InputError(f"point is outside view {view.view_id!r}")
    return Point(
        x=round(view.original_bounds.x + x * view.scale_x, 3),
        y=round(view.original_bounds.y + y * view.scale_y, 3),
    )


def view_bbox_to_original(view: ImageView, bbox: BoundingBox) -> BoundingBox:
    """Map a view bounding box to original pixels using both corners."""

    _validate_view(view)
    _validate_bbox(bbox, "view bbox")
    right = bbox.x + bbox.width
    bottom = bbox.y + bbox.height
    if bbox.x < -_GEOMETRY_EPSILON or bbox.y < -_GEOMETRY_EPSILON:
        raise InputError(f"bbox is outside view {view.view_id!r}")
    if right > view.width + _GEOMETRY_EPSILON or bottom > view.height + _GEOMETRY_EPSILON:
        raise InputError(f"bbox is outside view {view.view_id!r}")
    left = max(0.0, bbox.x)
    top = max(0.0, bbox.y)
    right = min(float(view.width), right)
    bottom = min(float(view.height), bottom)
    return BoundingBox(
        x=round(view.original_bounds.x + left * view.scale_x, 3),
        y=round(view.original_bounds.y + top * view.scale_y, 3),
        width=round((right - left) * view.scale_x, 3),
        height=round((bottom - top) * view.scale_y, 3),
    )


def view_polyline_to_original(view: ImageView, polyline: Sequence[Point]) -> list[Point]:
    """Map every point in a view polyline to original pixels."""

    if not isinstance(polyline, Sequence):
        raise InputError("polyline must be a sequence")
    return [view_point_to_original(view, point) for point in polyline]


def original_point_to_view(view: ImageView, point: Point) -> Point:
    """Map an original point into a complete view covering the full image."""

    _validate_view(view)
    x, y = _point_values(point, "original point")
    bounds = view.original_bounds
    if not (bounds.x <= x < bounds.x + bounds.width):
        raise InputError(f"point is outside view {view.view_id!r} coverage")
    if not (bounds.y <= y < bounds.y + bounds.height):
        raise InputError(f"point is outside view {view.view_id!r} coverage")
    return Point(
        x=(x - bounds.x) / view.scale_x,
        y=(y - bounds.y) / view.scale_y,
    )


def original_bbox_to_view(view: ImageView, bbox: BoundingBox) -> BoundingBox:
    """Map an original bounding box into a complete view."""

    _validate_view(view)
    _validate_bbox(bbox, "original bbox")
    bounds = view.original_bounds
    right_x = bbox.x + bbox.width
    bottom_y = bbox.y + bbox.height
    if (
        bbox.x < bounds.x
        or bbox.y < bounds.y
        or right_x > bounds.x + bounds.width
        or bottom_y > bounds.y + bounds.height
    ):
        raise InputError(f"bbox is outside view {view.view_id!r} coverage")
    left = _original_coordinate_to_view(view, bbox.x, bbox.y)
    right = _original_coordinate_to_view(view, right_x, bottom_y)
    return BoundingBox(
        x=left.x,
        y=left.y,
        width=right.x - left.x,
        height=right.y - left.y,
    )


def original_polyline_to_view(view: ImageView, polyline: Sequence[Point]) -> list[Point]:
    """Map every original polyline point into a complete view."""

    return [original_point_to_view(view, point) for point in polyline]


def scale_original_point(
    point: Point,
    original_size: tuple[int | float, int | float],
    target_size: tuple[int | float, int | float],
) -> Point:
    """Scale a point between explicit complete-image dimensions."""

    original_width, original_height = _validate_size(original_size, "original size")
    target_width, target_height = _validate_size(target_size, "target size")
    x, y = _point_values(point, "original point")
    if not (0 <= x < original_width and 0 <= y < original_height):
        raise InputError("point is outside original image")
    return Point(x=x / original_width * target_width, y=y / original_height * target_height)


def _draw_annotation(
    draw: ImageDraw.ImageDraw,
    bbox: BoundingBox,
    label: str,
    color: tuple[int, int, int],
    center: Point | None,
    *,
    occupied_labels: list[tuple[float, float, float, float]],
    blocked_bboxes: Sequence[BoundingBox],
) -> None:
    _validate_bbox(bbox, "annotation bbox")
    left, top = float(bbox.x), float(bbox.y)
    right, bottom = left + bbox.width, top + bbox.height
    canvas_width, canvas_height = draw.im.size
    if center is not None:
        center_x, center_y = _point_values(center, "annotation center")
        if not (0 <= center_x < canvas_width and 0 <= center_y < canvas_height):
            raise InputError("annotation center is outside the view")
    draw.rectangle((left, top, right, bottom), outline=color, width=2)
    if center is not None:
        draw.ellipse((center.x - 3, center.y - 3, center.x + 3, center.y + 3), fill=color)
    text_bbox = draw.textbbox((0, 0), label)
    text_width = text_bbox[2] - text_bbox[0]
    text_height = text_bbox[3] - text_bbox[1]
    label_width = text_width + 4
    label_height = text_height + 2
    max_x = max(0.0, float(canvas_width - label_width))
    max_y = max(0.0, float(canvas_height - label_height))
    candidates = (
        (left, top - label_height - 2),
        (left, bottom + 2),
        (right + 2, top),
        (left - label_width - 2, top),
    )
    label_rect = _find_annotation_rect(
        candidates,
        label_width,
        label_height,
        max_x,
        max_y,
        occupied_labels,
        blocked_bboxes,
        canvas_width,
        canvas_height,
    )
    label_x, label_y, _, _ = label_rect
    # Keep the underlying topology visible; the outline/stroke provides contrast.
    draw.rectangle(label_rect, outline=color, width=1)
    draw.text(
        (label_x + 2, label_y + 1),
        label,
        fill=color,
        stroke_width=1,
        stroke_fill=(255, 255, 255),
    )
    occupied_labels.append(label_rect)


def _find_annotation_rect(
    candidates: Sequence[tuple[float, float]],
    width: float,
    height: float,
    max_x: float,
    max_y: float,
    occupied_labels: Sequence[tuple[float, float, float, float]],
    blocked_bboxes: Sequence[BoundingBox],
    canvas_width: int,
    canvas_height: int,
) -> tuple[float, float, float, float]:
    blocked = [
        (item.x, item.y, item.x + item.width, item.y + item.height)
        for item in blocked_bboxes
    ]

    def fits(x: float, y: float) -> bool:
        rect = (x, y, x + width, y + height)
        return not any(_rectangles_overlap(rect, other) for other in (*occupied_labels, *blocked))

    for x, y in candidates:
        x = min(max(0.0, x), max_x)
        y = min(max(0.0, y), max_y)
        if fits(x, y):
            return (x, y, x + width, y + height)

    # A deterministic grid fallback handles crowded diagrams without stacking labels.
    step = max(4, int(min(width, height)))
    for y in range(0, max(1, canvas_height - int(height) + 1), step):
        for x in range(0, max(1, canvas_width - int(width) + 1), step):
            if fits(float(x), float(y)):
                return (float(x), float(y), float(x) + width, float(y) + height)
    x = min(max(0.0, candidates[0][0]), max_x)
    y = min(max(0.0, candidates[0][1]), max_y)
    return (x, y, x + width, y + height)


def _rectangles_overlap(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> bool:
    return (
        left[0] < right[2]
        and right[0] < left[2]
        and left[1] < right[3]
        and right[1] < left[3]
    )


def _original_coordinate_to_view(
    view: ImageView, x: float, y: float
) -> Point:
    bounds = view.original_bounds
    if not (
        bounds.x - _GEOMETRY_EPSILON
        <= x
        <= bounds.x + bounds.width + _GEOMETRY_EPSILON
        and bounds.y - _GEOMETRY_EPSILON
        <= y
        <= bounds.y + bounds.height + _GEOMETRY_EPSILON
    ):
        raise InputError(f"point is outside view {view.view_id!r} coverage")
    return Point(
        x=min(max((x - bounds.x) / view.scale_x, 0.0), float(view.width)),
        y=min(max((y - bounds.y) / view.scale_y, 0.0), float(view.height)),
    )


def _resize_complete(image: Image.Image, max_long_edge: int) -> Image.Image:
    width, height = image.size
    longest = max(width, height)
    if longest <= max_long_edge:
        return image.copy()
    scale = max_long_edge / longest
    target = (max(1, round(width * scale)), max(1, round(height * scale)))
    return image.resize(target, Image.Resampling.LANCZOS)


def _enhance_text_once(image: Image.Image) -> Image.Image:
    contrast = ImageEnhance.Contrast(image).enhance(1.05)
    return ImageEnhance.Sharpness(contrast).enhance(1.05).copy()


def _validate_source_path(image_path: str | Path) -> Path:
    if not isinstance(image_path, (str, Path)) or not str(image_path).strip():
        raise InputError("image path must be a non-empty string or Path")
    path = Path(image_path)
    try:
        if not path.exists():
            raise InputError(f"image file does not exist: {path}")
        if not path.is_file():
            raise InputError(f"image path is not a regular file: {path}")
    except OSError:
        raise InputError(f"cannot inspect image path: {path}") from None
    if path.suffix.lower() not in _ALLOWED_EXTENSIONS:
        raise InputError("image must use a .png, .jpg, or .jpeg extension")
    return path


def _load_normalized_image(path: Path) -> tuple[Image.Image, str]:
    try:
        with Image.open(path) as opened:
            detected_format = (opened.format or "").upper()
            if detected_format not in _ALLOWED_FORMATS:
                detail = (
                    "unsupported decoded image format: "
                    f"{detected_format or 'unknown'}"
                )
                raise InputError(detail)
            opened.load()
            oriented = ImageOps.exif_transpose(opened)
            try:
                normalized = oriented.convert("RGB").copy()
            finally:
                if oriented is not opened:
                    oriented.close()
    except InputError:
        raise
    except (
        PermissionError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        OSError,
        SyntaxError,
        ValueError,
    ):
        raise InputError(f"cannot parse image or image is damaged: {path}") from None
    return normalized, detected_format


def _normalized_image_hash(image: Image.Image) -> str:
    digest = hashlib.sha256()
    digest.update(b"topology-agent-normalized-image-v2\0")
    digest.update(image.width.to_bytes(8, "big"))
    digest.update(image.height.to_bytes(8, "big"))
    digest.update(b"RGB\0")
    digest.update(image.tobytes())
    return digest.hexdigest()


def _validate_view(view: ImageView) -> None:
    if not isinstance(view, ImageView):
        raise InputError("view must be an ImageView")


def _validate_bundle_consistency(bundle: ImageBundle) -> None:
    if not isinstance(bundle.image_info, ImageInfo):
        raise InputError("image bundle imageInfo must be an ImageInfo")
    if not all(
        isinstance(view, ImageView)
        for view in (
            bundle.original_view,
            bundle.structure_view,
            bundle.text_enhanced_view,
        )
    ):
        raise InputError("image bundle must contain original, structure, and text views")
    if bundle._links_view is not None and not isinstance(bundle._links_view, ImageView):
        raise InputError("image bundle links view must be an ImageView")
    if (
        not isinstance(bundle._normalized_image, Image.Image)
        or bundle._normalized_image.mode != "RGB"
    ):
        raise InputError("image bundle normalized image must be RGB")
    width, height = bundle.image_info.width, bundle.image_info.height
    if bundle.image_info.format.upper() not in _ALLOWED_FORMATS:
        raise InputError("image bundle imageInfo format must be PNG or JPEG")
    if bundle._normalized_image.size != (width, height):
        raise InputError("image bundle normalized image size does not match imageInfo")
    expected_bounds = BoundingBox(x=0, y=0, width=width, height=height)
    views = [bundle.original_view, bundle.structure_view, bundle.text_enhanced_view]
    if bundle._links_view is not None:
        views.append(bundle._links_view)
    expected_ids = ["original", "global_structure", "global_text"]
    if bundle._links_view is not None:
        expected_ids.insert(2, "global_links")
    if bundle.image_info.view_ids != expected_ids:
        raise InputError("image bundle viewIds are not registered deterministically")
    if not isinstance(bundle.sha256, str) or len(bundle.sha256) != 64 or any(
        char not in "0123456789abcdef" for char in bundle.sha256.lower()
    ):
        raise InputError("image bundle sha256 must be a hexadecimal SHA-256 value")
    if bundle.sha256.lower() != _normalized_image_hash(bundle._normalized_image):
        raise InputError("image bundle sha256 does not match normalized pixels")
    for view in views:
        if view.original_bounds != expected_bounds:
            raise InputError(
                f"view {view.view_id!r} does not cover the complete original image"
            )
        if view.width > width or view.height > height:
            raise InputError(f"view {view.view_id!r} enlarges the original image")
        if abs(view.width * height - view.height * width) > max(width, height):
            raise InputError(f"view {view.view_id!r} does not preserve aspect ratio")
    if bundle.original_view.image is not bundle._normalized_image:
        raise InputError("original view must retain the normalized source image")
    if (
        bundle.structure_view.width != bundle.text_enhanced_view.width
        or bundle.structure_view.height != bundle.text_enhanced_view.height
    ):
        raise InputError("structure and text views must have identical dimensions")
    if bundle._links_view is not None and (
        bundle._links_view.width != bundle.structure_view.width
        or bundle._links_view.height != bundle.structure_view.height
    ):
        raise InputError("links and structure views must have identical dimensions")


def _validate_bbox(bbox: BoundingBox, label: str) -> None:
    if not isinstance(bbox, BoundingBox):
        raise InputError(f"{label} must be a BoundingBox")
    values = (float(bbox.x), float(bbox.y), float(bbox.width), float(bbox.height))
    if not all(math.isfinite(value) for value in values):
        raise InputError(f"{label} must contain finite values")
    if values[0] < 0 or values[1] < 0 or values[2] <= 0 or values[3] <= 0:
        raise InputError(f"{label} must have non-negative origin and positive size")


def _point_values(point: Point, label: str) -> tuple[float, float]:
    if not isinstance(point, Point):
        raise InputError(f"{label} must be a Point")
    values = (float(point.x), float(point.y))
    if not all(math.isfinite(value) for value in values):
        raise InputError(f"{label} must contain finite values")
    return values


def _validate_size(
    size: tuple[int | float, int | float], label: str
) -> tuple[float, float]:
    if not isinstance(size, (tuple, list)) or len(size) != 2:
        raise InputError(f"{label} must contain width and height")
    values = (float(size[0]), float(size[1]))
    if not all(math.isfinite(value) and value > 0 for value in values):
        raise InputError(f"{label} dimensions must be finite and positive")
    return values
