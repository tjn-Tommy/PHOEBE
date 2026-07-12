"""Data plane: RunWriter and storage layout (refactor.md §10).

Bulk data is written by the experiment loop through ``await writer.append_*``
— a bounded queue whose backpressure naturally slows the loop down instead of
dropping data or ballooning memory.  Each run owns exactly one RunWriter,
whose single writer task is the ONLY writer of that run's HDF5 file.  The bus
only ever sees the returned ``DataPointer`` (plus a down-sampled preview).

Run directory layout::

    runs/<stamp>_<plugin>_<suffix>/
      run.json            RunManifest: config + hash, git commit, identities, options
      baseline_pre.json   pre-run snapshot of every leased device
      baseline_post.json  post-run snapshot
      experiment.jsonl    structured loguru log
      metrics.jsonl       scalar time series (append-safe, crash-safe)
      metrics.parquet     compacted from jsonl at finalize (if pyarrow present)
      artifacts.h5        matrix data: traces, masks, frames
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any, cast
from collections.abc import Callable

import h5py
import numpy as np
from loguru import logger
from pydantic import Field

from .contracts import (
    AwareDatetime,
    ContractModel,
    RunId,
    TaskId,
    timestamps,
    utc_now,
)
# DataPointer / RunManifest were promoted to phoebe.contracts.run (plan §7);
# re-imported here so pre-promotion import paths keep working.
from ..contracts.run import DataPointer, RunManifest
from .errors import WriterFailedError

__all__ = [
    "DataPointer",
    "MaskRecipe",
    "MetricRow",
    "RunManifest",
    "RunWriter",
    "git_state",
    "new_run_dir",
    "write_json",
]


class MetricRow(ContractModel):
    t_wall: AwareDatetime
    t_mono_ns: int
    step: int | None = None
    values: dict[str, float] = Field(default_factory=dict)


class MaskRecipe(ContractModel):
    """Parameterized-mask recipe: store seed + generator version, rebuild on demand."""

    generator: str
    version: str
    seed: int
    params: dict[str, float | int | str] = Field(default_factory=dict)


def git_state(cwd: Path | None = None) -> tuple[str, bool]:
    """(commit, dirty) of the working tree; empty when not in a repo."""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True,
            text=True, timeout=5,
        ).stdout.strip()
        dirty = bool(subprocess.run(
            ["git", "status", "--porcelain"], cwd=cwd, capture_output=True,
            text=True, timeout=5,
        ).stdout.strip())
        return commit, dirty
    except Exception:
        return "", False


class _Append:
    __slots__ = ("dataset", "array", "attrs_json", "future")

    def __init__(self, dataset: str, array: np.ndarray,
                 attrs_json: str | None, future: asyncio.Future) -> None:
        self.dataset = dataset
        self.array = array
        self.attrs_json = attrs_json
        self.future = future


class _Metric:
    __slots__ = ("line",)

    def __init__(self, line: str) -> None:
        self.line = line


_CLOSE = object()


class RunWriter:
    """Single writer of one run's artifacts.h5 + metrics.jsonl.

    ``append_array`` awaits a bounded queue (backpressure); the writer task
    performs the actual blocking I/O via ``asyncio.to_thread`` so the event
    loop never stalls on disk.

    Failure model: the writer task never dies silently.  If it fails (file
    open error, disk full on a metric write, ...) it records the failure,
    notifies ``on_failure`` so the run can fail fast, and keeps draining the
    queue — resolving every producer future exceptionally — until closed, so
    no producer is ever parked on an unresolved future.
    """

    def __init__(self, run_id: RunId, run_dir: Path, *, queue_size: int = 64,
                 compact_parquet: bool = True,
                 on_failure: Callable[[BaseException], None] | None = None,
                 close_timeout_s: float = 30.0) -> None:
        self.run_id = run_id
        self.run_dir = Path(run_dir)
        self._queue: asyncio.Queue[Any] = asyncio.Queue(queue_size)
        self._task: asyncio.Task | None = None
        self._h5: h5py.File | None = None
        self._metrics_fh = None
        self._indices: dict[str, int] = {}
        self._compact_parquet = compact_parquet
        self._closed = False
        self._on_failure = on_failure
        self._failure: BaseException | None = None
        self._close_timeout_s = close_timeout_s

    # ------------------------------------------------------------------ API
    @property
    def failure(self) -> BaseException | None:
        """The exception that killed the writer task, if any."""
        return self._failure

    def start(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._task = asyncio.create_task(self._run(), name=f"runwriter-{self.run_id}")

    async def append_array(self, dataset: str, arr: np.ndarray,
                           attrs: ContractModel | None = None) -> DataPointer:
        """Append to an extendable dataset (axis 0 growable, chunked, lzf).

        Returns a DataPointer for the pointer event.  Awaits when the queue is
        full → backpressure into the experiment loop.
        """
        if self._closed:
            raise RuntimeError("RunWriter is closed")
        self._raise_if_failed()
        fut: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        attrs_json = attrs.model_dump_json() if attrs is not None else None
        await self._queue.put(_Append(dataset, np.asarray(arr), attrs_json, fut))
        index = await fut
        return DataPointer(run_id=self.run_id, dataset=f"artifacts.h5:/{dataset}",
                           index=index)

    async def append_metric(self, row: MetricRow) -> None:
        if self._closed:
            raise RuntimeError("RunWriter is closed")
        self._raise_if_failed()
        await self._queue.put(_Metric(row.model_dump_json()))

    async def append_metrics(self, *, step: int | None = None,
                             **values: float) -> None:
        await self.append_metric(MetricRow(step=step,
                                           values={k: float(v) for k, v in values.items()},
                                           **timestamps()))

    async def aclose(self) -> None:
        """Flush everything, close files, compact metrics → parquet.

        Bounded: a wedged writer task is cancelled after ``close_timeout_s``
        instead of hanging the run's cleanup forever.
        """
        if self._closed:
            return
        self._closed = True
        if self._task is not None:
            await self._queue.put(_CLOSE)
            try:
                await asyncio.wait_for(self._task, timeout=self._close_timeout_s)
            except TimeoutError:
                logger.error("RunWriter {} close timed out after {}s; cancelling",
                             self.run_id, self._close_timeout_s)
                self._task.cancel()
            self._task = None
        if self._compact_parquet and self._failure is None:
            await asyncio.to_thread(self._compact_metrics)

    def _raise_if_failed(self) -> None:
        if self._failure is not None:
            raise WriterFailedError(
                f"RunWriter for {self.run_id} failed: {self._failure}"
            ) from self._failure

    def _note_failure(self, exc: BaseException) -> None:
        if self._failure is not None:
            return
        self._failure = exc
        logger.opt(exception=exc).error(
            "RunWriter {} failed; failing producers fast", self.run_id)
        if self._on_failure is not None:
            try:
                self._on_failure(exc)
            except Exception:
                logger.exception("RunWriter on_failure callback failed")

    # ---------------------------------------------------------- writer task
    async def _run(self) -> None:
        try:
            await asyncio.to_thread(self._open_files)
            while True:
                item = await self._queue.get()
                if item is _CLOSE:
                    return
                if isinstance(item, _Append):
                    try:
                        index = await asyncio.to_thread(self._write_array, item)
                    except Exception as exc:      # relay to the awaiting producer
                        item.future.set_exception(exc)
                    else:
                        item.future.set_result(index)
                elif isinstance(item, _Metric):
                    # unawaited by producers: a failure here fails the writer
                    await asyncio.to_thread(self._write_metric, item.line)
        except Exception as exc:
            self._note_failure(exc)
            # Keep serving the queue so producers never park on a dead writer:
            # every pending/incoming append future is resolved exceptionally.
            while True:
                item = await self._queue.get()
                if item is _CLOSE:
                    return
                if isinstance(item, _Append) and not item.future.done():
                    item.future.set_exception(WriterFailedError(
                        f"RunWriter for {self.run_id} failed: {exc}"))
        finally:
            try:
                await asyncio.to_thread(self._close_files)
            except Exception:
                logger.exception("RunWriter {} file close failed", self.run_id)

    # ------------------------------------------------- blocking (to_thread)
    def _open_files(self) -> None:
        self._h5 = h5py.File(self.run_dir / "artifacts.h5", "w", libver="latest")
        self._metrics_fh = open(self.run_dir / "metrics.jsonl", "a",
                                encoding="utf-8", buffering=1)

    def _write_array(self, item: _Append) -> int:
        assert self._h5 is not None
        arr = item.array
        if item.dataset in self._h5:
            ds = cast(h5py.Dataset, self._h5[item.dataset])
            index = ds.shape[0]
            ds.resize(index + 1, axis=0)
            ds[index] = arr
        else:
            ds = self._h5.create_dataset(
                item.dataset,
                shape=(1, *arr.shape),
                maxshape=(None, *arr.shape),
                chunks=(1, *arr.shape),
                dtype=arr.dtype,
                compression="lzf",
            )
            ds[0] = arr
            index = 0
        if item.attrs_json is not None:
            attrs_name = f"{item.dataset}__attrs"
            str_dtype = h5py.string_dtype("utf-8")
            if attrs_name in self._h5:
                ads = cast(h5py.Dataset, self._h5[attrs_name])
                ads.resize(index + 1, axis=0)
                ads[index] = item.attrs_json
            else:
                ads = self._h5.create_dataset(
                    attrs_name, shape=(1,), maxshape=(None,), dtype=str_dtype
                )
                ads[0] = item.attrs_json
        self._h5.flush()
        return index

    def _write_metric(self, line: str) -> None:
        assert self._metrics_fh is not None
        self._metrics_fh.write(line + "\n")

    def _close_files(self) -> None:
        if self._metrics_fh is not None:
            self._metrics_fh.close()
            self._metrics_fh = None
        if self._h5 is not None:
            self._h5.close()
            self._h5 = None

    def _compact_metrics(self) -> None:
        """metrics.jsonl → metrics.parquet (best effort; jsonl stays as source)."""
        src = self.run_dir / "metrics.jsonl"
        if not src.exists() or src.stat().st_size == 0:
            return
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError:
            return
        rows = []
        with open(src, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                flat = {"t_wall": obj["t_wall"], "t_mono_ns": obj["t_mono_ns"],
                        "step": obj.get("step")}
                flat.update(obj.get("values", {}))
                rows.append(flat)
        if not rows:
            return
        try:
            table = pa.Table.from_pylist(rows)
            pq.write_table(table, self.run_dir / "metrics.parquet")
        except Exception:
            logger.exception("metrics parquet compaction failed (jsonl retained)")


def write_json(path: Path, payload: dict | ContractModel) -> None:
    if isinstance(payload, ContractModel):
        text = payload.model_dump_json(indent=2)
    else:
        text = json.dumps(payload, indent=2, default=str)
    path.write_text(text, encoding="utf-8")


def new_run_dir(root: Path, plugin_id: str, task_id: TaskId) -> tuple[RunId, Path]:
    stamp = utc_now().strftime("%Y-%m-%dT%H-%M-%S")
    suffix = str(task_id)[-4:]
    run_id = RunId(f"{stamp}_{plugin_id}_{suffix}")
    return run_id, root / run_id
