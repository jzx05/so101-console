"""Camera handling: enumeration, background frame grabbing, MJPEG output.

Does not use lerobot's camera classes (they drag in more than cv2). Measured on
this machine, 2026-10-15:

* Index 0 is a close overhead view of the workspace; index 1 is a fixed view that
  was physically obstructed at the time.
* Both are DSHOW, 640x480 by default.
* **``CAP_PROP_FPS`` reads 0 before the stream starts** — normal DSHOW behaviour,
  not a fault; after ``set(CAP_PROP_FPS, 30)`` it reads back 30.00003.
* Index 1 refuses MJPG (the driver's ``set()`` returns True but the format stays
  YUY2), so it runs uncompressed at ~18 MB/s.
* Enumeration is slow upstream (60 indices). The default here is 0-7, with a deep
  scan behind an explicit button.
"""

from __future__ import annotations

import glob
import logging
import platform
import threading
import time
from dataclasses import dataclass, field

import cv2

logger = logging.getLogger(__name__)

IS_WINDOWS = platform.system() == "Windows"

#: Capture backend. On Windows DSHOW is chosen over the MSMF default because MSMF
#: stalls for a second or more on indices that do not exist, which makes
#: enumeration crawl. On Linux the backend is left to OpenCV (V4L2), and cameras
#: are addressed by device path anyway.
CAP_BACKEND = cv2.CAP_DSHOW if IS_WINDOWS else cv2.CAP_ANY

#: Indices to probe on Windows/macOS. Upstream's ``MAX_OPENCV_INDEX`` is 60, which
#: takes over a minute here — acceptable for a manual scan, and the only way to
#: find a camera that enumerated at a high index.
MAX_CAMERA_INDEX = 60

#: Ceiling on the reader loop rate, so it does not spin the CPU.
FRAME_INTERVAL_S = 0.02


def coerce_index(index: int | str) -> int | str:
    """Normalise a camera identifier to what ``cv2.VideoCapture`` expects.

    OpenCV reads an ``int`` as a device index and a ``str`` as a filename or URL.
    The UI takes free text so that Linux paths work, which means Windows indices
    arrive as ``"0"`` — and OpenCV would then look for a *file* named "0",
    failing with "backend is generally available but can't be used to capture by
    name". Anything that is all digits becomes an int.

    Args:
        index: A device index, a ``/dev/videoN`` path, or a video file path.

    Returns:
        An ``int`` when the input is numeric, otherwise the trimmed string.
    """
    if isinstance(index, int):
        return index
    text = str(index).strip()
    if text.isdigit():
        return int(text)
    return text


@dataclass
class CameraSlot:
    """Runtime state for one camera."""

    name: str
    #: int on Windows/macOS, ``/dev/videoN`` on Linux.
    index: int | str
    width: int = 640
    height: int = 480
    fps: int = 30
    fourcc: str | None = None

    _cap: cv2.VideoCapture | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _latest: object | None = field(default=None, repr=False)
    _jpeg_cache: bytes | None = field(default=None, repr=False)
    _jpeg_stamp: float = -1.0
    _stamp: float = 0.0
    _error: str | None = None
    _frames: int = 0
    _fps_t0: float = 0.0

    def __post_init__(self) -> None:
        self.index = coerce_index(self.index)

    @property
    def connected(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def frame_stamp(self) -> float:
        """When the latest frame arrived. Unchanged while the stream is stalled,
        so consumers can skip re-sending identical frames."""
        return self._stamp

    def open(self) -> None:
        """Open the device and start the reader thread."""
        cap = cv2.VideoCapture(self.index, CAP_BACKEND)
        if not cap.isOpened():
            cap.release()
            raise RuntimeError(
                f"cannot open camera {self.index!r}. It may be in use by another "
                "program, or the index may be wrong — run 'Scan cameras' to see "
                "what is available."
            )
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(self.width))
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(self.height))
        if self.fps:
            cap.set(cv2.CAP_PROP_FPS, float(self.fps))
        if self.fourcc:
            # Failure is only a warning: index 1 on this machine reports success
            # from set() while the format stays YUY2.
            ok = cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*self.fourcc))
            actual = _fourcc_str(cap.get(cv2.CAP_PROP_FOURCC))
            if not ok or actual != self.fourcc:
                logger.warning("camera %s did not accept %s (got %s), using the default format",
                               self.name, self.fourcc, actual)

        self._cap = cap
        self._stop.clear()
        self._error = None
        self._frames = 0
        self._fps_t0 = time.perf_counter()
        self._thread = threading.Thread(target=self._run, name=f"cam-{self.name}", daemon=True)
        self._thread.start()

    def close(self) -> None:
        """Stop the reader and release the device."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)
        cap, self._cap = self._cap, None
        if cap is not None:
            cap.release()
        with self._lock:
            self._latest = None

    def _run(self) -> None:
        while not self._stop.is_set():
            cap = self._cap
            if cap is None:
                break
            ok, frame = cap.read()
            if not ok or frame is None:
                self._error = "frame read failed (device busy or unplugged)"
                time.sleep(0.2)
                continue
            self._error = None
            with self._lock:
                self._latest = frame
                self._stamp = time.perf_counter()
            self._frames += 1
            time.sleep(FRAME_INTERVAL_S)

    def jpeg(self, quality: int = 80) -> bytes | None:
        """Encode the latest frame as JPEG; ``None`` when no frame has arrived yet.

        The result is cached against the frame's timestamp, so the MJPEG stream and
        the Rerun sink share one encode per captured frame. Without the cache the
        teleop loop would re-encode every camera on every iteration, at 30-60 Hz.
        """
        with self._lock:
            if self._latest is None:
                return None
            if self._jpeg_cache is not None and self._jpeg_stamp == self._stamp:
                return self._jpeg_cache
            frame = self._latest.copy()
            stamp = self._stamp

        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        data = buf.tobytes() if ok else None

        with self._lock:
            self._jpeg_cache, self._jpeg_stamp = data, stamp
        return data

    def status(self) -> dict:
        """Snapshot for the UI."""
        now = time.perf_counter()
        elapsed = now - self._fps_t0 if self._fps_t0 else 0.0
        fps = self._frames / elapsed if elapsed > 0.5 else 0.0
        age_ms = (now - self._stamp) * 1e3 if self._stamp else None
        return {
            "name": self.name,
            "index": self.index,
            "connected": self.connected,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "fps_measured": round(fps, 1),
            "frame_age_ms": None if age_ms is None else round(age_ms, 1),
            "error": self._error,
        }


def _fourcc_str(value: float) -> str:
    code = int(value)
    return "".join(chr((code >> 8 * i) & 0xFF) for i in range(4))


def _scan_targets() -> list[int | str]:
    """Which camera identifiers to probe on this platform.

    Linux exposes cameras as ``/dev/video*``, so probing integer indices there is
    both slow and unreliable. Windows and macOS address them by index.
    """
    if platform.system() == "Linux":
        return sorted(glob.glob("/dev/video*"))
    return list(range(MAX_CAMERA_INDEX))


def scan_cameras() -> list[dict]:
    """Enumerate available cameras.

    Returns:
        One entry per camera with ``index`` (int on Windows, ``/dev/videoN`` on
        Linux) / ``width`` / ``height`` / ``fps`` / ``fourcc``. ``fps`` is usually
        0 before the stream starts, which is normal and not a fault.
    """
    found: list[dict] = []
    for target in _scan_targets():
        cap = cv2.VideoCapture(target, CAP_BACKEND)
        try:
            if not cap.isOpened():
                continue
            found.append(
                {
                    "index": target,
                    "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                    "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                    "fps": round(float(cap.get(cv2.CAP_PROP_FPS)), 2),
                    "fourcc": _fourcc_str(cap.get(cv2.CAP_PROP_FOURCC)).strip("\x00"),
                }
            )
        finally:
            cap.release()
    return found


class CameraManager:
    """Holds a set of :class:`CameraSlot` objects; thread-safe."""

    def __init__(self) -> None:
        self._slots: dict[str, CameraSlot] = {}
        self._lock = threading.Lock()

    def open(self, name: str, index: int | str, *, width=640, height=480, fps=30,
             fourcc=None) -> None:
        """Open (or reopen) one camera."""
        self.close(name)
        slot = CameraSlot(name=name, index=index, width=width, height=height, fps=fps, fourcc=fourcc)
        slot.open()
        with self._lock:
            self._slots[name] = slot

    def close(self, name: str) -> None:
        with self._lock:
            slot = self._slots.pop(name, None)
        if slot is not None:
            slot.close()

    def close_all(self) -> None:
        with self._lock:
            names = list(self._slots)
        for name in names:
            self.close(name)

    def get(self, name: str) -> CameraSlot | None:
        with self._lock:
            return self._slots.get(name)

    def names(self) -> list[str]:
        with self._lock:
            return list(self._slots)

    def status(self) -> dict[str, dict]:
        with self._lock:
            slots = list(self._slots.values())
        return {s.name: s.status() for s in slots}

    def mjpeg(self, name: str, fps: float = 15.0):
        """Yield a ``multipart/x-mixed-replace`` frame stream for an ``<img src>``.

        Closing the client connection closes the generator, which ends the loop.
        """
        period = 1.0 / fps
        boundary = b"--frame"
        while True:
            slot = self.get(name)
            if slot is None:
                return
            data = slot.jpeg()
            if data is not None:
                yield (
                    boundary
                    + b"\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(data)).encode()
                    + b"\r\n\r\n"
                    + data
                    + b"\r\n"
                )
            time.sleep(period)
