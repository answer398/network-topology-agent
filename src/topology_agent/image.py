"""Image normalization, reusable views, and coordinate conversion."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from PIL import Image, ImageEnhance, ImageOps, UnidentifiedImageError

from .config import ImageProcessingConfig
from .models import BoundingBox, ImageInfo, InputError, Point


_ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg"}
_ALLOWED_FORMATS = {"PNG", "JPEG"}
_SHARPNESS_FACTOR = 1.05
_GEOMETRY_EPSILON = 1e-7


@dataclass(slots=True)
class ImageView:
    """An RGB image view with a linear mapping to normalized original pixels."""

    view_id: str
    kind: Literal["ORIGINAL", "GLOBAL", "TILE", "CROP"]
    image: Image.Image
    original_bounds: BoundingBox
    width: int = field(init=False)
    height: int = field(init=False)
    scale_x: float = field(init=False)
    scale_y: float = field(init=False)

    def __post_init__(self) -> None:
        self.view_id = _validate_view_id(self.view_id)
        if self.kind not in {"ORIGINAL", "GLOBAL", "TILE", "CROP"}:
            raise InputError(f"invalid image view kind: {self.kind!r}")
        if not isinstance(self.image, Image.Image) or self.image.mode != "RGB":
            raise InputError(f"view {self.view_id!r} must contain an RGB Pillow image")
        self.width, self.height = self.image.size
        if self.width <= 0 or self.height <= 0:
            raise InputError(f"view {self.view_id!r} has an invalid image size")
        _validate_bbox_numbers(self.original_bounds, f"view {self.view_id!r} bounds")
        self.scale_x = self.original_bounds.width / self.width
        self.scale_y = self.original_bounds.height / self.height


@dataclass(slots=True)
class ImageBundle:
    """Normalized source metadata and all reusable image views for one input."""

    source_path: Path
    image_info: ImageInfo
    sha256: str
    original_view: ImageView
    global_view: ImageView
    _enhanced_image: Image.Image = field(repr=False)
    tile_views: dict[str, ImageView] = field(default_factory=dict)
    crop_views: dict[str, ImageView] = field(default_factory=dict)

    def get_view(self, view_id: str) -> ImageView:
        """Return a registered view or raise an input error for an unknown ID."""

        checked_id = _validate_view_id(view_id)
        if checked_id == self.original_view.view_id:
            return self.original_view
        if checked_id == self.global_view.view_id:
            return self.global_view
        registered = self.tile_views.get(checked_id) or self.crop_views.get(checked_id)
        if registered is not None:
            return registered
        raise InputError(f"unknown image view ID: {checked_id!r}")


def load_image_bundle(
    image_path: str | Path,
    image_config: ImageProcessingConfig,
) -> ImageBundle:
    """Load, normalize, enhance once, resize, and tile an input image."""

    source_path = _validate_source_path(image_path)
    sizes = _validate_image_config(image_config)
    max_width, max_height, tile_width, tile_height, overlap_ratio = sizes
    normalized, detected_format = _load_normalized_image(source_path)
    width, height = normalized.size
    if width <= 0 or height <= 0:
        normalized.close()
        raise InputError(f"decoded image has invalid dimensions: {source_path}")

    image_hash = _normalized_image_hash(normalized)
    original_bounds = BoundingBox(x=0, y=0, width=width, height=height)
    original_view = ImageView("original_000", "ORIGINAL", normalized, original_bounds)
    enhanced_image = _enhance_image(normalized)
    global_view = _create_global_view(
        enhanced_image, original_bounds, max_width, max_height
    )
    tile_views = _create_tile_views(
        enhanced_image, tile_width, tile_height, overlap_ratio
    )
    view_ids = [original_view.view_id, global_view.view_id, *tile_views]
    image_info = ImageInfo(
        width=width, height=height, format=detected_format, view_ids=view_ids
    )
    return ImageBundle(
        source_path=source_path,
        image_info=image_info,
        sha256=image_hash,
        original_view=original_view,
        global_view=global_view,
        tile_views=tile_views,
        _enhanced_image=enhanced_image,
    )


def create_crop_view(
    bundle: ImageBundle,
    bbox: BoundingBox,
    view_id: str,
) -> ImageView:
    """Create and register an enhanced crop requested in original coordinates."""

    checked_id = _validate_view_id(view_id)
    if checked_id in bundle.image_info.view_ids:
        raise InputError(f"duplicate image view ID: {checked_id!r}")
    _validate_bbox_numbers(bbox, "crop bbox")

    original_width, original_height = bundle._enhanced_image.size
    requested_right = bbox.x + bbox.width
    requested_bottom = bbox.y + bbox.height
    left = max(0, math.floor(bbox.x))
    top = max(0, math.floor(bbox.y))
    right = min(original_width, math.ceil(requested_right))
    bottom = min(original_height, math.ceil(requested_bottom))
    if left >= right or top >= bottom:
        detail = (
            "crop bbox has no area within the normalized original image: "
            f"{bbox.model_dump()}"
        )
        raise InputError(detail)

    cropped_image = bundle._enhanced_image.crop((left, top, right, bottom))
    actual_bounds = BoundingBox(
        x=left, y=top, width=right - left, height=bottom - top
    )
    crop_view = ImageView(checked_id, "CROP", cropped_image, actual_bounds)
    updated_ids = [*bundle.image_info.view_ids, checked_id]
    info = bundle.image_info
    updated_info = ImageInfo(
        width=info.width, height=info.height, format=info.format, view_ids=updated_ids
    )
    bundle.crop_views[checked_id] = crop_view
    bundle.image_info = updated_info
    return crop_view


def view_point_to_original(view: ImageView, point: Point) -> Point:
    """Map a point in a view to normalized original-image coordinates."""

    x, y = _point_values(point, "view point")
    _require_point_in_rect(x, y, view.width, view.height, f"view {view.view_id!r}")
    return Point(x=view.original_bounds.x + x * view.scale_x,
                 y=view.original_bounds.y + y * view.scale_y)


def view_bbox_to_original(view: ImageView, bbox: BoundingBox) -> BoundingBox:
    """Map a view-relative bounding box to normalized original coordinates."""

    _validate_bbox_numbers(bbox, "view bbox")
    right = bbox.x + bbox.width
    bottom = bbox.y + bbox.height
    outside = (
        bbox.x >= view.width
        or bbox.y >= view.height
        or right > view.width + _GEOMETRY_EPSILON
        or bottom > view.height + _GEOMETRY_EPSILON
    )
    if outside:
        raise InputError(f"bbox is outside view {view.view_id!r}: {bbox.model_dump()}")
    right = min(right, float(view.width))
    bottom = min(bottom, float(view.height))

    left_original = view.original_bounds.x + bbox.x * view.scale_x
    top_original = view.original_bounds.y + bbox.y * view.scale_y
    right_original = view.original_bounds.x + right * view.scale_x
    bottom_original = view.original_bounds.y + bottom * view.scale_y
    return BoundingBox(
        x=left_original,
        y=top_original,
        width=right_original - left_original,
        height=bottom_original - top_original,
    )


def original_point_to_view(view: ImageView, point: Point) -> Point:
    """Map an original-image point into a view that covers that point."""

    x, y = _point_values(point, "original point")
    bounds = view.original_bounds
    right = bounds.x + bounds.width
    bottom = bounds.y + bounds.height
    if not (bounds.x <= x < right and bounds.y <= y < bottom):
        raise InputError(
            f"original point is outside view {view.view_id!r} coverage: ({x}, {y})"
        )
    return Point(x=(x - bounds.x) / view.scale_x,
                 y=(y - bounds.y) / view.scale_y)


def scale_original_point(
    point: Point,
    original_size: tuple[int | float, int | float],
    target_size: tuple[int | float, int | float],
) -> Point:
    """Linearly scale an original point to an explicit target canvas size."""

    original_width, original_height = _validate_size(original_size, "original size")
    target_width, target_height = _validate_size(target_size, "target size")
    x, y = _point_values(point, "original point")
    _require_point_in_rect(x, y, original_width, original_height, "original image")
    return Point(x=x / original_width * target_width,
                 y=y / original_height * target_height)


def _validate_source_path(image_path: str | Path) -> Path:
    if not isinstance(image_path, (str, Path)) or not str(image_path).strip():
        raise InputError("image path must be a non-empty string or Path")
    path = Path(image_path)
    try:
        exists = path.exists()
        is_file = path.is_file()
    except OSError:
        raise InputError(f"cannot inspect image path: {path}") from None
    if not exists:
        raise InputError(f"image file does not exist: {path}")
    if not is_file:
        raise InputError(f"image path is not a regular file: {path}")
    if path.suffix.lower() not in _ALLOWED_EXTENSIONS:
        detail = f"unsupported image extension for {path}: allowed .png, .jpg, .jpeg"
        raise InputError(detail)
    return path


def _validate_image_config(
    config: ImageProcessingConfig,
) -> tuple[int, int, int, int, float]:
    try:
        names = ("maxWidth", "maxHeight", "tileWidth", "tileHeight")
        values = (
            config.max_width,
            config.max_height,
            config.tile_width,
            config.tile_height,
        )
        overlap_ratio = config.overlap_ratio
    except AttributeError:
        raise InputError(
            "invalid image configuration: required fields are missing"
        ) from None

    for name, value in zip(names, values, strict=True):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise InputError(f"invalid image.{name}: expected a positive integer")
    valid_overlap = (
        not isinstance(overlap_ratio, bool)
        and isinstance(overlap_ratio, (int, float))
        and 0 <= overlap_ratio < 1
    )
    if not valid_overlap:
        raise InputError("invalid image.overlapRatio: expected a value in [0, 1)")
    return (*values, float(overlap_ratio))


def _load_normalized_image(path: Path) -> tuple[Image.Image, str]:
    try:
        with Image.open(path) as opened:
            detected_format = (opened.format or "").upper()
            if detected_format not in _ALLOWED_FORMATS:
                detail = (
                    f"unsupported decoded image format for {path}: "
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
    except PermissionError:
        raise InputError(f"cannot read image file: {path}") from None
    except (
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
    digest.update(b"topology-agent-normalized-image-v1\0")
    digest.update(image.width.to_bytes(8, byteorder="big", signed=False))
    digest.update(image.height.to_bytes(8, byteorder="big", signed=False))
    digest.update(b"RGB\0")
    digest.update(image.tobytes())
    return digest.hexdigest()


def _enhance_image(image: Image.Image) -> Image.Image:
    return ImageEnhance.Sharpness(image).enhance(_SHARPNESS_FACTOR).copy()


def _create_global_view(
    enhanced_image: Image.Image,
    original_bounds: BoundingBox,
    max_width: int,
    max_height: int,
) -> ImageView:
    width, height = enhanced_image.size
    if width > max_width or height > max_height:
        if max_width * height <= max_height * width:
            target_width = max_width
            target_height = max(1, (height * max_width + width // 2) // width)
        else:
            target_height = max_height
            target_width = max(1, (width * max_height + height // 2) // height)
        global_image = enhanced_image.resize(
            (target_width, target_height),
            Image.Resampling.LANCZOS,
        )
    else:
        global_image = enhanced_image.copy()
    return ImageView("global_000", "GLOBAL", global_image, original_bounds)


def _create_tile_views(
    enhanced_image: Image.Image,
    tile_width: int,
    tile_height: int,
    overlap_ratio: float,
) -> dict[str, ImageView]:
    width, height = enhanced_image.size
    if width <= tile_width and height <= tile_height:
        return {}

    x_starts = _axis_starts(width, tile_width, overlap_ratio)
    y_starts = _axis_starts(height, tile_height, overlap_ratio)
    views: dict[str, ImageView] = {}
    for row, top in enumerate(y_starts):
        bottom = min(top + tile_height, height)
        for column, left in enumerate(x_starts):
            right = min(left + tile_width, width)
            view_id = f"tile_r{row:03d}_c{column:03d}"
            bounds = BoundingBox(
                x=left, y=top, width=right - left, height=bottom - top
            )
            views[view_id] = ImageView(
                view_id, "TILE", enhanced_image.crop((left, top, right, bottom)), bounds
            )
    return views


def _axis_starts(length: int, tile_size: int, overlap_ratio: float) -> list[int]:
    if length <= tile_size:
        return [0]
    step = max(1, math.floor(tile_size * (1.0 - overlap_ratio)))
    last_start = length - tile_size
    starts = list(range(0, last_start + 1, step))
    if starts[-1] != last_start:
        starts.append(last_start)
    return starts


def _validate_view_id(view_id: str) -> str:
    if not isinstance(view_id, str) or not view_id.strip():
        raise InputError("image view ID must be a non-empty string")
    if view_id != view_id.strip():
        raise InputError("image view ID must not contain surrounding whitespace")
    return view_id


def _point_values(point: Point, label: str) -> tuple[float, float]:
    if not isinstance(point, Point):
        raise InputError(f"{label} must be a Point")
    try:
        x, y = float(point.x), float(point.y)
    except (TypeError, ValueError, OverflowError):
        raise InputError(f"{label} coordinates must be finite numbers") from None
    if not math.isfinite(x) or not math.isfinite(y):
        raise InputError(f"{label} coordinates must be finite")
    return x, y


def _validate_bbox_numbers(bbox: BoundingBox, label: str) -> None:
    if not isinstance(bbox, BoundingBox):
        raise InputError(f"{label} must be a BoundingBox")
    try:
        raw_values = (bbox.x, bbox.y, bbox.width, bbox.height)
        values = tuple(float(value) for value in raw_values)
    except (TypeError, ValueError, OverflowError):
        raise InputError(f"{label} values must be finite numbers") from None
    valid = all(math.isfinite(value) for value in values)
    valid = valid and math.isfinite(values[0] + values[2])
    valid = valid and math.isfinite(values[1] + values[3])
    valid = valid and values[0] >= 0 and values[1] >= 0
    valid = valid and values[2] > 0 and values[3] > 0
    if not valid:
        raise InputError(f"{label} must have finite coordinates and positive size")


def _require_point_in_rect(
    x: float, y: float, width: float, height: float, label: str
) -> None:
    if not (0 <= x < width and 0 <= y < height):
        raise InputError(
            f"point is outside {label}: ({x}, {y}) not in [0, {width}) x [0, {height})"
        )


def _validate_size(
    size: tuple[int | float, int | float], label: str
) -> tuple[float, float]:
    if not isinstance(size, (tuple, list)) or len(size) != 2:
        raise InputError(f"{label} must contain width and height")
    width, height = size
    invalid_type_or_range = any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or value <= 0
        for value in (width, height)
    )
    if invalid_type_or_range:
        raise InputError(f"{label} width and height must be finite and positive")
    try:
        width_float, height_float = float(width), float(height)
    except (TypeError, ValueError, OverflowError):
        raise InputError(
            f"{label} width and height must be finite and positive"
        ) from None
    if not math.isfinite(width_float) or not math.isfinite(height_float):
        raise InputError(f"{label} width and height must be finite and positive")
    return width_float, height_float
