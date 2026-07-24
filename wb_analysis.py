"""Western blot band detection and quantification core.

The module deliberately has no GUI dependencies, which makes the numerical
parts easy to test and reuse from scripts.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from itertools import combinations
from typing import Iterable, Literal, Mapping

import numpy as np
from scipy import ndimage as ndi
from scipy import signal as scipy_signal
from skimage import filters, measure, morphology


Polarity = Literal["dark", "light", "auto"]
MarkerMode = Literal["auto", "left", "right", "off"]
PairingStatus = Literal[
    "auto_matched",
    "manual_matched",
    "confirmed",
    "missing_reference",
    "missing_target",
    "ambiguous_target",
    "ambiguous_reference",
    "manual_unpaired",
    "invalid_manual_reference",
    "duplicate_reference",
    "zero_reference",
]


@dataclass(slots=True)
class BandROI:
    """One rectangular band region in original-image pixel coordinates."""

    uid: int
    x: int
    y: int
    width: int
    height: int
    name: str = ""
    lane: int = 0
    source: str = "自动"

    @property
    def x2(self) -> int:
        return self.x + self.width

    @property
    def y2(self) -> int:
        return self.y + self.height

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.width / 2, self.y + self.height / 2


@dataclass(slots=True)
class BandMeasurement:
    uid: int
    name: str
    lane: int
    x: int
    y: int
    width: int
    height: int
    area: int
    mean_gray: float
    background: float
    corrected_mean: float
    integrated_density: float
    relative: float = 1.0
    source: str = "自动"


@dataclass(slots=True)
class LaneNormalizationResult:
    """One target/reference lane pairing and its normalized intensity."""

    lane: int
    target_uid: int | None
    reference_uid: int | None
    target_integrated_density: float | None
    reference_integrated_density: float | None
    target_over_reference: float | None
    pairing_status: PairingStatus
    missing_reason: str = ""
    target_lane: int | None = None
    reference_lane: int | None = None


def normalize_target_to_reference_by_lane(
    target_measurements: Iterable[BandMeasurement],
    reference_measurements: Iterable[BandMeasurement],
    manual_pairs: Mapping[int, int | None] | None = None,
    confirmed_pairs: Iterable[tuple[int, int]] | None = None,
) -> list[LaneNormalizationResult]:
    """Pair target and reference bands and calculate target/reference ratios.

    Automatic matching is deliberately conservative: a lane is paired only
    when exactly one unassigned target and one unassigned reference band share
    that lane number. ``manual_pairs`` maps a target UID to a reference UID;
    mapping to ``None`` explicitly leaves that target unpaired. Manual matches
    may cross lane numbers so users can correct independently detected lanes.
    """

    targets = list(target_measurements)
    references = list(reference_measurements)
    manual = dict(manual_pairs or {})
    confirmed = set(confirmed_pairs or ())
    target_by_uid = {item.uid: item for item in targets}
    reference_by_uid = {item.uid: item for item in references}

    # A reference band may normalize only one target band. Mark every target
    # involved in a duplicate manual assignment instead of choosing one
    # silently according to input order.
    manual_reference_users: dict[int, list[int]] = {}
    for target_uid, reference_uid in manual.items():
        if target_uid in target_by_uid and reference_uid is not None:
            manual_reference_users.setdefault(reference_uid, []).append(target_uid)
    duplicate_reference_uids = {
        reference_uid
        for reference_uid, target_uids in manual_reference_users.items()
        if len(target_uids) > 1
    }
    reserved_reference_uids = {
        reference_uid
        for target_uid, reference_uid in manual.items()
        if (
            target_uid in target_by_uid
            and reference_uid in reference_by_uid
            and reference_uid not in duplicate_reference_uids
        )
    }

    auto_targets_by_lane: dict[int, list[BandMeasurement]] = {}
    available_references_by_lane: dict[int, list[BandMeasurement]] = {}
    for target in targets:
        if target.uid not in manual:
            auto_targets_by_lane.setdefault(target.lane, []).append(target)
    for reference in references:
        if reference.uid not in reserved_reference_uids:
            available_references_by_lane.setdefault(reference.lane, []).append(reference)

    results: list[LaneNormalizationResult] = []
    used_reference_uids: set[int] = set()

    def paired_result(
        target: BandMeasurement,
        reference: BandMeasurement,
        status: PairingStatus,
    ) -> LaneNormalizationResult:
        ratio: float | None
        reason = ""
        if reference.integrated_density <= 1e-12:
            ratio = None
            status = "zero_reference"
            reason = "内参积分灰度为 0，无法计算目的蛋白/内参比值"
        else:
            ratio = target.integrated_density / reference.integrated_density
            if (target.uid, reference.uid) in confirmed:
                status = "confirmed"
        used_reference_uids.add(reference.uid)
        return LaneNormalizationResult(
            lane=target.lane,
            target_uid=target.uid,
            reference_uid=reference.uid,
            target_integrated_density=target.integrated_density,
            reference_integrated_density=reference.integrated_density,
            target_over_reference=ratio,
            pairing_status=status,
            missing_reason=reason,
            target_lane=target.lane,
            reference_lane=reference.lane,
        )

    for target in sorted(targets, key=lambda item: (item.lane, item.uid)):
        if target.uid in manual:
            reference_uid = manual[target.uid]
            if reference_uid is None:
                results.append(LaneNormalizationResult(
                    lane=target.lane,
                    target_uid=target.uid,
                    reference_uid=None,
                    target_integrated_density=target.integrated_density,
                    reference_integrated_density=None,
                    target_over_reference=None,
                    pairing_status="manual_unpaired",
                    missing_reason="用户将该目的条带设为未配对",
                    target_lane=target.lane,
                ))
            elif reference_uid not in reference_by_uid:
                results.append(LaneNormalizationResult(
                    lane=target.lane,
                    target_uid=target.uid,
                    reference_uid=reference_uid,
                    target_integrated_density=target.integrated_density,
                    reference_integrated_density=None,
                    target_over_reference=None,
                    pairing_status="invalid_manual_reference",
                    missing_reason=f"手动指定的内参条带 ID {reference_uid} 已不存在",
                    target_lane=target.lane,
                ))
            elif reference_uid in duplicate_reference_uids:
                results.append(LaneNormalizationResult(
                    lane=target.lane,
                    target_uid=target.uid,
                    reference_uid=reference_uid,
                    target_integrated_density=target.integrated_density,
                    reference_integrated_density=reference_by_uid[reference_uid].integrated_density,
                    target_over_reference=None,
                    pairing_status="duplicate_reference",
                    missing_reason=f"内参条带 ID {reference_uid} 被多个目的条带重复配对",
                    target_lane=target.lane,
                    reference_lane=reference_by_uid[reference_uid].lane,
                ))
            else:
                results.append(paired_result(
                    target,
                    reference_by_uid[reference_uid],
                    "manual_matched",
                ))
            continue

        lane_targets = auto_targets_by_lane.get(target.lane, [])
        lane_references = available_references_by_lane.get(target.lane, [])
        if len(lane_targets) > 1:
            results.append(LaneNormalizationResult(
                lane=target.lane,
                target_uid=target.uid,
                reference_uid=None,
                target_integrated_density=target.integrated_density,
                reference_integrated_density=None,
                target_over_reference=None,
                pairing_status="ambiguous_target",
                missing_reason=f"目的图 lane {target.lane} 有 {len(lane_targets)} 个未指定条带，无法自动唯一配对",
                target_lane=target.lane,
            ))
        elif not lane_references:
            results.append(LaneNormalizationResult(
                lane=target.lane,
                target_uid=target.uid,
                reference_uid=None,
                target_integrated_density=target.integrated_density,
                reference_integrated_density=None,
                target_over_reference=None,
                pairing_status="missing_reference",
                missing_reason=f"内参图缺少可用于 lane {target.lane} 的条带",
                target_lane=target.lane,
            ))
        elif len(lane_references) > 1:
            results.append(LaneNormalizationResult(
                lane=target.lane,
                target_uid=target.uid,
                reference_uid=None,
                target_integrated_density=target.integrated_density,
                reference_integrated_density=None,
                target_over_reference=None,
                pairing_status="ambiguous_reference",
                missing_reason=f"内参图 lane {target.lane} 有 {len(lane_references)} 个条带，无法自动唯一配对",
                target_lane=target.lane,
            ))
        else:
            results.append(paired_result(target, lane_references[0], "auto_matched"))

    target_lanes = {target.lane for target in targets}
    ambiguous_lanes = {
        result.lane
        for result in results
        if result.pairing_status in {"ambiguous_target", "ambiguous_reference"}
    }
    for reference in sorted(references, key=lambda item: (item.lane, item.uid)):
        if (
            reference.uid in used_reference_uids
            or reference.uid in reserved_reference_uids
            or reference.uid in manual_reference_users
        ):
            continue
        # References in an ambiguous target lane are already represented by
        # the target-side diagnostic rows. Reference-only rows specifically
        # communicate a genuinely absent or unused target counterpart.
        if reference.lane in ambiguous_lanes:
            continue
        reason = (
            f"内参条带 ID {reference.uid} 未被任何目的条带配对"
            if reference.lane in target_lanes
            else f"目的图缺少 lane {reference.lane} 的条带"
        )
        results.append(LaneNormalizationResult(
            lane=reference.lane,
            target_uid=None,
            reference_uid=reference.uid,
            target_integrated_density=None,
            reference_integrated_density=reference.integrated_density,
            target_over_reference=None,
            pairing_status="missing_target",
            missing_reason=reason,
            reference_lane=reference.lane,
        ))

    return sorted(
        results,
        key=lambda item: (
            item.lane,
            item.target_uid is None,
            item.target_uid if item.target_uid is not None else item.reference_uid or 0,
        ),
    )


def to_gray_array(image: np.ndarray) -> np.ndarray:
    """Return an 8-bit grayscale array from an RGB/RGBA/grayscale array."""

    arr = np.asarray(image)
    if arr.ndim == 2:
        gray = arr.astype(np.float64)
    elif arr.ndim == 3 and arr.shape[2] >= 3:
        rgb = arr[..., :3].astype(np.float64)
        gray = rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114
    else:
        raise ValueError("不支持的图像维度")

    if gray.size == 0:
        raise ValueError("图像为空")
    if np.nanmax(gray) <= 1.0:
        gray *= 255.0
    return np.clip(np.nan_to_num(gray), 0, 255).astype(np.uint8)


def infer_polarity(gray: np.ndarray) -> Literal["dark", "light"]:
    """Infer whether bands are darker or lighter than their surroundings."""

    smooth = filters.gaussian(gray, sigma=max(2.0, min(gray.shape) / 80), preserve_range=True)
    dark_strength = np.percentile(smooth - gray, 99)
    light_strength = np.percentile(gray - smooth, 99)
    return "dark" if dark_strength >= light_strength else "light"


def signal_image(gray: np.ndarray, polarity: Polarity = "auto") -> tuple[np.ndarray, str]:
    """Create a locally background-corrected, positive band-signal image."""

    grayf = gray.astype(np.float64)
    actual = infer_polarity(gray) if polarity == "auto" else polarity
    sigma = max(3.0, min(gray.shape) / 45)
    local_background = filters.gaussian(grayf, sigma=sigma, preserve_range=True)
    signal = local_background - grayf if actual == "dark" else grayf - local_background
    signal = np.clip(signal, 0, None)
    signal = filters.gaussian(signal, sigma=0.8, preserve_range=True)
    return signal, actual


def _intersection_over_min_area(a: BandROI, b: BandROI) -> float:
    ix = max(0, min(a.x2, b.x2) - max(a.x, b.x))
    iy = max(0, min(a.y2, b.y2) - max(a.y, b.y))
    return ix * iy / max(1, min(a.width * a.height, b.width * b.height))


def _deduplicate(rois: list[BandROI]) -> list[BandROI]:
    kept: list[BandROI] = []
    for roi in sorted(rois, key=lambda r: r.width * r.height, reverse=True):
        if not any(_intersection_over_min_area(roi, old) > 0.72 for old in kept):
            kept.append(roi)
    return kept


def assign_lanes(rois: Iterable[BandROI]) -> list[BandROI]:
    """Assign lane numbers by clustering ROI horizontal centers."""

    items = list(rois)
    if not items:
        return []
    median_width = float(np.median([r.width for r in items]))
    # Same-lane bands across protein rows should have nearly identical x
    # centres. A wider tolerance can incorrectly merge two adjacent, tightly
    # spaced sample lanes and later allow their expanded ROIs to overlap.
    tolerance = max(5.0, median_width * 0.50)
    groups: list[dict[str, object]] = []
    for roi in sorted(items, key=lambda r: r.center[0]):
        cx = roi.center[0]
        nearest = min(groups, key=lambda g: abs(float(g["center"]) - cx), default=None)
        if nearest is None or abs(float(nearest["center"]) - cx) > tolerance:
            groups.append({"center": cx, "items": [roi]})
        else:
            group_items = nearest["items"]
            assert isinstance(group_items, list)
            group_items.append(roi)
            nearest["center"] = float(np.mean([r.center[0] for r in group_items]))

    groups.sort(key=lambda g: float(g["center"]))
    result: list[BandROI] = []
    for lane, group in enumerate(groups, 1):
        group_items = group["items"]
        assert isinstance(group_items, list)
        result.extend(replace(r, lane=lane) for r in group_items)
    return sorted(result, key=lambda r: (r.lane, r.y))


def filter_marker_lanes(
    rois: Iterable[BandROI],
    mode: MarkerMode = "auto",
) -> tuple[list[BandROI], list[BandROI]]:
    """Separate an edge Marker/ladder lane from sample-band candidates.

    Auto mode only removes an edge x-cluster when it contains vertically
    stacked candidates and is more fragmented than typical sample lanes.
    Explicit left/right modes remove the corresponding edge cluster, which is
    useful when only one Marker band is visible in the selected molecular-
    weight range. Returns ``(kept, removed)``.
    """

    items = list(rois)
    if mode == "off" or len(items) < 2:
        return assign_lanes(items), []
    if mode not in ("auto", "left", "right"):
        raise ValueError(f"未知 Marker 过滤模式: {mode}")

    median_width = float(np.median([roi.width for roi in items]))
    x_tolerance = max(6.0, median_width * 0.68)
    groups: list[dict[str, object]] = []
    for roi in sorted(items, key=lambda item: item.center[0]):
        cx = roi.center[0]
        nearest = min(groups, key=lambda group: abs(float(group["center"]) - cx), default=None)
        if nearest is None or abs(float(nearest["center"]) - cx) > x_tolerance:
            groups.append({"center": cx, "items": [roi]})
        else:
            group_items = nearest["items"]
            assert isinstance(group_items, list)
            group_items.append(roi)
            nearest["center"] = float(np.mean([item.center[0] for item in group_items]))
    groups.sort(key=lambda group: float(group["center"]))
    if len(groups) < 2:
        return assign_lanes(items), []

    marker_groups: list[dict[str, object]] = []
    if mode == "left":
        marker_groups = [groups[0]]
    elif mode == "right":
        marker_groups = [groups[-1]]
    else:
        typical_count = float(np.median([len(group["items"]) for group in groups]))
        median_height = float(np.median([roi.height for roi in items]))
        for group in (groups[0], groups[-1]):
            group_items = group["items"]
            assert isinstance(group_items, list)
            if len(group_items) < 2:
                continue
            y_centres = [item.center[1] for item in group_items]
            y_span = max(y_centres) - min(y_centres)
            group_width = float(np.median([item.width for item in group_items]))
            vertically_stacked = y_span >= max(3.0, median_height * 0.55)
            unusually_fragmented = len(group_items) > typical_count or group_width < median_width * 0.76
            if vertically_stacked and unusually_fragmented:
                marker_groups.append(group)

    removed_uids: set[int] = set()
    for group in marker_groups:
        group_items = group["items"]
        assert isinstance(group_items, list)
        removed_uids.update(item.uid for item in group_items)
    removed = [roi for roi in items if roi.uid in removed_uids]
    kept = [roi for roi in items if roi.uid not in removed_uids]
    return assign_lanes(kept), removed


def align_rois_by_rows(rois: Iterable[BandROI]) -> list[BandROI]:
    """Give candidates in the same blot row a shared vertical envelope.

    A weak or slightly shifted band may be detected from only its darkest core,
    producing a smaller box than its neighbours. Candidates with nearby y
    centres are clustered, then each cluster uses the union of its vertical
    bounds. Boxes narrower than the row median are widened around their own
    centres. Distinct protein rows remain separate.
    """

    items = list(rois)
    if len(items) < 2:
        return assign_lanes(items)
    median_height = float(np.median([roi.height for roi in items]))
    tolerance = max(4.0, median_height * 1.05)
    row_groups: list[dict[str, object]] = []
    for roi in sorted(items, key=lambda item: item.center[1]):
        cy = roi.center[1]
        nearest = min(row_groups, key=lambda group: abs(float(group["center"]) - cy), default=None)
        if nearest is None or abs(float(nearest["center"]) - cy) > tolerance:
            row_groups.append({"center": cy, "items": [roi]})
        else:
            group_items = nearest["items"]
            assert isinstance(group_items, list)
            group_items.append(roi)
            nearest["center"] = float(np.mean([item.center[1] for item in group_items]))

    aligned: list[BandROI] = []
    for group in row_groups:
        group_items = group["items"]
        assert isinstance(group_items, list)
        top = min(item.y for item in group_items)
        bottom = max(item.y2 for item in group_items)
        typical_width = int(round(float(np.median([item.width for item in group_items]))))
        for item in group_items:
            width = max(item.width, typical_width)
            x = int(round(item.center[0] - width / 2))
            aligned.append(replace(item, x=x, y=top, width=width, height=bottom - top))
    return assign_lanes(aligned)


def consolidate_auto_row_fragments(
    gray: np.ndarray,
    rois: Iterable[BandROI],
    polarity: Polarity = "auto",
) -> list[BandROI]:
    """Merge only shallow-valley fragments inside one automatically found band.

    Connected-component detection can emit two narrow candidates for one
    textured band. Candidates are considered fragments only when they overlap
    vertically, their centres are much closer than the typical row width, and
    the corrected x-profile has no substantial valley between them. Genuine
    close lanes with a background gap remain separate.
    """

    items = list(rois)
    if len(items) < 2:
        return items
    actual = infer_polarity(gray) if polarity == "auto" else polarity
    median_height = float(np.median([roi.height for roi in items]))
    row_tolerance = max(4.0, median_height * 0.85)
    row_groups: list[dict[str, object]] = []
    for roi in sorted(items, key=lambda item: item.center[1]):
        cy = roi.center[1]
        nearest = min(
            row_groups,
            key=lambda group: abs(float(group["center"]) - cy),
            default=None,
        )
        if nearest is None or abs(float(nearest["center"]) - cy) > row_tolerance:
            row_groups.append({"center": cy, "items": [roi]})
        else:
            group_items = nearest["items"]
            assert isinstance(group_items, list)
            group_items.append(roi)
            nearest["center"] = float(np.mean([item.center[1] for item in group_items]))

    consolidated: list[BandROI] = []
    for group in row_groups:
        group_items = group["items"]
        assert isinstance(group_items, list)
        if len(group_items) < 2:
            consolidated.extend(group_items)
            continue
        typical_width = float(np.percentile([item.width for item in group_items], 60))
        merged_row: list[BandROI] = []
        for candidate in sorted(group_items, key=lambda item: item.center[0]):
            if not merged_row:
                merged_row.append(candidate)
                continue
            previous = merged_row[-1]
            vertical_overlap = max(0, min(previous.y2, candidate.y2) - max(previous.y, candidate.y))
            overlap_ratio = vertical_overlap / max(1, min(previous.height, candidate.height))
            centre_distance = candidate.center[0] - previous.center[0]
            combined_left = min(previous.x, candidate.x)
            combined_right = max(previous.x2, candidate.x2)
            plausible_fragment_geometry = (
                overlap_ratio >= 0.35
                and centre_distance <= max(5.0, typical_width * 0.90)
                and combined_right - combined_left <= max(8.0, typical_width * 1.65)
            )
            if not plausible_fragment_geometry:
                merged_row.append(candidate)
                continue

            profile_top = max(previous.y, candidate.y)
            profile_bottom = min(previous.y2, candidate.y2)
            if profile_bottom - profile_top < 2:
                profile_top = min(previous.y, candidate.y)
                profile_bottom = max(previous.y2, candidate.y2)
            profile = gray[profile_top:profile_bottom, combined_left:combined_right].astype(np.float64).mean(axis=0)
            if actual == "dark":
                profile = -profile
            profile = ndi.gaussian_filter1d(profile, sigma=1.0)
            left_peak_index = int(round(previous.center[0] - combined_left))
            right_peak_index = int(round(candidate.center[0] - combined_left))
            left_peak_index = int(np.clip(left_peak_index, 0, len(profile) - 1))
            right_peak_index = int(np.clip(right_peak_index, 0, len(profile) - 1))
            lo, hi = sorted((left_peak_index, right_peak_index))
            baseline = float(np.percentile(profile, 10))
            weaker_peak = min(float(profile[left_peak_index]), float(profile[right_peak_index]))
            valley = float(np.min(profile[lo:hi + 1]))
            valley_depth = (weaker_peak - valley) / max(weaker_peak - baseline, 1e-6)
            if valley_depth >= 0.32:
                merged_row.append(candidate)
                continue

            top = min(previous.y, candidate.y)
            bottom = max(previous.y2, candidate.y2)
            merged_row[-1] = replace(
                previous,
                x=combined_left,
                y=top,
                width=combined_right - combined_left,
                height=bottom - top,
                source="自动合并",
            )
        row_width = int(round(float(np.median([item.width for item in merged_row]))))
        row_height = int(round(float(np.median([item.height for item in merged_row]))))
        for item in merged_row:
            width = max(item.width, row_width)
            height = max(item.height, row_height)
            consolidated.append(replace(
                item,
                x=int(round(item.center[0] - width / 2)),
                y=int(round(item.center[1] - height / 2)),
                width=width,
                height=height,
            ))
    return assign_lanes(consolidated)


def align_rois_to_region_signal(
    gray: np.ndarray,
    rois: Iterable[BandROI],
    region: tuple[int, int, int, int],
    polarity: Polarity = "auto",
) -> list[BandROI]:
    """Align the dominant band row using the selected region's y-projection.

    The projection is derived from background-corrected pixels across the full
    selected width, so one weak lane whose detected core is vertically shifted
    follows the row supported by the other lanes. Other distant protein rows
    continue to use geometry-based row alignment.
    """

    items = list(rois)
    if len(items) < 2:
        return assign_lanes(items)
    h, w = gray.shape
    x1, y1, x2, y2 = (int(round(value)) for value in region)
    x1, x2 = sorted((int(np.clip(x1, 0, w)), int(np.clip(x2, 0, w))))
    y1, y2 = sorted((int(np.clip(y1, 0, h)), int(np.clip(y2, 0, h))))
    patch = gray[y1:y2, x1:x2].astype(np.float64)
    if patch.size == 0 or patch.shape[0] < 3:
        return align_rois_by_rows(items)

    actual = infer_polarity(gray) if polarity == "auto" else polarity
    background = float(np.median(patch))
    corrected = (
        np.clip(background - patch, 0, None)
        if actual == "dark"
        else np.clip(patch - background, 0, None)
    )
    projection = ndi.gaussian_filter1d(corrected.mean(axis=1), sigma=1.0)
    baseline = float(np.percentile(projection, 20))
    peak_index = int(np.argmax(projection))
    peak_value = float(projection[peak_index])
    if peak_value <= baseline + 1e-6:
        return align_rois_by_rows(items)

    signal_cutoff = baseline + (peak_value - baseline) * 0.16
    top_index = peak_index
    bottom_index = peak_index
    while top_index > 0 and projection[top_index - 1] >= signal_cutoff:
        top_index -= 1
    while bottom_index + 1 < len(projection) and projection[bottom_index + 1] >= signal_cutoff:
        bottom_index += 1
    peak_y = y1 + peak_index

    median_height = float(np.median([item.height for item in items]))
    # Allow the small vertical offset seen in weak first lanes, but cap the
    # association by region height so complex upper/lower signals are never
    # forced into one tall ROI merely because the selection is wide.
    association_distance = max(
        8.0,
        min(median_height * 2.5, patch.shape[0] * 0.32),
    )
    dominant = [
        item for item in items
        if abs(item.center[1] - peak_y) <= association_distance
        or item.y <= peak_y <= item.y2
    ]
    if len(dominant) < 2:
        return align_rois_by_rows(items)

    dominant_uids = {item.uid for item in dominant}
    dominant_top = min(y1 + top_index, *(item.y for item in dominant))
    dominant_bottom = max(y1 + bottom_index + 1, *(item.y2 for item in dominant))
    typical_width = int(round(float(np.median([item.width for item in dominant]))))
    # WB bands should remain horizontal. If complex smearing made the union
    # taller than both the typical component scale and lane width, constrain it
    # around the projection peak instead of emitting a vertical strip.
    max_row_height = max(8, int(round(min(median_height * 2.2, typical_width * 0.95))))
    if dominant_bottom - dominant_top > max_row_height:
        dominant_top = max(y1, int(round(peak_y - max_row_height / 2)))
        dominant_bottom = min(y2, dominant_top + max_row_height)
        dominant_top = max(y1, dominant_bottom - max_row_height)

    remaining = [item for item in items if item.uid not in dominant_uids]
    result = align_rois_by_rows(remaining)
    for item in dominant:
        width = max(item.width, typical_width)
        x = int(round(item.center[0] - width / 2))
        result.append(replace(
            item,
            x=x,
            y=dominant_top,
            width=width,
            height=dominant_bottom - dominant_top,
        ))
    return assign_lanes(result)


def detect_bands(
    gray: np.ndarray,
    sensitivity: float = 55,
    min_area: int = 30,
    polarity: Polarity = "auto",
    first_uid: int = 1,
) -> tuple[list[BandROI], str]:
    """Automatically detect band-shaped connected regions.

    ``sensitivity`` is in the intuitive 1..100 direction: larger values find
    weaker regions. The returned polarity is the actual polarity used.
    """

    if gray.ndim != 2:
        raise ValueError("detect_bands 需要灰度图")
    signal, actual_polarity = signal_image(gray, polarity)
    positive = signal[signal > 0]
    if positive.size < 10:
        return [], actual_polarity

    sensitivity = float(np.clip(sensitivity, 1, 100))
    # High sensitivity lowers the percentile threshold. Otsu provides a stable
    # anchor while the percentile gives the UI control a useful range.
    otsu = filters.threshold_otsu(positive)
    percentile = 97.5 - sensitivity * 0.42  # 97.1 .. 55.5
    p_threshold = float(np.percentile(positive, percentile))
    blend = sensitivity / 100.0
    threshold = (1 - blend) * max(otsu, p_threshold) + blend * min(otsu, p_threshold)
    mask = signal >= threshold
    radius = max(1, int(round(min(gray.shape) / 350)))
    # Keep the horizontal footprint compact. A wider closing kernel easily
    # joins neighbouring lanes when bands are only a few pixels apart, causing
    # an entire row to be mistaken for one component.
    mask = morphology.binary_closing(mask, morphology.rectangle(1 + 2 * radius, 1))
    mask = morphology.remove_small_objects(mask, min_size=max(4, int(min_area * 0.45)))
    mask = ndi.binary_fill_holes(mask)

    labels = measure.label(mask)
    h_img, w_img = gray.shape
    found: list[BandROI] = []
    pad_x = max(2, int(round(w_img * 0.004)))
    pad_y = max(1, int(round(h_img * 0.004)))
    for region in measure.regionprops(labels, intensity_image=signal):
        y1, x1, y2, x2 = region.bbox
        component_width, component_height = x2 - x1, y2 - y1
        boxes = [(y1, x1, y2, x2)]

        # Closely spaced lanes can become one connected component even with a
        # small morphology kernel. Split an unusually wide component using the
        # raw grayscale x-profile. Raw gray is important here: a local
        # high-pass signal can create two edge-peaks within one broad band.
        if component_width >= component_height * 5 and component_width >= 18:
            component_gray = gray[y1:y2, x1:x2].astype(np.float64)
            profile = component_gray.mean(axis=0)
            if actual_polarity == "dark":
                profile = -profile
            profile = ndi.gaussian_filter1d(profile, sigma=1.0)
            dynamic_range = float(np.ptp(profile))
            # Add a low baseline at both ends so a band touching the component
            # boundary can still be recognized as a peak by find_peaks.
            edge_pad = max(3, component_height)
            padded_profile = np.pad(
                profile,
                (edge_pad, edge_pad),
                mode="constant",
                constant_values=float(np.min(profile) - max(dynamic_range, 1.0)),
            )
            peaks, properties = scipy_signal.find_peaks(
                padded_profile,
                prominence=max(1.5, dynamic_range * 0.08),
                distance=max(3, int(round(component_height * 0.65))),
                width=max(2, int(round(component_height * 0.18))),
                rel_height=0.55,
            )
            valid = (peaks >= edge_pad) & (peaks < edge_pad + component_width)
            peaks = peaks[valid]
            properties = {key: values[valid] for key, values in properties.items()}
            if len(peaks) >= 2:
                split_boxes: list[tuple[int, int, int, int]] = []
                for left, right in zip(properties["left_ips"], properties["right_ips"]):
                    sx1 = max(x1, x1 + int(np.floor(left - edge_pad)))
                    sx2 = min(x2, x1 + int(np.ceil(right - edge_pad)) + 1)
                    if sx2 - sx1 >= 3:
                        split_boxes.append((y1, sx1, y2, sx2))
                if len(split_boxes) >= 2:
                    boxes = split_boxes

        for by1, bx1, by2, bx2 in boxes:
            width, height = bx2 - bx1, by2 - by1
            bbox_area = width * height
            region_area = int(np.count_nonzero(mask[by1:by2, bx1:bx2]))
            if region_area < min_area or width < 3 or height < 2:
                continue
            if bbox_area > gray.size * 0.18 or width > w_img * 0.75 or height > h_img * 0.35:
                continue
            aspect = width / max(height, 1)
            # Blot bands tend to be horizontal, but keep near-square spots too.
            if aspect < 0.65 or aspect > 35:
                continue
            # Reject very sparse noise boxes.
            if region_area / bbox_area < 0.18:
                continue
            bx1 = max(0, bx1 - pad_x)
            by1 = max(0, by1 - pad_y)
            bx2 = min(w_img, bx2 + pad_x)
            by2 = min(h_img, by2 + pad_y)
            found.append(BandROI(0, bx1, by1, bx2 - bx1, by2 - by1))

    found = _deduplicate(found)
    found = split_merged_band_rois(gray, found, actual_polarity)
    found = refine_auto_rows_by_projection(gray, found, actual_polarity)
    found = consolidate_auto_row_fragments(gray, found, actual_polarity)
    found = assign_lanes(found)
    output: list[BandROI] = []
    for offset, roi in enumerate(found):
        uid = first_uid + offset
        output.append(replace(roi, uid=uid, name=f"Band {uid}"))
    return output, actual_polarity


def detect_bands_by_row_projection(
    gray: np.ndarray,
    region: tuple[int, int, int, int],
    polarity: Polarity = "auto",
    expected_width: float | None = None,
    first_uid: int = 1,
    band_count: int | None = None,
) -> tuple[list[BandROI], str]:
    """Detect one horizontal WB row from x-axis signal peaks.

    Unlike connected-component detection, projection peaks remain separable
    when neighbouring bands almost touch, and a shallow band can be recovered
    even when it never forms a binary component.
    """

    h, w = gray.shape
    x1, y1, x2, y2 = (int(round(value)) for value in region)
    x1, x2 = sorted((int(np.clip(x1, 0, w)), int(np.clip(x2, 0, w))))
    y1, y2 = sorted((int(np.clip(y1, 0, h)), int(np.clip(y2, 0, h))))
    patch = gray[y1:y2, x1:x2].astype(np.float64)
    actual = infer_polarity(gray) if polarity == "auto" else polarity
    if patch.size == 0 or patch.shape[0] < 3 or patch.shape[1] < 6:
        return [], actual

    if actual == "dark":
        column_background = np.percentile(patch, 85, axis=0)
        corrected = np.clip(column_background[None, :] - patch, 0, None)
    else:
        column_background = np.percentile(patch, 15, axis=0)
        corrected = np.clip(patch - column_background[None, :], 0, None)

    y_profile = ndi.gaussian_filter1d(corrected.mean(axis=1), sigma=1.0)
    y_baseline = float(np.percentile(y_profile, 20))
    y_peak = int(np.argmax(y_profile))
    y_amplitude = float(y_profile[y_peak] - y_baseline)
    if y_amplitude <= 1.0:
        return [], actual
    y_cutoff = y_baseline + y_amplitude * 0.14
    row_top = y_peak
    row_bottom = y_peak
    while row_top > 0 and y_profile[row_top - 1] >= y_cutoff:
        row_top -= 1
    while row_bottom + 1 < len(y_profile) and y_profile[row_bottom + 1] >= y_cutoff:
        row_bottom += 1
    max_row_height = max(5, int(round(patch.shape[0] * 0.48)))
    if row_bottom - row_top + 1 > max_row_height:
        row_top = max(0, y_peak - max_row_height // 2)
        row_bottom = min(patch.shape[0] - 1, row_top + max_row_height - 1)

    raw_x_profile = corrected[row_top : row_bottom + 1].mean(axis=0)
    raw_x_baseline = float(np.percentile(raw_x_profile, 20))
    x_profile = ndi.gaussian_filter1d(raw_x_profile, sigma=1.0)
    x_baseline = float(np.percentile(x_profile, 20))
    x_high = float(np.percentile(x_profile, 98))
    x_dynamic = x_high - x_baseline
    if x_dynamic <= 1.0:
        return [], actual
    if expected_width is None or expected_width <= 0:
        expected_width = max(8.0, patch.shape[1] / 12.0)
    if band_count is not None:
        band_count = int(band_count)
        if band_count < 1:
            raise ValueError("指定条带数必须为正整数")
        if band_count * 3 > len(x_profile):
            raise ValueError("指定条带数过多，当前区域宽度不足以建立独立选区")
    # Start with a deliberately short minimum distance.  A fixed distance near
    # the typical band width loses real lanes when two bands are close.  Small
    # internal maxima are consolidated below by inspecting the intervening
    # valley, which preserves a close pair when it has a genuine signal dip.
    peak_distance = max(3, int(round(expected_width * 0.32)))

    edge_pad = max(4, peak_distance)
    padded = np.pad(
        x_profile,
        (edge_pad, edge_pad),
        mode="constant",
        constant_values=x_baseline - max(1.0, x_dynamic * 0.1),
    )
    if band_count is None:
        peaks, properties = scipy_signal.find_peaks(
            padded,
            height=x_baseline + x_dynamic * 0.055,
            prominence=max(0.8, x_dynamic * 0.045),
            distance=peak_distance,
            width=2,
        )
    else:
        # With a requested count, retain weak local peaks and rank them by
        # topographic prominence. This makes the count useful precisely when
        # a shallow band is below the automatic global threshold.
        guided_distance = max(
            2,
            min(
                int(round(expected_width * 0.22)),
                len(x_profile) // max(2, band_count * 2),
            ),
        )
        peaks, properties = scipy_signal.find_peaks(
            padded,
            prominence=max(0.2, x_dynamic * 0.004),
            distance=guided_distance,
            width=1,
        )
    valid = (peaks >= edge_pad) & (peaks < edge_pad + len(x_profile))
    peaks = peaks[valid] - edge_pad
    properties = {key: values[valid] for key, values in properties.items()}
    if len(peaks) == 0 and band_count is None:
        return [], actual
    if band_count is not None:
        prominences = properties.get("prominences", np.zeros(len(peaks)))
        # A Marker deliberately left outside the selection can still spill a
        # strong tail through the crop boundary. Inspect signal on both sides
        # of the actual selection boundary: an edge candidate is rejected only
        # when a nearby strong peak exists outside the box and no deep valley
        # separates them. This preserves a real first lane that merely happens
        # to be close to the selection edge.
        rejected: set[int] = set()
        edge_cluster_span = max(5.0, expected_width * 0.80)
        distinct_valley_depth = 0.85
        context_span = max(6, int(np.ceil(expected_width * 1.1)))
        context_x1 = max(0, x1 - context_span)
        context_x2 = min(w, x2 + context_span)
        context_patch = gray[y1:y2, context_x1:context_x2].astype(np.float64)
        if actual == "dark":
            context_background = np.percentile(context_patch, 85, axis=0)
            context_corrected = np.clip(context_background[None, :] - context_patch, 0, None)
        else:
            context_background = np.percentile(context_patch, 15, axis=0)
            context_corrected = np.clip(context_patch - context_background[None, :], 0, None)
        context_profile = ndi.gaussian_filter1d(
            context_corrected[row_top : row_bottom + 1].mean(axis=0),
            sigma=1.0,
        )
        context_floor = float(np.percentile(context_profile, 5))
        left_boundary = x1 - context_x1
        right_boundary = x2 - context_x1

        for side in ("left", "right"):
            outside_slice = (
                context_profile[:left_boundary]
                if side == "left"
                else context_profile[right_boundary:]
            )
            if outside_slice.size < 2:
                # At the physical image edge there is no outside context. A
                # low-prominence maximum sitting directly on that edge is
                # usually a clipped tail/annotation, whereas a genuine edge
                # band remains comparatively prominent.
                maximum_candidate_prominence = max(
                    (float(value) for value in prominences),
                    default=0.0,
                )
                tiny_edge_margin = max(2, int(round(expected_width * 0.08)))
                for index, candidate in enumerate(peaks):
                    distance_to_edge = (
                        int(candidate)
                        if side == "left"
                        else len(x_profile) - 1 - int(candidate)
                    )
                    if (
                        distance_to_edge < tiny_edge_margin
                        and float(prominences[index]) < maximum_candidate_prominence * 0.24
                    ):
                        rejected.add(index)
                continue
            outside_local_peak = int(np.argmax(outside_slice))
            outside_peak = (
                outside_local_peak
                if side == "left"
                else right_boundary + outside_local_peak
            )
            outside_amplitude = float(context_profile[outside_peak] - context_floor)
            if outside_amplitude < max(1.0, x_dynamic * 0.10):
                continue
            for index, candidate in enumerate(peaks):
                candidate = int(candidate)
                distance_to_edge = (
                    candidate
                    if side == "left"
                    else len(x_profile) - 1 - candidate
                )
                if distance_to_edge >= edge_cluster_span:
                    continue
                candidate_context = left_boundary + candidate
                outside_distance = abs(candidate_context - outside_peak)
                extended_edge_span = max(edge_cluster_span, expected_width * 1.10)
                if outside_distance > extended_edge_span:
                    continue
                lo, hi = sorted((outside_peak, candidate_context))
                weaker = min(
                    float(context_profile[outside_peak]),
                    float(context_profile[candidate_context]),
                )
                valley = float(np.min(context_profile[lo:hi + 1]))
                valley_depth = (weaker - valley) / max(weaker - context_floor, 1e-6)
                candidate_amplitude = float(context_profile[candidate_context] - context_floor)
                looks_like_near_continuation = outside_distance <= edge_cluster_span
                looks_like_weaker_tail = candidate_amplitude < outside_amplitude * 0.75
                if valley_depth < distinct_valley_depth and (
                    looks_like_near_continuation or looks_like_weaker_tail
                ):
                    rejected.add(index)

        # Select the requested peaks as one row-level set. For ordinary WB lane
        # counts, evaluate candidate combinations jointly: good sets contain
        # prominent peaks, cover the selected row, and have reasonably stable
        # adjacent lane spacing. This prevents an internal peak from displacing
        # a weaker band at the far side of the selection.
        ranked_all = list(np.argsort(prominences)[::-1])
        meaningful_prominence = max(0.8, x_dynamic * 0.01)
        ranked = [
            int(index) for index in ranked_all
            if float(prominences[index]) >= meaningful_prominence
            and int(index) not in rejected
        ]
        selected_indices: list[int] = []
        if band_count == 1 and ranked:
            selected_indices = [ranked[0]]
        elif 2 <= band_count <= 10 and len(ranked) >= band_count:
            pool = ranked[: min(20, len(ranked))]
            maximum_prominence = max(float(prominences[index]) for index in pool)
            quality_scale = max(
                np.log1p(maximum_prominence / meaningful_prominence),
                1e-6,
            )
            best_score = -np.inf
            best_combination: tuple[int, ...] | None = None
            for candidate_set in combinations(pool, band_count):
                ordered = sorted(candidate_set, key=lambda index: int(peaks[index]))
                positions = np.asarray([int(peaks[index]) for index in ordered], dtype=float)
                peak_quality = float(np.mean([
                    np.log1p(float(prominences[index]) / meaningful_prominence) / quality_scale
                    for index in ordered
                ]))
                span_ratio = float((positions[-1] - positions[0]) / max(1, len(x_profile)))
                gaps = np.diff(positions)
                gap_mean = float(np.mean(gaps))
                gap_variation = float(np.std(gaps) / max(gap_mean, 1e-6))
                score = peak_quality + 1.25 * span_ratio - 2.50 * gap_variation
                if score > best_score:
                    best_score = score
                    best_combination = tuple(ordered)
            if best_combination is not None:
                selected_indices = list(best_combination)
        else:
            # Large requested counts use a bounded diversity-first fallback so
            # the combinatorial search cannot freeze the GUI.
            if ranked:
                selected_indices.append(ranked[0])
            while len(selected_indices) < band_count:
                remaining = [index for index in ranked if index not in selected_indices]
                if not remaining:
                    break
                best = max(
                    remaining,
                    key=lambda index: (
                        min(
                            abs(int(peaks[index]) - int(peaks[chosen]))
                            for chosen in selected_indices
                        )
                        + 0.18 * expected_width * np.log1p(
                            float(prominences[index]) / meaningful_prominence
                        )
                    ),
                )
                selected_indices.append(best)

        # If fewer than the requested number pass the meaningful-prominence
        # floor, use the same coverage rule on lower peaks before falling back
        # to equal-width seeds.
        if len(selected_indices) < band_count:
            low_ranked = [
                int(index) for index in ranked_all
                if int(index) not in rejected and int(index) not in selected_indices
            ]
            while len(selected_indices) < band_count and low_ranked:
                if not selected_indices:
                    selected_indices.append(low_ranked.pop(0))
                    continue
                best = max(
                    low_ranked,
                    key=lambda index: min(
                        abs(int(peaks[index]) - int(peaks[chosen]))
                        for chosen in selected_indices
                    ),
                )
                selected_indices.append(best)
                low_ranked.remove(best)

        peaks = np.sort(peaks[selected_indices]) if selected_indices else np.asarray([], dtype=int)

        # A nearly flat/blurred lane may not create enough strict local maxima.
        # As a last count-guided fallback, place one seed in each equal-width
        # section at its strongest x position. Valley boundaries below still
        # adapt those seeds to the actual signal geometry.
        if len(peaks) < band_count:
            sections = np.array_split(np.arange(len(x_profile)), band_count)
            peaks = np.asarray([
                int(section[np.argmax(x_profile[section])])
                for section in sections
                if len(section)
            ], dtype=int)
            peaks = np.unique(peaks)
        if len(peaks) != band_count:
            raise ValueError(f"无法在该区域内建立 {band_count} 个独立条带峰")
    else:
        peaks = np.sort(peaks)

    # A broad or slightly irregular single band can contain several local
    # maxima.  Merge only close maxima whose saddle is shallow relative to the
    # weaker peak; a deep valley is evidence for two distinct bands even when
    # their centres are much closer than the estimated lane width.
    if band_count is None:
        consolidated: list[int] = []
        close_limit = max(5.0, expected_width * 0.88)
        # The row can be densely occupied by bands, which raises the global
        # x-profile baseline close to the band plateau. In that case harmless
        # texture can produce a relative dip around 0.2, while a real inter-lane
        # gap still produces a much deeper saddle. A 0.30 cutoff removes those
        # plateau peaks without restoring the old centre-distance limitation.
        # Textured WB bands often contain two dark lobes with a saddle that is
        # still well above background. True neighbouring lanes usually fall
        # close to the row baseline between peaks. A 0.60 relative-depth cutoff
        # merges the former while retaining the latter.
        minimum_valley_depth = 0.60
        raw_peak_gaps = np.diff(peaks.astype(float))
        for peak_position, peak_value_index in enumerate(peaks):
            peak_index = int(peak_value_index)
            if not consolidated:
                consolidated.append(peak_index)
                continue
            previous = consolidated[-1]
            distance = peak_index - previous
            valley = float(np.min(x_profile[previous : peak_index + 1]))
            weaker_peak = min(float(x_profile[previous]), float(x_profile[peak_index]))
            weaker_amplitude = max(weaker_peak - x_baseline, 1e-6)
            valley_depth = (weaker_peak - valley) / weaker_amplitude
            raw_weaker_peak = min(
                float(raw_x_profile[previous]),
                float(raw_x_profile[peak_index]),
            )
            raw_valley = float(np.min(raw_x_profile[previous : peak_index + 1]))
            raw_valley_depth = (
                (raw_weaker_peak - raw_valley)
                / max(raw_weaker_peak - raw_x_baseline, 1e-6)
            )
            shallow_close_saddle = (
                distance < close_limit
                and valley_depth < minimum_valley_depth
            )

            # A textured lane can have a fairly deep internal light streak and
            # therefore survive the local 0.60 saddle test. Recheck it against
            # the whole row: when this one gap is much shorter than the other
            # lane spacings and the saddle still remains above true background,
            # the two peaks are more plausibly lobes of one band. A genuine
            # ultra-close pair with a background-level gap has valley_depth
            # near 1.0 and remains separate.
            spacing_outlier_with_residual_signal = False
            gap_index = peak_position - 1
            if len(raw_peak_gaps) >= 3 and 0 <= gap_index < len(raw_peak_gaps):
                other_gaps = np.delete(raw_peak_gaps, gap_index)
                typical_other_gap = float(np.median(other_gaps)) if other_gaps.size else 0.0
                spacing_outlier_with_residual_signal = (
                    typical_other_gap > 0
                    and float(raw_peak_gaps[gap_index]) < typical_other_gap * 0.70
                    and distance < max(close_limit, expected_width * 1.25)
                    and valley_depth < 0.88
                    and raw_valley_depth < 0.90
                )

            if shallow_close_saddle or spacing_outlier_with_residual_signal:
                # Retain the stronger representative of one irregular plateau.
                if x_profile[peak_index] > x_profile[previous]:
                    consolidated[-1] = peak_index
                continue
            consolidated.append(peak_index)
        peaks = np.asarray(consolidated, dtype=int)

    # Valleys between neighbouring peaks are hard lane boundaries. Within each
    # lane interval, walk down to a low fraction of that peak to capture the
    # whole band without joining its neighbour.
    boundaries = [0]
    for left_peak, right_peak in zip(peaks, peaks[1:]):
        segment = x_profile[left_peak : right_peak + 1]
        boundaries.append(left_peak + int(np.argmin(segment)))
    boundaries.append(len(x_profile))

    rois: list[BandROI] = []
    global_top = max(0, y1 + row_top - 2)
    global_bottom = min(h, y1 + row_bottom + 3)
    for index, peak in enumerate(peaks):
        interval_left = boundaries[index]
        interval_right = boundaries[index + 1]
        peak_value = float(x_profile[peak])
        if band_count is None:
            support_cutoff = x_baseline + (peak_value - x_baseline) * 0.12
        else:
            interval_floor = float(np.min(x_profile[interval_left:interval_right]))
            support_cutoff = interval_floor + (peak_value - interval_floor) * 0.12
        left = peak
        right = peak
        while left > interval_left and x_profile[left - 1] >= support_cutoff:
            left -= 1
        while right + 1 < interval_right and x_profile[right + 1] >= support_cutoff:
            right += 1
        left = max(interval_left, left - 2)
        right = min(interval_right, right + 3)
        if right - left < 3:
            continue
        uid = first_uid + len(rois)
        rois.append(BandROI(
            uid=uid,
            x=x1 + left,
            y=global_top,
            width=right - left,
            height=max(2, global_bottom - global_top),
            name=f"Band {uid}",
            source="投影",
        ))
    return assign_lanes(rois), actual


def refine_auto_rows_by_projection(
    gray: np.ndarray,
    rois: Iterable[BandROI],
    polarity: Polarity = "auto",
) -> list[BandROI]:
    """Replace noisy full-image row components with coherent x-profile peaks.

    Full-image thresholding may detect an upper smear and the band body as
    separate components. Candidates are grouped into visual rows, then a row
    projection is accepted only when it returns a plausible number of peaks
    relative to the original component count. Distinct protein rows are
    refined independently.
    """

    items = list(rois)
    if len(items) < 3:
        return items
    median_height = float(np.median([item.height for item in items]))
    row_tolerance = max(8.0, median_height * 1.55)
    groups: list[dict[str, object]] = []
    for item in sorted(items, key=lambda roi: roi.center[1]):
        cy = item.center[1]
        nearest = min(
            groups,
            key=lambda group: abs(float(group["center"]) - cy),
            default=None,
        )
        if nearest is None or abs(float(nearest["center"]) - cy) > row_tolerance:
            groups.append({"center": cy, "items": [item]})
        else:
            group_items = nearest["items"]
            assert isinstance(group_items, list)
            group_items.append(item)
            nearest["center"] = float(np.mean([roi.center[1] for roi in group_items]))

    h, w = gray.shape
    refined: list[BandROI] = []
    for group in groups:
        group_items = group["items"]
        assert isinstance(group_items, list)
        if len(group_items) < 3:
            refined.extend(group_items)
            continue
        typical_width = float(np.percentile([item.width for item in group_items], 60))
        typical_height = float(np.median([item.height for item in group_items]))
        centre_span_y = max(item.center[1] for item in group_items) - min(
            item.center[1] for item in group_items
        )
        if centre_span_y > max(typical_height * 2.8, h * 0.35):
            refined.extend(group_items)
            continue
        margin_x = max(8, int(round(typical_width * 0.60)))
        margin_y = max(10, int(round(typical_height * 0.85)))
        bounds = (
            max(0, min(item.x for item in group_items) - margin_x),
            max(0, min(item.y for item in group_items) - margin_y),
            min(w, max(item.x2 for item in group_items) + margin_x),
            min(h, max(item.y2 for item in group_items) + margin_y),
        )
        if bounds[2] - bounds[0] < typical_width * 2.5:
            refined.extend(group_items)
            continue
        projection_rois, _ = detect_bands_by_row_projection(
            gray,
            bounds,
            polarity=polarity,
            expected_width=typical_width,
            first_uid=1,
        )
        lower_count = max(2, int(np.ceil(len(group_items) * 0.55)))
        upper_count = max(len(group_items) + 2, int(np.ceil(len(group_items) * 1.25)))
        if lower_count <= len(projection_rois) <= upper_count:
            refined.extend(
                replace(roi, uid=0, name="", source="自动行投影")
                for roi in projection_rois
            )
        else:
            refined.extend(group_items)
    return assign_lanes(refined)


def split_merged_band_rois(
    gray: np.ndarray,
    rois: Iterable[BandROI],
    polarity: Polarity = "auto",
) -> list[BandROI]:
    """Split abnormally wide full-image candidates only when multiple peaks exist."""

    items = list(rois)
    if len(items) < 2:
        return items
    median_height = float(np.median([roi.height for roi in items]))
    row_tolerance = max(4.0, median_height * 1.25)
    row_groups: list[dict[str, object]] = []
    for roi in sorted(items, key=lambda item: item.center[1]):
        cy = roi.center[1]
        nearest = min(row_groups, key=lambda group: abs(float(group["center"]) - cy), default=None)
        if nearest is None or abs(float(nearest["center"]) - cy) > row_tolerance:
            row_groups.append({"center": cy, "items": [roi]})
        else:
            group_items = nearest["items"]
            assert isinstance(group_items, list)
            group_items.append(roi)
            nearest["center"] = float(np.mean([item.center[1] for item in group_items]))

    h, w = gray.shape
    output: list[BandROI] = []
    for group in row_groups:
        group_items = group["items"]
        assert isinstance(group_items, list)
        if len(group_items) < 2:
            output.extend(group_items)
            continue
        typical_width = float(np.percentile([item.width for item in group_items], 40))
        for roi in group_items:
            is_unusually_wide = roi.width >= typical_width * 1.55 and roi.width >= typical_width + 7
            if not is_unusually_wide:
                output.append(roi)
                continue
            y_margin = max(4, int(round(roi.height * 0.8)))
            bounds = (
                max(0, roi.x),
                max(0, roi.y - y_margin),
                min(w, roi.x2),
                min(h, roi.y2 + y_margin),
            )
            parts, _ = detect_bands_by_row_projection(
                gray,
                bounds,
                polarity=polarity,
                expected_width=typical_width,
                first_uid=1,
            )
            parts = [part for part in parts if roi.x <= part.center[0] <= roi.x2]
            maximum_plausible = max(2, int(np.ceil(roi.width / max(4.0, typical_width * 0.55))))
            widths_are_plausible = all(
                typical_width * 0.5 <= part.width <= typical_width * 1.6
                for part in parts
            )
            if 2 <= len(parts) <= maximum_plausible and widths_are_plausible:
                output.extend(replace(part, uid=0, name="", source="自动分峰") for part in parts)
            else:
                output.append(roi)
    return output


def detect_bands_in_region(
    gray: np.ndarray,
    region: tuple[int, int, int, int],
    sensitivity: float = 55,
    min_area: int = 30,
    polarity: Polarity = "auto",
    first_uid: int = 1,
    band_count: int | None = None,
    full_detection: tuple[Iterable[BandROI], str] | None = None,
) -> tuple[list[BandROI], str]:
    """Detect multiple bands inside an image region.

    ``region`` is ``(x1, y1, x2, y2)`` in original-image coordinates. The
    returned ROIs are mapped back to original-image coordinates.
    """

    if gray.ndim != 2:
        raise ValueError("detect_bands_in_region 需要灰度图")
    h, w = gray.shape
    x1, y1, x2, y2 = (int(round(v)) for v in region)
    x1, x2 = sorted((int(np.clip(x1, 0, w)), int(np.clip(x2, 0, w))))
    y1, y2 = sorted((int(np.clip(y1, 0, h)), int(np.clip(y2, 0, h))))
    if x2 - x1 < 3 or y2 - y1 < 3:
        raise ValueError("识别区域太小")
    if band_count is not None:
        band_count = int(band_count)
        if band_count < 1:
            raise ValueError("指定条带数必须为正整数")

    # Combine full-image candidates with two context-aware local passes. Full
    # boxes remain authoritative where available; local passes add weak lanes
    # that global thresholding missed.
    if full_detection is None:
        full_rois, actual = detect_bands(
            gray,
            sensitivity=sensitivity,
            min_area=min_area,
            polarity=polarity,
            first_uid=first_uid,
        )
    else:
        cached_rois, actual = full_detection
        full_rois = list(cached_rois)
    def matches_selection(roi: BandROI) -> bool:
        cx, cy = roi.center
        intersection_width = max(0, min(roi.x2, x2) - max(roi.x, x1))
        intersection_height = max(0, min(roi.y2, y2) - max(roi.y, y1))
        overlap = intersection_width * intersection_height / max(1, roi.width * roi.height)
        return (x1 <= cx <= x2 and y1 <= cy <= y2) or overlap >= 0.35

    full_matches = [roi for roi in full_rois if matches_selection(roi)]
    region_width, region_height = x2 - x1, y2 - y1

    def projection_from(candidates: Iterable[BandROI]) -> list[BandROI]:
        candidate_list = list(candidates)
        width_hint = (
            float(np.percentile([roi.width for roi in candidate_list], 40))
            if candidate_list
            else max(8.0, region_width / 12.0)
        )
        if band_count is not None:
            width_hint = max(width_hint, region_width / (band_count * 1.25))
        projected, _ = detect_bands_by_row_projection(
            gray,
            (x1, y1, x2, y2),
            polarity=actual,
            expected_width=width_hint,
            first_uid=first_uid,
            band_count=band_count,
        )
        return projected

    horizontal_selection = region_width >= region_height * 2.5 or band_count is not None
    if horizontal_selection:
        # Row projection is ultimately authoritative for a horizontal
        # selection. Try it before two expensive 2-D context scans; full-image
        # candidates already provide the same lane-width hint. Only fall back
        # to the context scans when projection lacks enough evidence.
        early_projection = projection_from(full_matches)
        if len(early_projection) >= 2 or (band_count == 1 and len(early_projection) == 1):
            return [
                replace(roi, uid=first_uid + offset, name=f"Band {first_uid + offset}")
                for offset, roi in enumerate(early_projection)
            ], actual

    # Analyse a padded context rather than the tight selection so background
    # estimation sees enough surrounding blot. The enhanced pass lowers the
    # threshold and minimum area specifically to recover shallow bands.
    margin_x = max(12, int(round(region_width * 0.08)))
    margin_y = max(20, int(round(region_height * 2.0)))
    cx1, cy1 = max(0, x1 - margin_x), max(0, y1 - margin_y)
    cx2, cy2 = min(w, x2 + margin_x), min(h, y2 + margin_y)
    context = gray[cy1:cy2, cx1:cx2]
    local_matches: list[BandROI] = []
    local_sensitivities = sorted({
        float(np.clip(sensitivity, 1, 100)),
        float(np.clip(sensitivity + 18, 1, 100)),
    })
    for local_sensitivity in local_sensitivities:
        local_rois, _ = detect_bands(
            context,
            sensitivity=local_sensitivity,
            min_area=max(4, int(round(min_area * 0.55))),
            polarity=actual,
            first_uid=1,
        )
        mapped = [replace(roi, x=roi.x + cx1, y=roi.y + cy1) for roi in local_rois]
        local_matches.extend(roi for roi in mapped if matches_selection(roi))

    combined: list[BandROI] = list(full_matches)
    for candidate in local_matches:
        duplicate = False
        for existing in combined:
            cx1_existing, cy1_existing = existing.center
            cx2_candidate, cy2_candidate = candidate.center
            centre_close = (
                abs(cx1_existing - cx2_candidate) <= max(3.0, min(existing.width, candidate.width) * 0.38)
                and abs(cy1_existing - cy2_candidate) <= max(existing.height, candidate.height) * 0.8
            )
            if _intersection_over_min_area(existing, candidate) >= 0.28 or centre_close:
                duplicate = True
                break
        if not duplicate:
            combined.append(candidate)

    combined = assign_lanes(combined)
    # A clearly horizontal one-row selection is better represented by x-axis
    # peaks than by connected components. This pass is authoritative: it both
    # splits touching bands and adds shallow peaks missed by all binary masks.
    if horizontal_selection:
        projection_rois = projection_from(combined)
        if len(projection_rois) >= 2 or (band_count == 1 and len(projection_rois) == 1):
            combined = projection_rois

    return [
        replace(roi, uid=first_uid + offset, name=f"Band {first_uid + offset}")
        for offset, roi in enumerate(combined)
    ], actual


def expand_band_rois(
    rois: Iterable[BandROI],
    bounds: tuple[int, int, int, int],
    padding: int = 6,
    gap: int = 0,
) -> list[BandROI]:
    """Expand detected ROIs without overlapping neighbouring band ROIs.

    The expansion is clipped to ``bounds``. Horizontal limits use midpoints
    between lane centres; vertical limits use midpoints between bands in the
    same lane. ``gap`` reserves background pixels around each shared midpoint,
    so detector boxes neither overlap nor visually stick together.
    """

    items = assign_lanes(rois)
    if not items:
        return items
    bx1, by1, bx2, by2 = bounds
    bx1, bx2 = sorted((int(bx1), int(bx2)))
    by1, by2 = sorted((int(by1), int(by2)))
    padding = max(0, int(padding))
    gap = max(0, int(gap))
    half_gap = gap / 2.0

    lane_centres: dict[int, float] = {}
    for lane in sorted({r.lane for r in items}):
        lane_centres[lane] = float(np.median([r.center[0] for r in items if r.lane == lane]))
    ordered_lanes = sorted(lane_centres, key=lane_centres.get)

    expanded: list[BandROI] = []
    for roi in items:
        lane_index = ordered_lanes.index(roi.lane)
        has_left_neighbour = lane_index > 0
        has_right_neighbour = lane_index + 1 < len(ordered_lanes)
        left_limit = float(bx1)
        right_limit = float(bx2)
        if has_left_neighbour:
            previous = lane_centres[ordered_lanes[lane_index - 1]]
            left_limit = (previous + lane_centres[roi.lane]) / 2
        if has_right_neighbour:
            following = lane_centres[ordered_lanes[lane_index + 1]]
            right_limit = (lane_centres[roi.lane] + following) / 2

        same_lane = sorted((r for r in items if r.lane == roi.lane), key=lambda r: r.center[1])
        row_index = next(i for i, item in enumerate(same_lane) if item.uid == roi.uid)
        has_top_neighbour = row_index > 0
        has_bottom_neighbour = row_index + 1 < len(same_lane)
        top_limit = float(by1)
        bottom_limit = float(by2)
        if has_top_neighbour:
            top_limit = (same_lane[row_index - 1].center[1] + roi.center[1]) / 2
        if has_bottom_neighbour:
            bottom_limit = (roi.center[1] + same_lane[row_index + 1].center[1]) / 2

        candidate_x1 = max(bx1, roi.x - padding)
        candidate_x2 = min(bx2, roi.x2 + padding)
        candidate_y1 = max(by1, roi.y - padding)
        candidate_y2 = min(by2, roi.y2 + padding)
        if has_left_neighbour:
            candidate_x1 = max(candidate_x1, int(np.ceil(left_limit + half_gap)))
        if has_right_neighbour:
            candidate_x2 = min(candidate_x2, int(np.floor(right_limit - half_gap)))
        if has_top_neighbour:
            candidate_y1 = max(candidate_y1, int(np.ceil(top_limit + half_gap)))
        if has_bottom_neighbour:
            candidate_y2 = min(candidate_y2, int(np.floor(bottom_limit - half_gap)))

        expanded.append(replace(
            roi,
            x=int(candidate_x1),
            y=int(candidate_y1),
            width=max(1, int(candidate_x2 - candidate_x1)),
            height=max(1, int(candidate_y2 - candidate_y1)),
        ))
    # Final geometry invariant independent of lane labels: candidates in the
    # same visual row may never overlap horizontally. This catches any earlier
    # lane-clustering mistake before pixels are quantified.
    if len(expanded) >= 2:
        median_expanded_height = float(np.median([item.height for item in expanded]))
        row_tolerance = max(4.0, median_expanded_height * 0.7)
        visual_rows: list[dict[str, object]] = []
        for index in sorted(range(len(expanded)), key=lambda idx: expanded[idx].center[1]):
            cy = expanded[index].center[1]
            nearest = min(
                visual_rows,
                key=lambda row: abs(float(row["center"]) - cy),
                default=None,
            )
            if nearest is None or abs(float(nearest["center"]) - cy) > row_tolerance:
                visual_rows.append({"center": cy, "indices": [index]})
            else:
                indices = nearest["indices"]
                assert isinstance(indices, list)
                indices.append(index)
                nearest["center"] = float(np.mean([expanded[idx].center[1] for idx in indices]))

        for row in visual_rows:
            indices = row["indices"]
            assert isinstance(indices, list)
            indices.sort(key=lambda idx: expanded[idx].center[0])
            for left_index, right_index in zip(indices, indices[1:]):
                left_roi = expanded[left_index]
                right_roi = expanded[right_index]
                if left_roi.x2 + gap <= right_roi.x:
                    continue
                midpoint = (left_roi.center[0] + right_roi.center[0]) / 2
                left_end = int(np.floor(midpoint - half_gap))
                right_start = int(np.ceil(midpoint + half_gap))
                left_end = max(left_roi.x + 1, min(left_roi.x2, left_end))
                right_start = min(right_roi.x2 - 1, max(right_roi.x, right_start))
                expanded[left_index] = replace(left_roi, width=left_end - left_roi.x)
                expanded[right_index] = replace(
                    right_roi,
                    x=right_start,
                    width=right_roi.x2 - right_start,
                )
    return assign_lanes(expanded)


def clamp_roi(roi: BandROI, shape: tuple[int, int]) -> BandROI:
    h, w = shape
    x1 = int(np.clip(roi.x, 0, max(0, w - 1)))
    y1 = int(np.clip(roi.y, 0, max(0, h - 1)))
    x2 = int(np.clip(roi.x2, x1 + 1, w))
    y2 = int(np.clip(roi.y2, y1 + 1, h))
    return replace(roi, x=x1, y=y1, width=x2 - x1, height=y2 - y1)


def measure_band(
    gray: np.ndarray,
    roi: BandROI,
    polarity: Polarity = "auto",
    ring_width: int | None = None,
) -> BandMeasurement:
    """Measure a band using a local rectangular background ring."""

    roi = clamp_roi(roi, gray.shape)
    actual = infer_polarity(gray) if polarity == "auto" else polarity
    h, w = gray.shape
    ring = ring_width if ring_width is not None else max(3, int(round(min(roi.width, roi.height) * 0.45)))
    ox1, oy1 = max(0, roi.x - ring), max(0, roi.y - ring)
    ox2, oy2 = min(w, roi.x2 + ring), min(h, roi.y2 + ring)
    outer = gray[oy1:oy2, ox1:ox2].astype(np.float64)
    ring_mask = np.ones(outer.shape, dtype=bool)
    ring_mask[roi.y - oy1 : roi.y2 - oy1, roi.x - ox1 : roi.x2 - ox1] = False
    bg_values = outer[ring_mask]
    pixels = gray[roi.y : roi.y2, roi.x : roi.x2].astype(np.float64)
    background = float(np.median(bg_values)) if bg_values.size else float(np.median(pixels))
    mean_gray = float(np.mean(pixels))
    if actual == "dark":
        corrected_pixels = np.clip(background - pixels, 0, None)
        corrected_mean = background - mean_gray
    else:
        corrected_pixels = np.clip(pixels - background, 0, None)
        corrected_mean = mean_gray - background
    corrected_mean = max(0.0, float(corrected_mean))
    return BandMeasurement(
        uid=roi.uid,
        name=roi.name,
        lane=roi.lane,
        x=roi.x,
        y=roi.y,
        width=roi.width,
        height=roi.height,
        area=int(pixels.size),
        mean_gray=mean_gray,
        background=background,
        corrected_mean=corrected_mean,
        integrated_density=float(np.sum(corrected_pixels)),
        source=roi.source,
    )


def measure_all(
    gray: np.ndarray,
    rois: Iterable[BandROI],
    polarity: Polarity = "auto",
    reference_uid: int | None = None,
) -> list[BandMeasurement]:
    # Inferring automatic polarity applies Gaussian filtering to the complete
    # image. Do it once per image refresh, not once for every individual ROI.
    actual = infer_polarity(gray) if polarity == "auto" else polarity
    measurements = [measure_band(gray, roi, actual) for roi in rois]
    if not measurements:
        return []
    reference = next((m.integrated_density for m in measurements if m.uid == reference_uid), None)
    if reference is None:
        reference = measurements[0].integrated_density
    denominator = reference if reference and reference > 1e-12 else 1.0
    for m in measurements:
        m.relative = m.integrated_density / denominator
    return measurements


def filter_bands_below_mean_signal(
    gray: np.ndarray,
    rois: Iterable[BandROI],
    polarity: Polarity = "auto",
    region: tuple[int, int, int, int] | None = None,
) -> tuple[list[BandROI], float]:
    """Remove candidates weaker than the selected region's mean signal.

    Signal is the local-background-corrected mean, not raw pixel gray. This
    keeps the direction consistent for dark and light bands: a larger value
    always represents a stronger band. When ``region`` is provided, its median
    gray estimates background and the mean positive signal of all region
    pixels (including background) becomes the threshold. This retains genuine
    weak bands better than averaging candidate-band strengths. The returned
    float is the threshold.
    """

    items = list(rois)
    if not items:
        return [], 0.0
    measurements = [measure_band(gray, roi, polarity) for roi in items]
    candidate_scores = [measurement.corrected_mean for measurement in measurements]
    if region is not None:
        h, w = gray.shape
        x1, y1, x2, y2 = (int(round(value)) for value in region)
        x1, x2 = sorted((int(np.clip(x1, 0, w)), int(np.clip(x2, 0, w))))
        y1, y2 = sorted((int(np.clip(y1, 0, h)), int(np.clip(y2, 0, h))))
        patch = gray[y1:y2, x1:x2].astype(np.float64)
        if patch.size:
            actual = infer_polarity(gray) if polarity == "auto" else polarity
            background = float(np.median(patch))
            region_signal = (
                np.clip(background - patch, 0, None)
                if actual == "dark"
                else np.clip(patch - background, 0, None)
            )
            threshold = float(np.mean(region_signal))
            actual = infer_polarity(gray) if polarity == "auto" else polarity
            candidate_scores = []
            for roi, measurement in zip(items, measurements):
                pixels = gray[roi.y : roi.y2, roi.x : roi.x2].astype(np.float64)
                core_signal = (
                    np.clip(measurement.background - pixels, 0, None)
                    if actual == "dark"
                    else np.clip(pixels - measurement.background, 0, None)
                )
                # The ROI may intentionally include background margin. Its
                # upper-fifth core signal remains representative of a weak
                # band while uniform background noise stays below threshold.
                candidate_scores.append(float(np.percentile(core_signal, 80)))
        else:
            threshold = 0.0
    else:
        # Backward-compatible fallback for callers without a selection region.
        threshold = float(np.mean([m.corrected_mean for m in measurements]))
    if len(items) == 1 or threshold <= 0:
        return items, threshold
    kept = [roi for roi, score in zip(items, candidate_scores) if score >= threshold]
    if not kept:
        strongest = int(np.argmax(candidate_scores))
        kept = [items[strongest]]
    return assign_lanes(kept), threshold
