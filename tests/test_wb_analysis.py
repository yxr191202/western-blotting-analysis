import numpy as np

from wb_analysis import (
    BandROI,
    align_rois_by_rows,
    align_rois_to_region_signal,
    assign_lanes,
    consolidate_auto_row_fragments,
    detect_bands,
    detect_bands_by_row_projection,
    detect_bands_in_region,
    expand_band_rois,
    filter_bands_below_mean_signal,
    filter_marker_lanes,
    measure_all,
    measure_band,
    normalize_target_to_reference_by_lane,
)


def synthetic_blot():
    rng = np.random.default_rng(42)
    image = rng.normal(220, 2, (240, 360))
    bands = [(35, 45, 45, 11), (135, 47, 48, 12), (238, 44, 52, 13),
             (34, 140, 47, 12), (136, 142, 50, 11), (240, 138, 49, 14)]
    for x, y, w, h in bands:
        image[y:y+h, x:x+w] = rng.normal(70, 3, (h, w))
    return np.clip(image, 0, 255).astype(np.uint8), bands


def test_detects_dark_bands():
    image, expected = synthetic_blot()
    rois, polarity = detect_bands(image, sensitivity=55, min_area=60)
    assert polarity == "dark"
    assert 5 <= len(rois) <= 7
    for x, y, w, h in expected:
        assert any(r.x < x + w and r.x2 > x and r.y < y + h and r.y2 > y for r in rois)


def test_full_image_detection_splits_nearly_touching_bands():
    rng = np.random.default_rng(2)
    image = rng.normal(225, 2, (120, 360))
    # The first two 28 px bands have only one background pixel between them.
    for x in (20, 49, 110, 170, 230):
        image[50:62, x:x + 28] = rng.normal(55, 3, (12, 28))
    image = np.clip(image, 0, 255).astype(np.uint8)
    rois, _ = detect_bands(image, sensitivity=55, min_area=20, polarity="dark")
    assert len(rois) == 5
    centres = [round(roi.center[0]) for roi in rois]
    assert any(abs(center - 34) <= 4 for center in centres)
    assert any(abs(center - 63) <= 4 for center in centres)
    expanded = expand_band_rois(rois, (0, 0, 360, 120), padding=6, gap=0)
    for x in (20, 49, 110, 170, 230):
        assert any(
            roi.x <= x and roi.x2 >= x + 28 and roi.y <= 50 and roi.y2 >= 62
            for roi in expanded
        )


def test_full_image_detection_does_not_split_one_genuinely_wide_band():
    rng = np.random.default_rng(3)
    image = rng.normal(225, 2, (120, 360))
    image[50:62, 20:80] = rng.normal(70, 2, (12, 60))
    for x in (120, 180, 240):
        image[50:62, x:x + 30] = rng.normal(60, 2, (12, 30))
    image = np.clip(image, 0, 255).astype(np.uint8)
    rois, _ = detect_bands(image, sensitivity=55, min_area=20, polarity="dark")
    assert len(rois) == 4
    assert sum(roi.width > 55 for roi in rois) == 1


def test_auto_row_consolidation_merges_shallow_fragments_but_not_true_gap():
    image = np.full((80, 180), 230, dtype=np.uint8)
    image[35:47, 20:52] = 55
    image[35:47, 90:112] = 60
    image[35:47, 116:138] = 60
    candidates = [
        BandROI(1, 20, 35, 14, 12),
        BandROI(2, 38, 35, 14, 12),
        BandROI(3, 90, 35, 22, 12),
        BandROI(4, 116, 35, 22, 12),
    ]
    rois = consolidate_auto_row_fragments(image, candidates, polarity="dark")
    assert len(rois) == 3
    assert any(roi.x <= 20 and roi.x2 >= 52 for roi in rois)
    assert sum(85 <= roi.center[0] <= 145 for roi in rois) == 2


def test_full_auto_row_projection_keeps_twelve_textured_grouped_bands():
    rng = np.random.default_rng(37)
    height, width = 300, 1500
    image = rng.normal(248, 1.0, (height, width))
    centres = [150, 245, 340, 435, 610, 705, 800, 895, 1070, 1165, 1260, 1355]
    for index, centre_x in enumerate(centres):
        band_width = 82
        left = centre_x - band_width // 2
        gray_value = 55 + 10 * (index % 4)
        image[150:205, left:left + band_width] = rng.normal(
            gray_value,
            3,
            (55, band_width),
        )
        # Add realistic vertical exposure smear above each band.
        for y in range(75, 150):
            strength = (150 - y) / 75
            image[y:y + 1, left:left + band_width] -= (
                28
                * strength
                * np.exp(-((np.arange(band_width) - band_width // 2) / (band_width * 0.32)) ** 2)
            )
        # Every second lane in each group has two dark lobes separated by a
        # saddle that remains well above background: texture, not two lanes.
        if index % 4 == 1:
            image[150:205, centre_x - 8:centre_x + 8] = rng.normal(120, 3, (55, 16))
    image = np.clip(image, 0, 255).astype(np.uint8)
    rois, _ = detect_bands(
        image,
        sensitivity=55,
        min_area=30,
        polarity="dark",
    )
    assert len(rois) == 12
    detected_centres = [round(roi.center[0]) for roi in rois]
    for expected_x in centres:
        assert any(abs(center - expected_x) <= 5 for center in detected_centres)
    assert all(roi.source == "自动行投影" for roi in rois)


def test_measurement_strength_and_background():
    image, _ = synthetic_blot()
    strong = measure_band(image, BandROI(1, 35, 45, 45, 11), "dark")
    empty = measure_band(image, BandROI(2, 85, 85, 45, 11), "dark")
    assert strong.integrated_density > 50_000
    assert strong.background > 200
    assert strong.integrated_density > empty.integrated_density * 20


def test_measure_all_infers_auto_polarity_only_once(monkeypatch):
    import wb_analysis as analysis_module

    image, bands = synthetic_blot()
    rois = [
        BandROI(index, x, y, width, height, lane=index)
        for index, (x, y, width, height) in enumerate(bands[:3], start=1)
    ]
    original = analysis_module.infer_polarity
    call_count = 0

    def counting_infer(gray):
        nonlocal call_count
        call_count += 1
        return original(gray)

    monkeypatch.setattr(analysis_module, "infer_polarity", counting_infer)
    measurements = measure_all(image, rois, polarity="auto")

    assert len(measurements) == 3
    assert call_count == 1


def test_lane_assignment():
    rois = [BandROI(1, 10, 10, 30, 8), BandROI(2, 12, 50, 30, 8), BandROI(3, 100, 12, 30, 8)]
    assigned = {r.uid: r.lane for r in assign_lanes(rois)}
    assert assigned == {1: 1, 2: 1, 3: 2}


def test_close_neighbouring_bands_are_not_mistaken_for_one_lane():
    rois = [
        BandROI(1, 14, 30, 55, 14),
        BandROI(2, 59, 30, 54, 14),
        BandROI(3, 110, 30, 52, 14),
    ]
    assigned = assign_lanes(rois)
    assert [roi.lane for roi in assigned] == [1, 2, 3]
    expanded = sorted(
        expand_band_rois(assigned, (0, 0, 200, 90), padding=6, gap=0),
        key=lambda roi: roi.x,
    )
    assert all(left.x2 <= right.x for left, right in zip(expanded, expanded[1:]))


def test_same_lane_across_rows_tolerates_small_horizontal_shift():
    rois = [BandROI(1, 40, 20, 50, 10), BandROI(2, 48, 70, 50, 10)]
    assigned = assign_lanes(rois)
    assert {roi.lane for roi in assigned} == {1}


def test_light_band_measurement():
    image = np.full((80, 100), 25, dtype=np.uint8)
    image[30:40, 25:70] = 210
    m = measure_band(image, BandROI(1, 25, 30, 45, 10), "light")
    assert m.background == 25
    assert m.corrected_mean == 185
    assert m.integrated_density == 45 * 10 * 185


def test_detects_multiple_bands_inside_selected_region():
    image, _ = synthetic_blot()
    # Select only the first horizontal row, like drawing one large rectangle
    # around multiple lanes in the GUI.
    rois, polarity = detect_bands_in_region(
        image, (20, 30, 315, 75), sensitivity=55, min_area=60, first_uid=20
    )
    assert polarity == "dark"
    assert len(rois) == 3
    assert [r.uid for r in rois] == [20, 21, 22]
    assert all(20 <= r.x < 315 and 30 <= r.y < 75 for r in rois)
    assert [r.lane for r in rois] == [1, 2, 3]


def test_region_detection_reuses_full_scan_and_skips_context_passes(monkeypatch):
    import wb_analysis as analysis_module

    image, _ = synthetic_blot()
    full_detection = detect_bands(
        image,
        sensitivity=55,
        min_area=60,
        polarity="dark",
        first_uid=1,
    )
    original = analysis_module.detect_bands
    call_count = 0

    def counting_detect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(analysis_module, "detect_bands", counting_detect)
    rois, _ = detect_bands_in_region(
        image,
        (20, 30, 315, 75),
        sensitivity=55,
        min_area=60,
        polarity="dark",
        full_detection=full_detection,
    )

    assert len(rois) == 3
    assert call_count == 0


def test_region_detection_keeps_closely_spaced_lanes_separate():
    rng = np.random.default_rng(7)
    image = rng.normal(232, 2, (90, 420))
    # 12 bands separated by only 4 pixels, representative of a tightly packed
    # row selected with the region tool.
    for index in range(12):
        x = 8 + index * 32
        image[38:48, x:x + 28] = rng.normal(58, 4, (10, 28))
    image = np.clip(image, 0, 255).astype(np.uint8)
    rois, _ = detect_bands_in_region(
        image, (3, 25, 390, 62), sensitivity=55, min_area=30, polarity="dark"
    )
    assert len(rois) == 12
    assert len({r.lane for r in rois}) == 12


def test_projection_splits_ultra_close_bands_when_the_valley_is_real():
    rng = np.random.default_rng(17)
    image = rng.normal(230, 1.5, (80, 250))
    # The first pair is closer than two-thirds of a normal 24 px band width,
    # but the two-pixel background valley shows that these are separate lanes.
    for x in (20, 34, 100, 170):
        image[34:46, x:x + 12] = rng.normal(55, 2, (12, 12))
    image = np.clip(image, 0, 255).astype(np.uint8)
    rois, _ = detect_bands_by_row_projection(
        image,
        (10, 22, 205, 58),
        polarity="dark",
        expected_width=24,
    )
    assert len(rois) == 4
    centres = [round(roi.center[0]) for roi in rois]
    for expected_x in (26, 40, 106, 176):
        assert any(abs(center - expected_x) <= 3 for center in centres)


def test_projection_does_not_split_shallow_lobes_inside_one_band():
    rng = np.random.default_rng(18)
    image = rng.normal(230, 1.5, (80, 250))
    image[34:46, 20:44] = rng.normal(60, 2, (12, 24))
    image[34:46, 100:124] = rng.normal(60, 2, (12, 24))
    image[34:46, 170:194] = rng.normal(60, 2, (12, 24))
    # One 24 px band with two lobes and a shallow internal saddle.
    image[34:46, 100:110] = rng.normal(48, 2, (12, 10))
    image[34:46, 110:114] = rng.normal(72, 2, (12, 4))
    image[34:46, 114:124] = rng.normal(48, 2, (12, 10))
    image = np.clip(image, 0, 255).astype(np.uint8)
    rois, _ = detect_bands_by_row_projection(
        image,
        (10, 22, 215, 58),
        polarity="dark",
        expected_width=24,
    )
    assert len(rois) == 3
    assert sum(90 <= roi.center[0] <= 135 for roi in rois) == 1


def test_projection_merges_deep_texture_saddle_when_row_spacing_shows_one_lane():
    rng = np.random.default_rng(181)
    image = rng.normal(230, 1.5, (90, 300))
    # Four physical lanes. The second is one broad band with two dark lobes
    # separated by a strong internal light streak. Its saddle is deeper than
    # the ordinary texture cutoff, but it never returns to background.
    image[38:52, 20:50] = rng.normal(60, 2, (14, 30))
    image[38:52, 82:100] = rng.normal(70, 2, (14, 18))
    image[38:52, 100:108] = rng.normal(175, 2, (14, 8))
    image[38:52, 108:126] = rng.normal(70, 2, (14, 18))
    image[38:52, 160:190] = rng.normal(90, 2, (14, 30))
    image[38:52, 225:255] = rng.normal(105, 2, (14, 30))
    image = np.clip(image, 0, 255).astype(np.uint8)

    rois, _ = detect_bands_by_row_projection(
        image,
        (10, 24, 270, 68),
        polarity="dark",
        expected_width=30,
    )

    assert len(rois) == 4
    centres = [round(roi.center[0]) for roi in rois]
    for expected_x in (35, 104, 175, 240):
        assert any(abs(center - expected_x) <= 6 for center in centres)
    assert sum(75 <= center <= 135 for center in centres) == 1


def test_region_count_guidance_keeps_requested_close_and_weak_bands():
    rng = np.random.default_rng(19)
    image = rng.normal(230, 1.5, (90, 300))
    # Two close bands plus one very weak band that is easy to miss with a
    # global threshold. The supplied count must make all five peak basins
    # authoritative.
    for x, gray_value in ((20, 55), (46, 62), (105, 211), (165, 70), (230, 65)):
        image[38:50, x:x + 24] = rng.normal(gray_value, 1.5, (12, 24))
    image = np.clip(image, 0, 255).astype(np.uint8)
    rois, _ = detect_bands_in_region(
        image,
        (10, 24, 270, 65),
        sensitivity=55,
        min_area=20,
        polarity="dark",
        band_count=5,
    )
    assert len(rois) == 5
    centres = [round(roi.center[0]) for roi in rois]
    for expected_x in (32, 58, 117, 177, 242):
        assert any(abs(center - expected_x) <= 5 for center in centres)


def test_count_guidance_rejects_marker_tail_crossing_selection_edge():
    rng = np.random.default_rng(23)
    yy, xx = np.mgrid[:100, :400]
    image = rng.normal(232, 1.2, (100, 400))
    # The Marker centre is outside x=48, but its broad tail remains dark at
    # that selection boundary. It must not consume one of the four requested
    # band slots.
    image -= 215 * np.exp(-(((xx - 20) / 48) ** 2 + ((yy - 53) / 16) ** 2))
    for centre_x, amplitude, sigma_x in (
        (125, 175, 24),
        (195, 150, 28),
        (270, 130, 30),
        (350, 105, 27),
    ):
        image -= amplitude * np.exp(-(((xx - centre_x) / sigma_x) ** 2 + ((yy - 55) / 11) ** 2))
    image = np.clip(image, 0, 255).astype(np.uint8)
    rois, _ = detect_bands_in_region(
        image,
        (48, 25, 392, 82),
        sensitivity=55,
        min_area=20,
        polarity="dark",
        band_count=4,
    )
    assert len(rois) == 4
    centres = [round(roi.center[0]) for roi in rois]
    for expected_x in (125, 195, 270, 350):
        assert any(abs(center - expected_x) <= 5 for center in centres)
    assert all(center > 90 for center in centres)


def test_count_guidance_keeps_strong_first_band_next_to_marker():
    rng = np.random.default_rng(29)
    yy, xx = np.mgrid[:110, :340]
    image = rng.normal(232, 1.2, (110, 340))
    # The Marker is outside the selected region, while a genuine strong first
    # lane begins close to the left selection edge and partially blends with
    # its tail. The first lane must remain and no later broad lane may be used
    # twice merely to reach the requested count.
    image -= 210 * np.exp(-(((xx - 20) / 32) ** 2 + ((yy - 58) / 15) ** 2))
    for centre_x, amplitude, sigma_x in (
        (72, 195, 21),
        (140, 145, 26),
        (205, 125, 27),
        (270, 105, 28),
    ):
        image -= amplitude * np.exp(-(((xx - centre_x) / sigma_x) ** 2 + ((yy - 58) / 11) ** 2))
    image = np.clip(image, 0, 255).astype(np.uint8)
    rois, _ = detect_bands_in_region(
        image,
        (55, 25, 315, 90),
        sensitivity=55,
        min_area=20,
        polarity="dark",
        band_count=4,
    )
    assert len(rois) == 4
    centres = [round(roi.center[0]) for roi in rois]
    for expected_x in (76, 142, 206, 273):
        assert any(abs(center - expected_x) <= 5 for center in centres)
    assert all(right - left >= 45 for left, right in zip(centres, centres[1:]))


def test_count_guidance_prefers_weak_far_lane_over_internal_double_peak():
    rng = np.random.default_rng(31)
    yy, xx = np.mgrid[:90, :250]
    image = rng.normal(230, 1.2, (90, 250))
    image -= 165 * np.exp(-(((xx - 25) / 15) ** 2 + ((yy - 45) / 8) ** 2))
    # One broad second lane contains two strong internal maxima.
    image -= 105 * np.exp(-(((xx - 76) / 12) ** 2 + ((yy - 45) / 8) ** 2))
    image -= 100 * np.exp(-(((xx - 94) / 12) ** 2 + ((yy - 45) / 8) ** 2))
    image -= 120 * np.exp(-(((xx - 145) / 18) ** 2 + ((yy - 45) / 8) ** 2))
    # The fourth lane is much weaker but occupies a distinct far-right lane.
    image -= 48 * np.exp(-(((xx - 205) / 18) ** 2 + ((yy - 45) / 8) ** 2))
    image = np.clip(image, 0, 255).astype(np.uint8)
    rois, _ = detect_bands_in_region(
        image,
        (5, 25, 235, 65),
        sensitivity=55,
        min_area=15,
        polarity="dark",
        band_count=4,
    )
    assert len(rois) == 4
    centres = [round(roi.center[0]) for roi in rois]
    for expected_x in (26, 86, 146, 203):
        assert any(abs(center - expected_x) <= 5 for center in centres)
    assert sum(65 <= center <= 105 for center in centres) == 1
    assert any(center >= 195 for center in centres)


def test_expands_region_boxes_without_reaching_neighbour_centres():
    rois = [
        BandROI(1, 20, 30, 20, 8, lane=1),
        BandROI(2, 70, 30, 20, 8, lane=2),
        BandROI(3, 120, 30, 20, 8, lane=3),
    ]
    expanded = expand_band_rois(rois, (10, 20, 150, 55), padding=8)
    assert [(r.x, r.y, r.width, r.height) for r in expanded] == [
        (12, 22, 36, 24),
        (62, 22, 36, 24),
        (112, 22, 36, 24),
    ]
    # Even excessive padding cannot cross the midpoint between lane centres.
    huge = expand_band_rois(rois, (10, 20, 150, 55), padding=100)
    assert huge[0].x2 <= 55
    assert huge[1].x >= 55 and huge[1].x2 <= 105
    assert huge[2].x >= 105


def test_expansion_separates_boxes_that_already_overlap():
    rois = [
        BandROI(1, 20, 30, 72, 14, lane=1),
        BandROI(2, 86, 30, 80, 14, lane=2),
        BandROI(3, 157, 30, 78, 14, lane=3),
        BandROI(4, 228, 30, 82, 14, lane=4),
    ]
    expanded = sorted(expand_band_rois(rois, (0, 0, 340, 80), padding=6, gap=2), key=lambda r: r.x)
    assert all(left.x2 <= right.x for left, right in zip(expanded, expanded[1:]))
    assert all(right.x - left.x2 >= 2 for left, right in zip(expanded, expanded[1:]))


def test_expansion_separates_vertical_boxes_in_the_same_lane():
    rois = [
        BandROI(1, 40, 20, 55, 28, lane=1),
        BandROI(2, 40, 42, 55, 30, lane=1),
    ]
    expanded = sorted(expand_band_rois(rois, (0, 0, 130, 100), padding=8, gap=2), key=lambda r: r.y)
    assert expanded[1].y - expanded[0].y2 >= 2


def test_zero_gap_keeps_boxes_separate_but_allows_shared_boundary():
    rois = [BandROI(1, 20, 30, 75, 12), BandROI(2, 88, 30, 75, 12)]
    expanded = sorted(expand_band_rois(rois, (0, 0, 200, 80), padding=8, gap=0), key=lambda r: r.x)
    assert 0 <= expanded[1].x - expanded[0].x2 <= 1


def test_row_alignment_repairs_short_shifted_first_band_box():
    rois = [
        BandROI(1, 20, 55, 48, 10),
        BandROI(2, 85, 42, 52, 16),
        BandROI(3, 150, 41, 54, 17),
        BandROI(4, 215, 43, 51, 15),
    ]
    aligned = align_rois_by_rows(rois)
    assert {(r.y, r.y2) for r in aligned} == {(41, 65)}
    first = next(r for r in aligned if r.uid == 1)
    assert first.width >= 51


def test_row_alignment_does_not_merge_distinct_protein_rows():
    rois = [
        BandROI(1, 20, 20, 45, 10),
        BandROI(2, 80, 22, 45, 10),
        BandROI(3, 20, 65, 45, 11),
        BandROI(4, 80, 67, 45, 11),
    ]
    aligned = align_rois_by_rows(rois)
    envelopes = sorted({(r.y, r.y2) for r in aligned})
    assert envelopes == [(20, 32), (65, 78)]


def test_region_projection_corrects_vertically_shifted_first_roi():
    image = np.full((100, 280), 220, dtype=np.uint8)
    # Four actual bands share the same row, but the first is weak and its
    # detector ROI is deliberately shifted down to mimic the reported case.
    image[40:55, 20:50] = 150
    for x in (80, 140, 200):
        image[40:55, x:x + 35] = 75
    rois = [
        BandROI(1, 20, 56, 28, 9),
        BandROI(2, 80, 40, 35, 15),
        BandROI(3, 140, 40, 35, 15),
        BandROI(4, 200, 40, 35, 15),
    ]
    aligned = align_rois_to_region_signal(
        image, rois, (10, 20, 250, 80), polarity="dark"
    )
    assert len({(roi.y, roi.y2) for roi in aligned}) == 1
    first = next(roi for roi in aligned if roi.uid == 1)
    assert first.y <= 40
    assert first.y2 >= 65


def test_region_projection_does_not_make_tall_boxes_from_complex_rows():
    image = np.full((130, 280), 220, dtype=np.uint8)
    for x in (20, 85, 150, 215):
        image[20:33, x:x + 35] = 65
        image[72:88, x:x + 35] = 105
    rois = [
        BandROI(1, 20, 20, 35, 13),
        BandROI(2, 85, 72, 35, 16),
        BandROI(3, 150, 20, 35, 13),
        BandROI(4, 215, 72, 35, 16),
    ]
    aligned = align_rois_to_region_signal(
        image, rois, (10, 5, 265, 105), polarity="dark"
    )
    envelopes = sorted({(roi.y, roi.y2) for roi in aligned})
    assert len(envelopes) == 2
    assert all(bottom - top < 30 for top, bottom in envelopes)


def test_region_projection_constrains_vertical_smear_candidates():
    image = np.full((120, 260), 220, dtype=np.uint8)
    for x in (20, 80, 140, 200):
        image[18:32, x:x + 32] = 55
        image[33:90, x:x + 32] = 175
    tall = [BandROI(uid, x, 15, 34, 80) for uid, x in enumerate((20, 80, 140, 200), 1)]
    aligned = align_rois_to_region_signal(
        image, tall, (10, 5, 245, 105), polarity="dark"
    )
    assert all(roi.height <= roi.width for roi in aligned)


def test_filters_candidates_below_region_mean_corrected_signal():
    image = np.full((100, 240), 220, dtype=np.uint8)
    values = [40, 60, 80, 205]
    rois = []
    for index, gray_value in enumerate(values):
        x = 15 + index * 55
        image[40:50, x:x + 35] = gray_value
        rois.append(BandROI(index + 1, x, 40, 35, 10, lane=index + 1))
    kept, threshold = filter_bands_below_mean_signal(image, rois, "dark")
    assert threshold > 100
    assert [r.uid for r in kept] == [1, 2, 3]


def test_mean_signal_filter_uses_correct_direction_for_light_bands():
    image = np.full((80, 180), 25, dtype=np.uint8)
    image[30:40, 15:50] = 210
    image[30:40, 70:105] = 170
    image[30:40, 125:160] = 40
    rois = [
        BandROI(1, 15, 30, 35, 10, lane=1),
        BandROI(2, 70, 30, 35, 10, lane=2),
        BandROI(3, 125, 30, 35, 10, lane=3),
    ]
    kept, _ = filter_bands_below_mean_signal(image, rois, "light")
    assert [r.uid for r in kept] == [1, 2]


def test_region_mean_filter_keeps_legitimate_weak_bands_and_drops_noise():
    image = np.full((100, 270), 220, dtype=np.uint8)
    rois = []
    for uid, (x, gray_value) in enumerate(
        zip((15, 65, 115, 165), (40, 100, 140, 175)), start=1
    ):
        image[40:50, x:x + 30] = gray_value
        rois.append(BandROI(uid, x, 40, 30, 10, lane=uid))
    # A barely darker patch represents a background/noise candidate.
    image[40:50, 220:245] = 215
    rois.append(BandROI(5, 220, 40, 25, 10, lane=5))
    kept, threshold = filter_bands_below_mean_signal(
        image, rois, "dark", region=(5, 25, 255, 70)
    )
    assert 5 < threshold < 45
    assert [r.uid for r in kept] == [1, 2, 3, 4]


def test_region_mean_filter_keeps_weak_light_bands():
    image = np.full((90, 220), 25, dtype=np.uint8)
    rois = []
    for uid, (x, gray_value) in enumerate(zip((15, 65, 115), (210, 145, 80)), start=1):
        image[35:45, x:x + 30] = gray_value
        rois.append(BandROI(uid, x, 35, 30, 10, lane=uid))
    image[35:45, 170:195] = 30
    rois.append(BandROI(4, 170, 35, 25, 10, lane=4))
    kept, _ = filter_bands_below_mean_signal(
        image, rois, "light", region=(5, 20, 205, 60)
    )
    assert [r.uid for r in kept] == [1, 2, 3]


def test_region_detection_reuses_full_image_boxes_for_tight_selection():
    image, _ = synthetic_blot()
    full, _ = detect_bands(image, sensitivity=55, min_area=60, polarity="dark")
    # Deliberately draw a vertically tight region through the first row. The
    # returned boxes should remain the complete full-image boxes, not fragments
    # clipped or recalculated inside this short crop.
    region_bounds = (20, 48, 315, 54)
    region, _ = detect_bands_in_region(
        image, region_bounds, sensitivity=55, min_area=60, polarity="dark", first_uid=100
    )
    expected = [
        roi for roi in full
        if region_bounds[0] <= roi.center[0] <= region_bounds[2]
        and region_bounds[1] <= roi.center[1] <= region_bounds[3]
    ]
    assert [(r.x, r.y, r.width, r.height) for r in region] == [
        (r.x, r.y, r.width, r.height) for r in expected
    ]
    assert any(r.y < region_bounds[1] and r.y2 > region_bounds[3] for r in region)


def test_region_hybrid_detection_adds_weak_bands_missed_by_full_scan():
    rng = np.random.default_rng(1)
    image = rng.normal(225, 2, (420, 620))
    # Strong bands elsewhere dominate the global threshold.
    for y in (40, 90, 330):
        for x in range(20, 580, 45):
            image[y:y + 12, x:x + 32] = rng.normal(45, 3, (12, 32))
    image[205:217, 80:115] = 50
    for x in (180, 280, 380):
        image[205:217, x:x + 35] = rng.normal(200, 2, (12, 35))
    image = np.clip(image, 0, 255).astype(np.uint8)
    bounds = (50, 180, 450, 240)
    full, _ = detect_bands(image, sensitivity=55, min_area=30, polarity="dark")
    full_inside = [
        roi for roi in full
        if bounds[0] <= roi.center[0] <= bounds[2] and bounds[1] <= roi.center[1] <= bounds[3]
    ]
    region, _ = detect_bands_in_region(
        image, bounds, sensitivity=55, min_area=30, polarity="dark"
    )
    assert len(full_inside) < 4
    assert len(region) >= 4
    for expected_x in (98, 198, 298, 398):
        assert any(abs(roi.center[0] - expected_x) <= 20 for roi in region)


def test_full_region_pipeline_splits_close_bands_and_retains_shallow_bands():
    rng = np.random.default_rng(1)
    image = rng.normal(225, 2, (100, 360))
    # The first pair has only a 3 px gap; the fourth and sixth bands are shallow.
    for x, gray_value in ((20, 55), (51, 65), (100, 75), (150, 190), (210, 80), (270, 200)):
        image[42:54, x:x + 28] = rng.normal(gray_value, 2, (12, 28))
    image = np.clip(image, 0, 255).astype(np.uint8)
    bounds = (10, 25, 320, 72)
    rois, actual = detect_bands_in_region(
        image, bounds, sensitivity=55, min_area=20, polarity="dark"
    )
    rois, _ = filter_marker_lanes(rois, "auto")
    rois = align_rois_to_region_signal(image, rois, bounds, actual)
    rois = expand_band_rois(rois, (0, 0, 360, 100), padding=6, gap=0)
    rois, _ = filter_bands_below_mean_signal(image, rois, actual, region=bounds)
    assert len(rois) == 6
    centres = [round(roi.center[0]) for roi in rois]
    for expected_x in (34, 65, 114, 164, 224, 284):
        assert any(abs(center - expected_x) <= 5 for center in centres)


def test_auto_marker_filter_removes_stacked_edge_fragments():
    marker = [
        BandROI(1, 8, 10, 24, 8),
        BandROI(2, 10, 28, 30, 9),
        BandROI(3, 7, 48, 22, 8),
        BandROI(4, 30, 49, 15, 8),
    ]
    samples = [
        BandROI(5, 75, 30, 55, 12),
        BandROI(6, 145, 30, 55, 12),
        BandROI(7, 215, 30, 55, 12),
    ]
    kept, removed = filter_marker_lanes(marker + samples, "auto")
    assert {r.uid for r in removed} == {1, 2, 3, 4}
    assert {r.uid for r in kept} == {5, 6, 7}


def test_auto_marker_filter_keeps_normal_multirow_sample_lanes():
    rois = []
    uid = 1
    for x in (20, 90, 160, 230):
        for y in (25, 65):
            rois.append(BandROI(uid, x, y, 45, 10))
            uid += 1
    kept, removed = filter_marker_lanes(rois, "auto")
    assert len(kept) == 8
    assert removed == []


def test_explicit_marker_side_can_remove_single_visible_marker_band():
    rois = [
        BandROI(1, 5, 30, 25, 9),
        BandROI(2, 70, 30, 50, 11),
        BandROI(3, 140, 30, 50, 11),
    ]
    kept, removed = filter_marker_lanes(rois, "left")
    assert [r.uid for r in removed] == [1]
    assert [r.uid for r in kept] == [2, 3]


def _measurement(uid, lane, integrated_density):
    """Build the minimum realistic measured band needed by pairing tests."""

    from wb_analysis import BandMeasurement

    return BandMeasurement(
        uid=uid,
        name=f"Band {uid}",
        lane=lane,
        x=0,
        y=0,
        width=10,
        height=5,
        area=50,
        mean_gray=100.0,
        background=200.0,
        corrected_mean=100.0,
        integrated_density=float(integrated_density),
    )


def test_lane_normalization_auto_pairs_unique_lanes_and_calculates_ratio():
    targets = [_measurement(101, 1, 600), _measurement(102, 2, 300)]
    references = [_measurement(201, 1, 200), _measurement(202, 2, 150)]

    results = normalize_target_to_reference_by_lane(targets, references)

    assert [(r.target_uid, r.reference_uid) for r in results] == [(101, 201), (102, 202)]
    assert [r.target_over_reference for r in results] == [3.0, 2.0]
    assert all(r.pairing_status == "auto_matched" for r in results)
    assert all(r.missing_reason == "" for r in results)


def test_lane_normalization_reports_missing_target_and_reference():
    targets = [_measurement(101, 1, 600)]
    references = [_measurement(202, 2, 150)]

    results = normalize_target_to_reference_by_lane(targets, references)

    assert [(r.lane, r.pairing_status) for r in results] == [
        (1, "missing_reference"),
        (2, "missing_target"),
    ]
    assert results[0].target_integrated_density == 600
    assert results[0].reference_integrated_density is None
    assert results[1].target_integrated_density is None
    assert results[1].reference_integrated_density == 150
    assert all(r.target_over_reference is None for r in results)
    assert all(r.missing_reason for r in results)


def test_lane_normalization_manual_pair_can_cross_lanes_and_be_confirmed():
    targets = [_measurement(101, 1, 600)]
    references = [_measurement(201, 3, 200)]

    manual_result = normalize_target_to_reference_by_lane(
        targets,
        references,
        manual_pairs={101: 201},
    )[0]
    assert manual_result.pairing_status == "manual_matched"
    assert manual_result.target_lane == 1
    assert manual_result.reference_lane == 3
    assert manual_result.target_over_reference == 3.0

    results = normalize_target_to_reference_by_lane(
        targets,
        references,
        manual_pairs={101: 201},
        confirmed_pairs={(101, 201)},
    )

    assert len(results) == 1
    result = results[0]
    assert result.target_over_reference == 3.0
    assert result.pairing_status == "confirmed"


def test_lane_normalization_does_not_guess_when_lane_has_duplicates():
    targets = [_measurement(101, 1, 600)]
    references = [_measurement(201, 1, 200), _measurement(202, 1, 150)]

    result = normalize_target_to_reference_by_lane(targets, references)[0]

    assert result.pairing_status == "ambiguous_reference"
    assert result.reference_uid is None
    assert result.target_over_reference is None
    assert "2 个条带" in result.missing_reason


def test_lane_normalization_rejects_zero_reference_and_duplicate_manual_use():
    targets = [_measurement(101, 1, 600), _measurement(102, 2, 300)]
    zero_reference = [_measurement(201, 1, 0)]
    zero_result = normalize_target_to_reference_by_lane(targets[:1], zero_reference)[0]
    assert zero_result.pairing_status == "zero_reference"
    assert zero_result.target_over_reference is None
    assert "积分灰度为 0" in zero_result.missing_reason

    references = [_measurement(201, 1, 200)]
    duplicate_results = normalize_target_to_reference_by_lane(
        targets,
        references,
        manual_pairs={101: 201, 102: 201},
    )
    assert {r.pairing_status for r in duplicate_results} == {"duplicate_reference"}
    assert all(r.target_over_reference is None for r in duplicate_results)
