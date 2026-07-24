"""Tkinter desktop application for Western blot grayscale analysis."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field, replace
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import numpy as np
from PIL import Image, ImageDraw, ImageTk

from wb_analysis import (
    BandMeasurement,
    BandROI,
    LaneNormalizationResult,
    align_rois_to_region_signal,
    assign_lanes,
    detect_bands,
    detect_bands_in_region,
    expand_band_rois,
    filter_bands_below_mean_signal,
    filter_marker_lanes,
    infer_polarity,
    measure_all,
    normalize_target_to_reference_by_lane,
    to_gray_array,
)


APP_TITLE = "WB 条带灰度分析"
PAIRING_STATUS_LABELS = {
    "auto_matched": "自动配对",
    "manual_matched": "手动配对",
    "confirmed": "已确认",
    "missing_reference": "缺少内参",
    "missing_target": "缺少目的",
    "ambiguous_target": "目的泳道重复",
    "ambiguous_reference": "内参泳道重复",
    "manual_unpaired": "手动未配对",
    "invalid_manual_reference": "配对已失效",
    "duplicate_reference": "配对冲突",
    "zero_reference": "内参为零",
}


@dataclass
class ImageState:
    key: str
    label: str
    image: Image.Image | None = None
    gray: np.ndarray | None = None
    path: Path | None = None
    rois: list[BandROI] = field(default_factory=list)
    reference_uid: int | None = None
    selected_uid: int | None = None
    preview_image: ImageTk.PhotoImage | None = None
    preview_size: tuple[int, int] | None = None
    detection_cache_key: tuple[float, int, str] | None = None
    detection_cache: tuple[list[BandROI], str] | None = None
    auto_polarity: str | None = None


class PairingManager(tk.Toplevel):
    """Review, confirm, and manually override target/reference pairings."""

    def __init__(self, parent: "WBAnalyzerApp") -> None:
        super().__init__(parent)
        self.parent_app = parent
        self.title("泳道配对管理")
        self.geometry("980x560")
        self.minsize(760, 430)
        self.transient(parent)
        self.reference_var = tk.StringVar(value="未配对")
        self.reference_uid_by_label: dict[str, int | None] = {}
        self.result_by_target_uid: dict[int, LaneNormalizationResult] = {}
        self._refreshing = False
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        ttk.Label(
            self,
            text="唯一同号 lane 会自动配对。选择目的条带后，可改用任意内参条带（允许跨 lane），并确认配对。",
            padding=(10, 9),
        ).pack(fill=tk.X)
        frame = ttk.Frame(self, padding=(10, 0, 10, 8))
        frame.pack(fill=tk.BOTH, expand=True)
        columns = ("lane", "target", "reference", "ratio", "status", "reason")
        self.tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        headings = {
            "lane": "目的 lane",
            "target": "目的条带",
            "reference": "内参条带",
            "ratio": "目的/内参",
            "status": "配对状态",
            "reason": "缺失原因/提示",
        }
        widths = {"lane": 75, "target": 150, "reference": 150, "ratio": 95, "status": 100, "reason": 330}
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], minwidth=60, anchor="center")
        scroll_y = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.tree.yview)
        scroll_x = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll_y.grid(row=0, column=1, sticky="ns")
        scroll_x.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", self._selection_changed)

        controls = ttk.LabelFrame(self, text="所选目的条带", padding=9)
        controls.pack(fill=tk.X, padx=10, pady=(0, 10))
        ttk.Label(controls, text="配对内参").pack(side=tk.LEFT)
        self.reference_combo = ttk.Combobox(
            controls,
            state="readonly",
            width=34,
            textvariable=self.reference_var,
        )
        self.reference_combo.pack(side=tk.LEFT, padx=(7, 8))
        ttk.Button(controls, text="应用手动配对", command=self._apply_manual).pack(side=tk.LEFT, padx=2)
        ttk.Button(controls, text="确认当前配对", command=self._confirm).pack(side=tk.LEFT, padx=2)
        ttk.Button(controls, text="恢复所选自动配对", command=self._reset_selected).pack(side=tk.LEFT, padx=2)
        ttk.Button(controls, text="全部恢复自动", command=self._reset_all).pack(side=tk.RIGHT, padx=2)

    def refresh(self, select_target_uid: int | None = None) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        try:
            self.parent_app._update_results()
            previous = select_target_uid
            if previous is None and self.tree.selection():
                previous = int(self.tree.selection()[0])
            self.tree.delete(*self.tree.get_children())
            target_measurements = {
                measurement.uid: measurement
                for key, measurement in self.parent_app.combined_measurements
                if key == "target"
            }
            reference_measurements = {
                measurement.uid: measurement
                for key, measurement in self.parent_app.combined_measurements
                if key == "reference"
            }
            self.result_by_target_uid = {
                result.target_uid: result
                for result in self.parent_app.normalized_results
                if result.target_uid is not None
            }
            for target_uid, result in self.result_by_target_uid.items():
                target = target_measurements[target_uid]
                reference = reference_measurements.get(result.reference_uid)
                target_text = f"ID {target.uid} | lane {target.lane} | {target.name}"
                reference_text = (
                    f"ID {reference.uid} | lane {reference.lane} | {reference.name}"
                    if reference is not None else "—"
                )
                ratio = "" if result.target_over_reference is None else f"{result.target_over_reference:.6f}"
                self.tree.insert(
                    "",
                    tk.END,
                    iid=str(target_uid),
                    values=(
                        result.target_lane,
                        target_text,
                        reference_text,
                        ratio,
                        PAIRING_STATUS_LABELS.get(result.pairing_status, result.pairing_status),
                        result.missing_reason,
                    ),
                )

            reference_labels = ["未配对"]
            self.reference_uid_by_label = {"未配对": None}
            for reference in sorted(reference_measurements.values(), key=lambda item: (item.lane, item.uid)):
                label = f"ID {reference.uid} | lane {reference.lane} | {reference.name}"
                reference_labels.append(label)
                self.reference_uid_by_label[label] = reference.uid
            self.reference_combo["values"] = reference_labels
            if previous is not None and self.tree.exists(str(previous)):
                self.tree.selection_set(str(previous))
                self.tree.see(str(previous))
            elif self.tree.get_children():
                self.tree.selection_set(self.tree.get_children()[0])
            self._selection_changed()
        finally:
            self._refreshing = False

    def _selected_target_uid(self) -> int | None:
        selection = self.tree.selection()
        return int(selection[0]) if selection else None

    def _selection_changed(self, _event: tk.Event | None = None) -> None:
        target_uid = self._selected_target_uid()
        if target_uid is None:
            self.reference_var.set("未配对")
            return
        result = self.result_by_target_uid[target_uid]
        matching_label = next(
            (
                label
                for label, uid in self.reference_uid_by_label.items()
                if uid == result.reference_uid
            ),
            "未配对",
        )
        self.reference_var.set(matching_label)

    def _apply_manual(self) -> None:
        target_uid = self._selected_target_uid()
        if target_uid is None:
            messagebox.showinfo(APP_TITLE, "请先选择一个目的条带。", parent=self)
            return
        reference_uid = self.reference_uid_by_label.get(self.reference_var.get())
        duplicate_target = next(
            (
                other_target_uid
                for other_target_uid, other_reference_uid in self.parent_app.pairing_overrides.items()
                if (
                    other_target_uid != target_uid
                    and reference_uid is not None
                    and other_reference_uid == reference_uid
                )
            ),
            None,
        )
        if duplicate_target is not None:
            messagebox.showerror(
                APP_TITLE,
                f"该内参已被目的条带 ID {duplicate_target} 手动使用；一个内参条带只能配对一个目的条带。",
                parent=self,
            )
            return
        self.parent_app.set_manual_pair(target_uid, reference_uid)
        self.refresh(target_uid)

    def _confirm(self) -> None:
        target_uid = self._selected_target_uid()
        if target_uid is None:
            return
        if self.parent_app.confirm_target_pair(target_uid, parent=self):
            self.refresh(target_uid)

    def _reset_selected(self) -> None:
        target_uid = self._selected_target_uid()
        if target_uid is None:
            return
        self.parent_app.reset_pairing(target_uid)
        self.refresh(target_uid)

    def _reset_all(self) -> None:
        if not messagebox.askyesno(
            APP_TITLE,
            "确定清除全部手动调整和确认状态，重新按 lane 自动配对吗？",
            parent=self,
        ):
            return
        self.parent_app.reset_all_pairings()
        self.refresh()


class WBAnalyzerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        minimum_width = min(1050, max(760, screen_width - 40))
        minimum_height = min(680, max(540, screen_height - 80))
        initial_width = max(minimum_width, min(1480, screen_width - 80))
        initial_height = max(minimum_height, min(850, screen_height - 100))
        self.geometry(f"{initial_width}x{initial_height}")
        self.minsize(minimum_width, minimum_height)

        self.original_image: Image.Image | None = None
        self.gray: np.ndarray | None = None
        self.image_path: Path | None = None
        self.tk_image: ImageTk.PhotoImage | None = None
        self.rois: list[BandROI] = []
        self.measurements = []
        self.reference_uid: int | None = None
        self.selected_uid: int | None = None
        self.next_uid = 1
        self.image_states = {
            "reference": ImageState("reference", "内参"),
            "target": ImageState("target", "目的"),
        }
        self.active_image_key = "reference"
        self.combined_measurements: list[tuple[str, BandMeasurement]] = []
        self.normalized_results: list[LaneNormalizationResult] = []
        self.pairing_result_by_uid: dict[int, LaneNormalizationResult] = {}
        self.pairing_overrides: dict[int, int | None] = {}
        self.confirmed_pairings: set[tuple[int, int]] = set()
        self.pairing_manager: PairingManager | None = None

        self.zoom = 1.0
        self.image_origin = (0.0, 0.0)
        self.drag_start: tuple[float, float] | None = None
        self.preview_item: int | None = None

        self.mode_var = tk.StringVar(value="region")
        self.active_image_var = tk.StringVar(value="reference")
        self.category_name_vars = {
            "reference": tk.StringVar(value=self.image_states["reference"].label),
            "target": tk.StringVar(value=self.image_states["target"].label),
        }
        self.polarity_var = tk.StringVar(value="auto")
        self.sensitivity_var = tk.DoubleVar(value=55)
        self.min_area_var = tk.IntVar(value=30)
        self.region_padding_var = tk.IntVar(value=6)
        self.roi_gap_var = tk.IntVar(value=0)
        self.region_band_count_var = tk.StringVar(value="0")
        self.filter_below_mean_var = tk.BooleanVar(value=True)
        self.marker_mode_var = tk.StringVar(value="auto")
        self.status_var = tk.StringVar(value="请先选择内参图像或目的图像")
        self._build_ui()
        self._bind_events()

    def _build_ui(self) -> None:
        self.option_add("*Font", ("Arial", 12))
        toolbar = ttk.Frame(self, padding=(8, 7))
        toolbar.pack(fill=tk.X)
        ttk.Button(toolbar, text="选择内参图像", command=lambda: self.open_image("reference")).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="选择目的图像", command=lambda: self.open_image("target")).pack(side=tk.LEFT, padx=2)
        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=7)
        ttk.Button(toolbar, text="全图自动识别", command=self.auto_detect).pack(side=tk.LEFT, padx=2)
        ttk.Radiobutton(
            toolbar,
            text="拆分所选区域",
            value="region",
            variable=self.mode_var,
        ).pack(side=tk.LEFT, padx=3)
        ttk.Label(toolbar, text="条带数").pack(side=tk.LEFT, padx=(2, 1))
        ttk.Spinbox(
            toolbar,
            from_=0,
            to=200,
            width=4,
            textvariable=self.region_band_count_var,
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Radiobutton(
            toolbar,
            text="单条带框选",
            value="rect",
            variable=self.mode_var,
        ).pack(side=tk.LEFT, padx=3)
        ttk.Button(toolbar, text="删除选区", command=self.delete_selected).pack(side=tk.LEFT, padx=(8, 2))
        ttk.Button(toolbar, text="清空", command=self.clear_rois).pack(side=tk.LEFT, padx=2)

        ttk.Button(toolbar, text="导出 CSV", command=self.export_csv).pack(side=tk.RIGHT, padx=2)
        ttk.Button(toolbar, text="导出标注图", command=self.export_annotated).pack(side=tk.RIGHT, padx=2)

        main = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        main.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 6))

        image_frame = ttk.Frame(main)
        control_frame = ttk.Frame(main, width=410)
        main.add(image_frame, weight=4)
        main.add(control_frame, weight=2)

        image_switch = ttk.Frame(image_frame, padding=(0, 0, 0, 5))
        image_switch.grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Label(image_switch, text="当前编辑图像：").pack(side=tk.LEFT)
        for text, value in (("内参图像", "reference"), ("目的图像", "target")):
            ttk.Radiobutton(
                image_switch,
                text=text,
                value=value,
                variable=self.active_image_var,
                command=lambda key=value: self.activate_image(key),
            ).pack(side=tk.LEFT, padx=4)
            ttk.Label(image_switch, text="分类名称").pack(side=tk.LEFT, padx=(2, 2))
            name_entry = ttk.Entry(
                image_switch,
                width=12,
                textvariable=self.category_name_vars[value],
            )
            name_entry.pack(side=tk.LEFT, padx=(0, 7))
            name_entry.bind(
                "<Return>",
                lambda _event, key=value: self.apply_category_name(key),
            )
            name_entry.bind(
                "<FocusOut>",
                lambda _event, key=value: self.apply_category_name(key, announce=False),
            )
        ttk.Button(
            image_switch,
            text="应用名称",
            command=self.apply_category_names,
        ).pack(side=tk.LEFT, padx=(2, 0))

        self.canvas = tk.Canvas(image_frame, background="#25282b", highlightthickness=0, cursor="crosshair")
        xscroll = ttk.Scrollbar(image_frame, orient=tk.HORIZONTAL, command=self.canvas.xview)
        yscroll = ttk.Scrollbar(image_frame, orient=tk.VERTICAL, command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=xscroll.set, yscrollcommand=yscroll.set)
        self.canvas.grid(row=1, column=0, sticky="nsew")
        yscroll.grid(row=1, column=1, sticky="ns")
        xscroll.grid(row=2, column=0, sticky="ew")
        image_frame.rowconfigure(1, weight=1)
        image_frame.columnconfigure(0, weight=1)

        settings = ttk.LabelFrame(control_frame, text="识别参数", padding=9)
        settings.pack(fill=tk.X, pady=(0, 7))
        ttk.Label(settings, text="条带极性").grid(row=0, column=0, sticky="w")
        polarity = ttk.Combobox(settings, state="readonly", width=15, textvariable=self.polarity_var)
        polarity["values"] = ("auto", "dark", "light")
        polarity.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        polarity.bind("<<ComboboxSelected>>", lambda _event: self._update_results())
        ttk.Label(settings, text="自动 / 深色 / 浅色").grid(row=1, column=1, sticky="w", padx=(8, 0))
        ttk.Label(settings, text="灵敏度").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Scale(settings, from_=1, to=100, variable=self.sensitivity_var).grid(row=2, column=1, sticky="ew", padx=(8, 0), pady=(8, 0))
        self.sensitivity_label = ttk.Label(settings, text="55")
        self.sensitivity_label.grid(row=2, column=2, padx=(5, 0), pady=(8, 0))
        self.sensitivity_var.trace_add("write", lambda *_: self.sensitivity_label.configure(text=f"{self.sensitivity_var.get():.0f}"))
        ttk.Label(settings, text="最小面积(px)").grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Spinbox(settings, from_=4, to=100000, width=10, textvariable=self.min_area_var).grid(row=3, column=1, sticky="w", padx=(8, 0), pady=(8, 0))
        ttk.Label(settings, text="识别框扩展(px)").grid(row=4, column=0, sticky="w", pady=(8, 0))
        ttk.Spinbox(settings, from_=0, to=100, width=10, textvariable=self.region_padding_var).grid(row=4, column=1, sticky="w", padx=(8, 0), pady=(8, 0))
        ttk.Label(settings, text="自动/拆分后向四周扩展").grid(row=5, column=1, sticky="w", padx=(8, 0))
        ttk.Label(settings, text="选区间隔(px)").grid(row=6, column=0, sticky="w", pady=(8, 0))
        ttk.Spinbox(settings, from_=0, to=30, width=10, textvariable=self.roi_gap_var).grid(row=6, column=1, sticky="w", padx=(8, 0), pady=(8, 0))
        ttk.Checkbutton(
            settings,
            text="排除低于框选区域平均校正信号的噪声候选",
            variable=self.filter_below_mean_var,
        ).grid(row=7, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Label(settings, text="Marker过滤").grid(row=8, column=0, sticky="w", pady=(8, 0))
        marker_mode = ttk.Combobox(settings, state="readonly", width=15, textvariable=self.marker_mode_var)
        marker_mode["values"] = ("auto", "left", "right", "off")
        marker_mode.grid(row=8, column=1, sticky="ew", padx=(8, 0), pady=(8, 0))
        ttk.Label(settings, text="自动 / 左侧 / 右侧 / 关闭").grid(row=9, column=1, sticky="w", padx=(8, 0))
        settings.columnconfigure(1, weight=1)

        actions = ttk.Frame(control_frame)
        actions.pack(fill=tk.X, pady=(0, 7))
        ttk.Button(actions, text="重命名", command=self.rename_selected).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 2))
        ttk.Button(actions, text="确认当前配对", command=self.confirm_selected_pair).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        ttk.Button(actions, text="配对管理…", command=self.open_pairing_manager).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(2, 0))

        table_frame = ttk.LabelFrame(control_frame, text="定量结果（按泳道目的蛋白/内参归一化）", padding=5)
        table_frame.pack(fill=tk.BOTH, expand=True)
        columns = (
            "role", "id", "name", "lane", "mean", "bg", "corrected", "integrated",
            "target_integrated_density", "reference_integrated_density",
            "target_over_reference", "pairing_status", "missing_reason",
        )
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        headings = {
            "role": "类型", "id": "ID", "name": "名称", "lane": "泳道", "mean": "平均灰度",
            "bg": "局部背景", "corrected": "校正均值", "integrated": "本条带积分灰度",
            "target_integrated_density": "目的积分灰度",
            "reference_integrated_density": "内参积分灰度",
            "target_over_reference": "目的/内参",
            "pairing_status": "配对状态",
            "missing_reason": "缺失原因/提示",
        }
        widths = {
            "role": 55, "id": 42, "name": 88, "lane": 48, "mean": 76, "bg": 76,
            "corrected": 76, "integrated": 100, "target_integrated_density": 110,
            "reference_integrated_density": 110, "target_over_reference": 90,
            "pairing_status": 90, "missing_reason": 240,
        }
        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=widths[col], minwidth=38, anchor="center")
        table_scroll_y = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.tree.yview)
        table_scroll_x = ttk.Scrollbar(table_frame, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=table_scroll_y.set, xscrollcommand=table_scroll_x.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        table_scroll_y.grid(row=0, column=1, sticky="ns")
        table_scroll_x.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        help_text = "提示：两张图中唯一同号 lane 自动配对。请在“配对管理”中检查、手动调整并确认；重复或缺失泳道不会被静默计算。"
        ttk.Label(control_frame, text=help_text, wraplength=400, foreground="#555").pack(fill=tk.X, pady=(6, 0))
        ttk.Label(self, textvariable=self.status_var, relief=tk.SUNKEN, anchor="w", padding=(8, 4)).pack(fill=tk.X, side=tk.BOTTOM)

        self.roi_context_menu = tk.Menu(self, tearoff=False)
        self.roi_context_menu.add_command(label="重命名此选区", command=self.rename_selected)
        self.roi_context_menu.add_command(label="删除此选区", command=self.delete_selected)

    def _bind_events(self) -> None:
        self.canvas.bind("<Configure>", lambda _e: self._redraw())
        self.canvas.bind("<ButtonPress-1>", self._canvas_press)
        self.canvas.bind("<B1-Motion>", self._canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self._canvas_release)
        self.canvas.bind("<Button-2>", self._canvas_context_menu)
        self.canvas.bind("<Button-3>", self._canvas_context_menu)
        self.canvas.bind("<Control-Button-1>", self._canvas_context_menu)
        self.canvas.bind("<MouseWheel>", self._mousewheel)
        self.tree.bind("<<TreeviewSelect>>", self._tree_select)
        self.tree.bind("<Double-1>", lambda _e: self.rename_selected())
        self.bind("<Delete>", lambda _e: self.delete_selected())
        self.bind("<BackSpace>", lambda _e: self.delete_selected())
        self.bind("<Command-o>", lambda _e: self.open_image(self.active_image_key))
        self.bind("<Command-s>", lambda _e: self.export_csv())
        self.bind("<Control-o>", lambda _e: self.open_image(self.active_image_key))
        self.bind("<Control-s>", lambda _e: self.export_csv())

    def _sync_active_state(self) -> None:
        state = self.image_states[self.active_image_key]
        state.image = self.original_image
        state.gray = self.gray
        state.path = self.image_path
        state.rois = self.rois
        state.reference_uid = self.reference_uid
        state.selected_uid = self.selected_uid

    def apply_category_name(self, key: str, announce: bool = True) -> None:
        if key not in self.image_states:
            return
        default = "内参" if key == "reference" else "目的"
        name = self.category_name_vars[key].get().strip()
        if not name:
            name = default
            self.category_name_vars[key].set(name)
        state = self.image_states[key]
        if state.label == name:
            return
        state.label = name
        self._update_results()
        if announce:
            self.status_var.set(f"{default}图像的结果分类名称已更新为：{name}")

    def apply_category_names(self) -> None:
        for key in ("reference", "target"):
            self.apply_category_name(key, announce=False)
        self.status_var.set(
            f"结果分类已同步：内参 = {self.image_states['reference'].label}；"
            f"目的 = {self.image_states['target'].label}"
        )

    def activate_image(self, key: str, fit: bool = True) -> None:
        if key not in self.image_states:
            return
        self._sync_active_state()
        self.active_image_key = key
        self.active_image_var.set(key)
        state = self.image_states[key]
        self.original_image = state.image
        self.gray = state.gray
        self.image_path = state.path
        self.rois = state.rois
        self.reference_uid = state.reference_uid
        self.selected_uid = state.selected_uid
        if fit and self.original_image is not None:
            self.after(10, self._fit_image)
        else:
            self._redraw()
        name = self.image_path.name if self.image_path else "尚未选择"
        self.status_var.set(f"当前编辑：{state.label}图像 | {name}")

    def open_image(self, key: str) -> None:
        label = self.image_states[key].label
        filename = filedialog.askopenfilename(
            title=f"选择{label}图像",
            filetypes=[("图像", "*.png *.jpg *.jpeg *.tif *.tiff *.bmp"), ("所有文件", "*.*")],
        )
        if not filename:
            return
        try:
            image = Image.open(filename)
            image.load()
            if image.mode not in ("L", "RGB", "RGBA"):
                image = image.convert("RGB")
            loaded_image = image.copy()
            loaded_gray = to_gray_array(np.asarray(image))
        except Exception as exc:
            messagebox.showerror("无法打开图像", str(exc))
            return
        self._sync_active_state()
        state = self.image_states[key]
        state.image = loaded_image
        state.gray = loaded_gray
        state.path = Path(filename)
        state.rois = []
        state.reference_uid = None
        state.selected_uid = None
        state.preview_image = None
        state.preview_size = None
        state.detection_cache_key = None
        state.detection_cache = None
        state.auto_polarity = None
        self.active_image_key = key
        self.active_image_var.set(key)
        self.original_image = state.image
        self.gray = state.gray
        self.image_path = state.path
        self.rois = state.rois
        self.reference_uid = None
        self.selected_uid = None
        self.after(50, self._fit_image)
        self._update_results()
        self.status_var.set(f"已选择{label}图像：{self.image_path.name} | {image.width} × {image.height} px")

    def _fit_image(self) -> None:
        if self.original_image is None:
            return
        cw, ch = max(1, self.canvas.winfo_width() - 30), max(1, self.canvas.winfo_height() - 30)
        self.zoom = min(cw / self.original_image.width, ch / self.original_image.height, 1.0)
        self.zoom = max(self.zoom, 0.05)
        self._redraw()

    def _get_full_detection(self) -> tuple[list[BandROI], str]:
        """Return full-image candidates cached for the active parameters."""

        if self.gray is None:
            return [], self.polarity_var.get()
        state = self.image_states[self.active_image_key]
        cache_key = (
            round(float(self.sensitivity_var.get()), 4),
            max(4, int(self.min_area_var.get())),
            self.polarity_var.get(),
        )
        if state.detection_cache_key == cache_key and state.detection_cache is not None:
            return state.detection_cache
        detected = detect_bands(
            self.gray,
            sensitivity=cache_key[0],
            min_area=cache_key[1],
            polarity=cache_key[2],
            first_uid=1,
        )
        state.detection_cache_key = cache_key
        state.detection_cache = detected
        if cache_key[2] == "auto":
            state.auto_polarity = detected[1]
        return detected

    def auto_detect(self) -> None:
        if self.gray is None:
            messagebox.showinfo(APP_TITLE, "请先打开一张 WB 图像。")
            return
        try:
            cached_rois, actual = self._get_full_detection()
            rois = [
                replace(
                    roi,
                    uid=self.next_uid + offset,
                    name=f"Band {self.next_uid + offset}",
                )
                for offset, roi in enumerate(cached_rois)
            ]
        except Exception as exc:
            messagebox.showerror("自动识别失败", str(exc))
            return
        candidate_count = len(rois)
        rois, marker_removed = filter_marker_lanes(rois, self.marker_mode_var.get())
        rois = expand_band_rois(
            rois,
            (0, 0, self.gray.shape[1], self.gray.shape[0]),
            padding=max(0, self.region_padding_var.get()),
            gap=max(0, self.roi_gap_var.get()),
        )
        self.rois = rois
        self.next_uid = max((r.uid for r in rois), default=0) + 1
        self.reference_uid = None
        self.selected_uid = rois[0].uid if rois else None
        self._update_results()
        self._redraw()
        self.status_var.set(
            f"全图识别完成：保留 {len(rois)}/{candidate_count} 个条带；"
            f"Marker过滤 {len(marker_removed)} 个；识别框扩展 "
            f"{max(0, self.region_padding_var.get())} px；实际使用 {actual} 极性。"
        )

    def _detect_region(
        self,
        region: tuple[float, float, float, float],
        replace_uid: int | None = None,
    ) -> None:
        """Detect bands in a user region and merge them into the ROI list."""

        if self.gray is None:
            return
        x1, y1, x2, y2 = (int(round(v)) for v in region)
        try:
            count_text = self.region_band_count_var.get().strip()
            requested_count = int(count_text) if count_text else 0
            if requested_count < 0:
                raise ValueError("条带数不能小于 0；输入 0 表示自动判断")
            found, actual = detect_bands_in_region(
                self.gray,
                (x1, y1, x2, y2),
                sensitivity=self.sensitivity_var.get(),
                min_area=max(4, self.min_area_var.get()),
                polarity=self.polarity_var.get(),
                first_uid=self.next_uid,
                band_count=requested_count or None,
                full_detection=self._get_full_detection(),
            )
        except Exception as exc:
            messagebox.showerror("拆分所选区域失败", str(exc))
            return
        if not found:
            self.status_var.set("该区域内未识别到条带。可提高灵敏度、降低最小面积，或扩大选区上下边距后重试。")
            return

        candidate_count = len(found)
        if requested_count:
            # An explicit count is authoritative for this selection; automatic
            # candidate filters must not silently reduce it afterwards.
            marker_removed: list[BandROI] = []
        else:
            found, marker_removed = filter_marker_lanes(found, self.marker_mode_var.get())
        found = align_rois_to_region_signal(
            self.gray,
            found,
            (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)),
            polarity=actual,
        )
        found = expand_band_rois(
            found,
            (0, 0, self.gray.shape[1], self.gray.shape[0]),
            padding=max(0, self.region_padding_var.get()),
            gap=max(0, self.roi_gap_var.get()),
        )
        signal_threshold: float | None = None
        if self.filter_below_mean_var.get() and not requested_count:
            found, signal_threshold = filter_bands_below_mean_signal(
                self.gray,
                found,
                polarity=actual,
                region=(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)),
            )
        weak_excluded_count = candidate_count - len(marker_removed) - len(found)
        if not found:
            self.status_var.set(
                f"区域内的 {candidate_count} 个候选均被过滤：Marker {len(marker_removed)} 个，"
                f"弱候选 {weak_excluded_count} 个。可调整 Marker 模式或关闭弱候选过滤后重试。"
            )
            return

        # Re-running a region should replace bands inside it instead of adding
        # duplicates. A deliberately selected broad ROI is always removed.
        rx1, rx2 = sorted((x1, x2))
        ry1, ry2 = sorted((y1, y2))
        kept: list[BandROI] = []
        removed_uids: set[int] = set()
        for old in self.rois:
            cx, cy = old.center
            inside = rx1 <= cx <= rx2 and ry1 <= cy <= ry2
            if old.uid == replace_uid or inside:
                removed_uids.add(old.uid)
            else:
                kept.append(old)
        self.rois = assign_lanes(kept + found)
        self.next_uid = max((r.uid for r in self.rois), default=0) + 1
        self.selected_uid = found[0].uid
        if self.reference_uid in removed_uids:
            self.reference_uid = None
        self._update_results()
        self._redraw()
        filter_text = ""
        if signal_threshold is not None:
            filter_text = f"；平均校正信号阈值 {signal_threshold:.2f}，排除 {weak_excluded_count} 个弱候选"
        self.status_var.set(
            f"区域拆分完成：保留 {len(found)}/{candidate_count} 个独立条带"
            f"{'；已按指定数量强制拆分' if requested_count else ''}"
            f"；Marker过滤 {len(marker_removed)} 个{filter_text}；"
            f"识别框扩展 {max(0, self.region_padding_var.get())} px，"
            f"间隔 {max(0, self.roi_gap_var.get())} px；"
            f"实际使用 {actual} 极性。"
        )

    def _display_geometry(self) -> tuple[int, int, float, float]:
        assert self.original_image is not None
        dw = max(1, round(self.original_image.width * self.zoom))
        dh = max(1, round(self.original_image.height * self.zoom))
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        ox, oy = max(8.0, (cw - dw) / 2), max(8.0, (ch - dh) / 2)
        return dw, dh, ox, oy

    def _redraw(self) -> None:
        self.canvas.delete("all")
        if self.original_image is None:
            self.canvas.create_text(
                max(1, self.canvas.winfo_width()) / 2,
                max(1, self.canvas.winfo_height()) / 2,
                text="打开 WB 图片后开始分析", fill="#c8c8c8", font=("Arial", 18),
            )
            return
        dw, dh, ox, oy = self._display_geometry()
        self.image_origin = (ox, oy)
        state = self.image_states[self.active_image_key]
        if state.preview_image is None or state.preview_size != (dw, dh):
            display = self.original_image.resize((dw, dh), Image.Resampling.LANCZOS)
            state.preview_image = ImageTk.PhotoImage(display)
            state.preview_size = (dw, dh)
        self.tk_image = state.preview_image
        self.canvas.create_image(ox, oy, image=self.tk_image, anchor="nw", tags="image")
        self.canvas.configure(scrollregion=(0, 0, max(self.canvas.winfo_width(), ox + dw + 8), max(self.canvas.winfo_height(), oy + dh + 8)))
        for roi in self.rois:
            x1, y1 = self._image_to_canvas(roi.x, roi.y)
            x2, y2 = self._image_to_canvas(roi.x2, roi.y2)
            selected = roi.uid == self.selected_uid
            color = "#ff3b30" if selected else "#00d7ff"
            width = 3 if selected else 2
            self.canvas.create_rectangle(x1, y1, x2, y2, outline=color, width=width, tags=("roi", f"roi_{roi.uid}"))
            label = f"{roi.uid}"
            text_id = self.canvas.create_text(x1 + 2, y1 - 2, text=label, fill="white", anchor="sw", font=("Arial", 11, "bold"), tags=("roi", f"roi_{roi.uid}"))
            bbox = self.canvas.bbox(text_id)
            if bbox:
                bg = self.canvas.create_rectangle(*bbox, fill=color, outline=color, tags=("roi", f"roi_{roi.uid}"))
                self.canvas.tag_lower(bg, text_id)

    def _image_to_canvas(self, x: float, y: float) -> tuple[float, float]:
        ox, oy = self.image_origin
        return ox + x * self.zoom, oy + y * self.zoom

    def _canvas_to_image(self, x: float, y: float) -> tuple[float, float]:
        ox, oy = self.image_origin
        return (self.canvas.canvasx(x) - ox) / self.zoom, (self.canvas.canvasy(y) - oy) / self.zoom

    def _point_in_image(self, point: tuple[float, float]) -> bool:
        if self.original_image is None:
            return False
        return 0 <= point[0] < self.original_image.width and 0 <= point[1] < self.original_image.height

    def _roi_at_point(self, point: tuple[float, float]) -> BandROI | None:
        """Return the smallest ROI containing an image-coordinate point."""

        hits = [
            roi for roi in self.rois
            if roi.x <= point[0] <= roi.x2 and roi.y <= point[1] <= roi.y2
        ]
        return min(hits, key=lambda roi: roi.width * roi.height, default=None)

    def _canvas_context_menu(self, event: tk.Event) -> str | None:
        if self.original_image is None:
            return None
        point = self._canvas_to_image(event.x, event.y)
        roi = self._roi_at_point(point)
        if roi is None:
            self.status_var.set("右键点击已有识别框，可重命名或删除选区。")
            return None
        self._select_uid(roi.uid)
        self.status_var.set(f"已选择 ID {roi.uid}：{roi.name}")
        try:
            self.roi_context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.roi_context_menu.grab_release()
        return "break"

    def _canvas_press(self, event: tk.Event) -> None:
        if self.original_image is None:
            return
        p = self._canvas_to_image(event.x, event.y)
        if not self._point_in_image(p):
            return
        hit = self._roi_at_point(p)
        if hit is not None:
            self._select_uid(hit.uid)
            self.status_var.set(f"已选择 ID {hit.uid}：{hit.name}")
        self.drag_start = p
        cx, cy = self._image_to_canvas(*p)
        self.preview_item = self.canvas.create_rectangle(cx, cy, cx, cy, outline="#ffd60a", width=2, dash=(5, 3))

    def _canvas_drag(self, event: tk.Event) -> None:
        if self.drag_start is None or self.preview_item is None:
            return
        p = self._canvas_to_image(event.x, event.y)
        p = self._clamp_point(p)
        x1, y1 = self._image_to_canvas(*self.drag_start)
        x2, y2 = self._image_to_canvas(*p)
        self.canvas.coords(self.preview_item, x1, y1, x2, y2)

    def _canvas_release(self, event: tk.Event) -> None:
        if self.drag_start is None or self.original_image is None:
            return
        end = self._clamp_point(self._canvas_to_image(event.x, event.y))
        start = self.drag_start
        self.drag_start = None
        if self.preview_item is not None:
            self.canvas.delete(self.preview_item)
            self.preview_item = None
        if self.mode_var.get() == "region":
            x1, x2 = sorted((start[0], end[0]))
            y1, y2 = sorted((start[1], end[1]))
            x1, y1 = self._clamp_point((x1, y1))
            x2, y2 = self._clamp_point((x2, y2), upper=True)
            if x2 - x1 < 3 or y2 - y1 < 3:
                self.status_var.set("识别区域太小，请重新框选。")
                self._redraw()
                return
            self._detect_region((x1, y1, x2, y2))
            return
        x1, x2 = sorted((start[0], end[0]))
        y1, y2 = sorted((start[1], end[1]))
        x1, y1 = self._clamp_point((x1, y1))
        x2, y2 = self._clamp_point((x2, y2), upper=True)
        if x2 - x1 < 2 or y2 - y1 < 2:
            hits = [r for r in self.rois if r.x <= start[0] <= r.x2 and r.y <= start[1] <= r.y2]
            if hits:
                self._select_uid(min(hits, key=lambda r: r.width * r.height).uid)
                self.status_var.set("已选择条带；拖动鼠标可创建新选区。")
            else:
                self.status_var.set("选区太小，未添加。")
            self._redraw()
            return
        roi = BandROI(
            uid=self.next_uid, x=int(round(x1)), y=int(round(y1)),
            width=max(1, int(round(x2 - x1))), height=max(1, int(round(y2 - y1))),
            name=f"Band {self.next_uid}", source="手工",
        )
        self.rois.append(roi)
        self.rois = assign_lanes(self.rois)
        self.selected_uid = self.next_uid
        self.next_uid += 1
        self._update_results()
        self._redraw()
        self.status_var.set(f"已添加手工选区 ID {self.selected_uid}")

    def _clamp_point(self, point: tuple[float, float], upper: bool = False) -> tuple[float, float]:
        assert self.original_image is not None
        max_x = self.original_image.width if upper else self.original_image.width - 1
        max_y = self.original_image.height if upper else self.original_image.height - 1
        return max(0.0, min(point[0], max_x)), max(0.0, min(point[1], max_y))

    def _mousewheel(self, event: tk.Event) -> None:
        if self.original_image is None:
            return
        factor = 1.12 if event.delta > 0 else 1 / 1.12
        self.zoom = float(np.clip(self.zoom * factor, 0.05, 8.0))
        self._redraw()
        self.status_var.set(f"缩放：{self.zoom * 100:.0f}%")

    def _update_results(self) -> None:
        self._sync_active_state()
        self.combined_measurements = []
        measurements_by_role: dict[str, list[BandMeasurement]] = {"target": [], "reference": []}
        for key, state in self.image_states.items():
            if state.gray is None:
                continue
            requested_polarity = self.polarity_var.get()
            if requested_polarity == "auto":
                if state.auto_polarity is None:
                    state.auto_polarity = infer_polarity(state.gray)
                measurement_polarity = state.auto_polarity
            else:
                measurement_polarity = requested_polarity
            measurements = measure_all(
                state.gray,
                state.rois,
                measurement_polarity,
                None,
            )
            measurements_by_role[key] = measurements
            self.combined_measurements.extend((key, measurement) for measurement in measurements)
        self.normalized_results = normalize_target_to_reference_by_lane(
            measurements_by_role["target"],
            measurements_by_role["reference"],
            manual_pairs=self.pairing_overrides,
            confirmed_pairs=self.confirmed_pairings,
        )
        self.pairing_result_by_uid = {}
        for result in self.normalized_results:
            if result.target_uid is not None:
                self.pairing_result_by_uid[result.target_uid] = result
            if result.reference_uid is not None:
                self.pairing_result_by_uid[result.reference_uid] = result
        # Ambiguous lane results intentionally have no selected reference UID.
        # Still attach their diagnostic to the raw reference rows in the main
        # table so every affected band visibly explains why no ratio exists.
        for key, measurement in self.combined_measurements:
            if measurement.uid in self.pairing_result_by_uid:
                continue
            if key == "reference":
                diagnostic = next(
                    (
                        result
                        for result in self.normalized_results
                        if (
                            result.lane == measurement.lane
                            and result.pairing_status in {"ambiguous_target", "ambiguous_reference"}
                        )
                    ),
                    None,
                )
                if diagnostic is not None:
                    self.pairing_result_by_uid[measurement.uid] = diagnostic
        self.measurements = self.combined_measurements
        selected = self.selected_uid
        self.tree.delete(*self.tree.get_children())
        for key, m in self.combined_measurements:
            state = self.image_states[key]
            result = self.pairing_result_by_uid.get(m.uid)
            target_density = (
                f"{m.integrated_density:.2f}" if key == "target" and (
                    result is None or result.target_integrated_density is None
                )
                else (
                    "" if result is None or result.target_integrated_density is None
                    else f"{result.target_integrated_density:.2f}"
                )
            )
            reference_density = (
                f"{m.integrated_density:.2f}" if key == "reference" and (
                    result is None or result.reference_integrated_density is None
                )
                else (
                    "" if result is None or result.reference_integrated_density is None
                    else f"{result.reference_integrated_density:.2f}"
                )
            )
            ratio = (
                "" if result is None or result.target_over_reference is None
                else f"{result.target_over_reference:.6f}"
            )
            pairing_status = (
                "" if result is None
                else PAIRING_STATUS_LABELS.get(result.pairing_status, result.pairing_status)
            )
            missing_reason = "" if result is None else result.missing_reason
            self.tree.insert("", tk.END, iid=str(m.uid), values=(
                state.label, m.uid, m.name, m.lane, f"{m.mean_gray:.2f}", f"{m.background:.2f}",
                f"{m.corrected_mean:.2f}", f"{m.integrated_density:.2f}",
                target_density, reference_density, ratio, pairing_status, missing_reason,
            ))
        if selected is not None and self.tree.exists(str(selected)):
            self.tree.selection_set(str(selected))
            self.tree.see(str(selected))
        if (
            self.pairing_manager is not None
            and self.pairing_manager.winfo_exists()
            and not self.pairing_manager._refreshing
        ):
            self.pairing_manager.refresh()

    def _tree_select(self, _event: tk.Event | None = None) -> None:
        selection = self.tree.selection()
        if selection:
            uid = int(selection[0])
            selected_key = next(
                (key for key, state in self.image_states.items() if any(roi.uid == uid for roi in state.rois)),
                None,
            )
            if selected_key is not None and selected_key != self.active_image_key:
                self.image_states[selected_key].selected_uid = uid
                self.activate_image(selected_key)
                return
            self.selected_uid = uid
            self._sync_active_state()
            self._redraw()

    def _select_uid(self, uid: int) -> None:
        self.selected_uid = uid
        if self.tree.exists(str(uid)):
            self.tree.selection_set(str(uid))
            self.tree.see(str(uid))
        self._redraw()

    def delete_selected(self) -> None:
        if self.selected_uid is None:
            return
        uid = self.selected_uid
        self.rois = [r for r in self.rois if r.uid != uid]
        if self.reference_uid == uid:
            self.reference_uid = self.rois[0].uid if self.rois else None
        self.selected_uid = self.rois[0].uid if self.rois else None
        self.rois = assign_lanes(self.rois)
        self._update_results()
        self._redraw()
        self.status_var.set(f"已删除选区 ID {uid}")

    def clear_rois(self) -> None:
        if not self.rois:
            return
        label = self.image_states[self.active_image_key].label
        if not messagebox.askyesno(APP_TITLE, f"确定清空{label}图像的全部选区吗？"):
            return
        self.rois.clear()
        self.measurements.clear()
        self.reference_uid = None
        self.selected_uid = None
        self._update_results()
        self._redraw()
        self.status_var.set(f"已清空{label}图像的全部选区；另一张图的结果已保留。")

    def rename_selected(self) -> None:
        roi = next((r for r in self.rois if r.uid == self.selected_uid), None)
        if roi is None:
            return
        name = simpledialog.askstring("重命名条带", "请输入名称：", initialvalue=roi.name, parent=self)
        if name is None or not name.strip():
            return
        roi.name = name.strip()
        self._update_results()
        self._redraw()

    def open_pairing_manager(self) -> None:
        if not self.image_states["target"].rois and not self.image_states["reference"].rois:
            messagebox.showinfo(APP_TITLE, "请先在目的图和内参图中创建条带选区。")
            return
        if self.pairing_manager is not None and self.pairing_manager.winfo_exists():
            self.pairing_manager.lift()
            self.pairing_manager.focus_force()
            return
        self.pairing_manager = PairingManager(self)

    def set_manual_pair(self, target_uid: int, reference_uid: int | None) -> None:
        self.pairing_overrides[target_uid] = reference_uid
        self.confirmed_pairings = {
            pair for pair in self.confirmed_pairings if pair[0] != target_uid
        }
        self._update_results()
        if reference_uid is None:
            self.status_var.set(f"目的条带 ID {target_uid} 已手动设为未配对。")
        else:
            self.status_var.set(f"目的条带 ID {target_uid} 已手动配对到内参条带 ID {reference_uid}。")

    def confirm_target_pair(self, target_uid: int, parent: tk.Misc | None = None) -> bool:
        self._update_results()
        result = next(
            (item for item in self.normalized_results if item.target_uid == target_uid),
            None,
        )
        if result is None or result.reference_uid is None:
            messagebox.showinfo(
                APP_TITLE,
                "当前目的条带没有可确认的内参配对；请先在配对管理中选择内参。",
                parent=parent or self,
            )
            return False
        if result.pairing_status in {
            "ambiguous_target",
            "ambiguous_reference",
            "duplicate_reference",
            "invalid_manual_reference",
            "manual_unpaired",
            "missing_reference",
        }:
            messagebox.showinfo(
                APP_TITLE,
                "当前配对仍存在冲突或缺失，不能确认。",
                parent=parent or self,
            )
            return False
        self.confirmed_pairings = {
            pair for pair in self.confirmed_pairings if pair[0] != target_uid
        }
        self.confirmed_pairings.add((target_uid, result.reference_uid))
        self._update_results()
        self.status_var.set(
            f"已确认目的条带 ID {target_uid} ↔ 内参条带 ID {result.reference_uid}。"
        )
        return True

    def confirm_selected_pair(self) -> None:
        if self.selected_uid is None:
            messagebox.showinfo(APP_TITLE, "请先在结果表中选择一个已配对条带。")
            return
        result = self.pairing_result_by_uid.get(self.selected_uid)
        if result is None or result.target_uid is None:
            messagebox.showinfo(APP_TITLE, "所选条带没有可确认的目的蛋白/内参配对。")
            return
        self.confirm_target_pair(result.target_uid)

    def reset_pairing(self, target_uid: int) -> None:
        self.pairing_overrides.pop(target_uid, None)
        self.confirmed_pairings = {
            pair for pair in self.confirmed_pairings if pair[0] != target_uid
        }
        self._update_results()
        self.status_var.set(f"目的条带 ID {target_uid} 已恢复按 lane 自动配对。")

    def reset_all_pairings(self) -> None:
        self.pairing_overrides.clear()
        self.confirmed_pairings.clear()
        self._update_results()
        self.status_var.set("已清除全部手动调整和确认状态，并重新按 lane 自动配对。")

    def export_csv(self) -> None:
        self._update_results()
        if not self.normalized_results:
            messagebox.showinfo(APP_TITLE, "当前没有可导出的定量结果。")
            return
        initial = "WB_按泳道目的蛋白内参归一化.csv"
        filename = filedialog.asksaveasfilename(
            title="导出定量结果", defaultextension=".csv", initialfile=initial,
            filetypes=[("CSV", "*.csv")],
        )
        if not filename:
            return
        measurement_lookup = {
            (key, measurement.uid): measurement
            for key, measurement in self.combined_measurements
        }
        target_state = self.image_states["target"]
        reference_state = self.image_states["reference"]
        headers = [
            "lane", "target_lane", "reference_lane",
            "target_category", "reference_category",
            "target_image", "reference_image", "polarity",
            "target_uid", "reference_uid", "target_name", "reference_name",
            "target_x", "target_y", "target_width", "target_height",
            "reference_x", "reference_y", "reference_width", "reference_height",
            "target_mean_gray", "target_local_background", "target_background_corrected_mean",
            "reference_mean_gray", "reference_local_background", "reference_background_corrected_mean",
            "target_integrated_density", "reference_integrated_density", "target_over_reference",
            "pairing_status", "missing_reason", "target_source", "reference_source",
        ]

        def number(value: float | int | None) -> str:
            if value is None:
                return ""
            return f"{value:.6f}" if isinstance(value, float) else str(value)

        with open(filename, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow(headers)
            for result in self.normalized_results:
                target = measurement_lookup.get(("target", result.target_uid))
                reference = measurement_lookup.get(("reference", result.reference_uid))
                writer.writerow([
                    result.lane,
                    number(result.target_lane),
                    number(result.reference_lane),
                    target_state.label,
                    reference_state.label,
                    target_state.path.name if target_state.path else "",
                    reference_state.path.name if reference_state.path else "",
                    self.polarity_var.get(),
                    number(result.target_uid),
                    number(result.reference_uid),
                    target.name if target is not None else "",
                    reference.name if reference is not None else "",
                    number(target.x if target is not None else None),
                    number(target.y if target is not None else None),
                    number(target.width if target is not None else None),
                    number(target.height if target is not None else None),
                    number(reference.x if reference is not None else None),
                    number(reference.y if reference is not None else None),
                    number(reference.width if reference is not None else None),
                    number(reference.height if reference is not None else None),
                    number(target.mean_gray if target is not None else None),
                    number(target.background if target is not None else None),
                    number(target.corrected_mean if target is not None else None),
                    number(reference.mean_gray if reference is not None else None),
                    number(reference.background if reference is not None else None),
                    number(reference.corrected_mean if reference is not None else None),
                    number(result.target_integrated_density),
                    number(result.reference_integrated_density),
                    number(result.target_over_reference),
                    result.pairing_status,
                    result.missing_reason,
                    target.source if target is not None else "",
                    reference.source if reference is not None else "",
                ])
        self.status_var.set(f"CSV 已导出：{filename}")

    def export_annotated(self) -> None:
        if self.original_image is None or not self.rois:
            messagebox.showinfo(APP_TITLE, "请先打开图片并创建选区。")
            return
        role = self.image_states[self.active_image_key].label
        safe_role = role.translate(str.maketrans({"/": "_", "\\": "_", ":": "_"}))
        initial = f"{self.image_path.stem if self.image_path else 'WB'}_{safe_role}_annotated.png"
        filename = filedialog.asksaveasfilename(
            title="导出标注图", defaultextension=".png", initialfile=initial,
            filetypes=[("PNG", "*.png"), ("TIFF", "*.tif")],
        )
        if not filename:
            return
        annotated = self.original_image.convert("RGB")
        draw = ImageDraw.Draw(annotated)
        line_width = max(2, round(min(annotated.size) / 350))
        for roi in self.rois:
            color = (0, 180, 230)
            draw.rectangle((roi.x, roi.y, roi.x2, roi.y2), outline=color, width=line_width)
            draw.text((roi.x + 2, max(0, roi.y - 13)), f"{roi.uid} {roi.name}", fill=color, stroke_width=1, stroke_fill=(255, 255, 255))
        annotated.save(filename)
        self.status_var.set(f"标注图已导出：{filename}")


def main() -> None:
    app = WBAnalyzerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
