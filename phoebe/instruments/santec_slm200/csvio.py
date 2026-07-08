"""Santec SLM CSV format I/O (migrated from TPA_experiment slm_module/generator).

Format: cell A1 is a ``y/x`` label; row 1 holds x indices 0..W-1; column A
holds y indices 0..H-1; the data area is integer grayscale 0..1023.  Written
as plain ASCII without BOM — the DLL's CSV reader may not skip a UTF-8 BOM.
"""
from __future__ import annotations

import csv
from pathlib import Path

import numpy as np

MIN_LEVEL = 0
MAX_LEVEL = 1023


def write_santec_csv(data: np.ndarray, csv_path: str | Path) -> Path:
    data_uint16 = _validate_mask_array(data)
    path = Path(csv_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    height, width = data_uint16.shape
    with open(path, "w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["y/x", *range(width)])
        for y in range(height):
            writer.writerow([y, *data_uint16[y].tolist()])
    return path


def read_santec_csv(csv_path: str | Path) -> np.ndarray:
    """Inverse of write_santec_csv: recover the (H, W) uint16 grid."""
    path = Path(csv_path)
    with open(path, "r", encoding="utf-8-sig", newline="") as file:
        rows = list(csv.reader(file))
    rows = [row[:-1] if row and row[-1] == "" else row for row in rows]
    if len(rows) < 2:
        raise ValueError(f"CSV has no data rows: {path}")
    try:
        data = [[int(cell) for cell in row[1:]] for row in rows[1:]]
    except ValueError as exc:
        raise ValueError(f"CSV contains a non-integer grayscale: {path}") from exc
    array = np.asarray(data, dtype=np.int64)
    if array.ndim != 2 or array.size == 0:
        raise ValueError(f"CSV did not parse to a 2D grid: {path}")
    if np.any(array < MIN_LEVEL) or np.any(array > MAX_LEVEL):
        raise ValueError(f"CSV grayscale out of range 0..{MAX_LEVEL}: {path}")
    return array.astype(np.uint16, copy=False)


def _validate_mask_array(data: np.ndarray) -> np.ndarray:
    array = np.asarray(data)
    if array.ndim != 2 or array.size == 0:
        raise ValueError("SLM mask data must be a non-empty 2D array")
    if not np.all(np.isfinite(array)):
        raise ValueError("SLM mask data must be finite")
    if np.any(array < MIN_LEVEL) or np.any(array > MAX_LEVEL):
        raise ValueError("SLM mask data must be in 0..1023")
    rounded = np.rint(array)
    if not np.array_equal(array, rounded):
        raise ValueError("SLM mask data must contain integer levels")
    return rounded.astype(np.uint16, copy=False)
