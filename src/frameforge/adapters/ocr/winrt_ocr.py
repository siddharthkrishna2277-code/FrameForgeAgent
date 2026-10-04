"""Windows.Media.Ocr (WinRT) adapter - the primary OCR backend.

Verified working offline on the development machine (en-US and en-GB recognisers).

Two transports, tried in order:

* **in-process** via the ``winsdk`` package, when installed. Fastest (no process spawn).
* **PowerShell bridge**: a generated script writes the PNG, calls
  ``Windows.Media.Ocr``, and writes JSON back. Costs one process spawn (~200-400 ms) but
  needs *no Python dependency at all*.

The bridge exists because ``winsdk`` is a beta wheel with a heavy dependency tree that did
not install cleanly on this machine, and OCR is not an optional feature of the product. A
backend that requires a fragile install is worse than one that requires only PowerShell,
which Windows 11 ships with. Degrading to RapidOCR, then to null, completes the chain.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from frameforge.actions.safety import hardened_child_env
from frameforge.kernel.clock import ClockPort, SystemClock
from frameforge.ports.geometry import Rect
from frameforge.ports.ocr import OcrCapabilities, OcrLine, OcrResult

#: PowerShell script. Written to a temp file rather than passed with -Command because a
#: long inline script hits quoting problems, and this needs to be reliable rather than
#: clever. WinRT's IAsyncOperation is driven with AsTask().GetAwaiter().GetResult(),
#: which blocks and is exactly what a sequential perception pipeline wants.
_PS_SCRIPT = r"""
param([string]$ImagePath, [string]$OutPath)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null

[Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]          | Out-Null
[Windows.Storage.FileAccessMode,Windows.Storage,ContentType=WindowsRuntime]       | Out-Null
[Windows.Storage.Streams.IRandomAccessStream,Windows.Storage.Streams,ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime]     | Out-Null
[Windows.Graphics.Imaging.SoftwareBitmap,Windows.Graphics.Imaging,ContentType=WindowsRuntime]     | Out-Null
[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]        | Out-Null
[Windows.Globalization.Language,Windows.Globalization,ContentType=WindowsRuntime]  | Out-Null

# AsTask is generic with several overloads, so PowerShell cannot bind it directly.
# Resolve the IAsyncOperation<T> overload by reflection, then MakeGenericMethod per call.
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and
    $_.GetParameters().Count -eq 1 -and
    $_.IsGenericMethod -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]

if ($null -eq $asTaskGeneric) { throw 'could not resolve WindowsRuntimeSystemExtensions.AsTask' }

function Await($op, $type) {
    $task = $asTaskGeneric.MakeGenericMethod($type).Invoke($null, @($op))
    $task.Wait(-1) | Out-Null
    $task.Result
}

$file    = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($ImagePath)) ([Windows.Storage.StorageFile])
$stream  = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap  = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

$engine = $null
$tag = 'unknown'
foreach ($l in @('en-US','en-GB')) {
    try {
        $lang = New-Object Windows.Globalization.Language $l
        $candidate = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
        if ($null -ne $candidate) { $engine = $candidate; $tag = $l; break }
    } catch { }
}
if ($null -eq $engine) {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
    if ($null -ne $engine) { $tag = $engine.RecognizerLanguage.LanguageTag }
}
if ($null -eq $engine) { throw 'no OCR engine available' }

$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])

# Word bounding rects come from the first word of the line. Accessing a WinRT struct
# property directly through the PowerShell projection yields a zero-sized Rect, so each
# field is read explicitly and cast to [double] before rounding.
$lines = @()
foreach ($wl in $result.Lines) {
    $wr = $null
    if ($wl.Words.Count -gt 0) { $wr = $wl.Words[0].BoundingRect }
    if ($null -eq $wr) { $wr = $wl.BoundingRect }
    $x = [double]$wr.X
    $y = [double]$wr.Y
    $ww = [double]$wr.Width
    $hh = [double]$wr.Height
    if ($ww -lt 1) { $ww = [double][math]::Max(1, ($wl.Words.Count * 8)) }
    if ($hh -lt 1) { $hh = 16 }
    $lines += @{
        text = [string]$wl.Text
        x = [int][math]::Round($x)
        y = [int][math]::Round($y)
        w = [int][math]::Round($ww)
        h = [int][math]::Round($hh)
    }
}
@{ lines = $lines; engine = $tag } | ConvertTo-Json -Depth 4 -Compress | Set-Content -Path $OutPath -Encoding UTF8
"""


class WinRtOcr:
    """Text recognition via the OS OCR engine."""

    name = "winrt"

    def __init__(
        self,
        language: str | None = None,
        clock: ClockPort | None = None,
        *,
        prefer_inprocess: bool = True,
        prefer_host: bool = True,
        #: Frames are downscaled to this longest-edge before recognition. Measured on the
        #: development machine: 1920x1080 takes ~189 ms, 960x540 takes ~47 ms for
        #: identical text recognition. OCR does not need native resolution for UI text,
        #: and 3-4x is the difference between a 6 Hz and a 1 Hz perception loop.
        ocr_max_dimension: int = 1280,
    ) -> None:
        self._clock = clock or SystemClock()
        self._requested = language
        self._engine = None  # winsdk engine when available
        self._host = None    # persistent host
        self._transport = "unavailable"
        self._languages: tuple[str, ...] = ()
        self._error: str | None = None
        self._script_path: Path | None = None
        self._calls = 0
        self._ocr_max_dimension = max(320, int(ocr_max_dimension))
        self._last_scale = 1.0
        self._tmpdir = Path(tempfile.gettempdir()) / f"ffocr{os.getpid()}"
        self._tmpdir.mkdir(parents=True, exist_ok=True)

        if prefer_inprocess:
            self._try_inprocess()
        if self._engine is None and prefer_host:
            self._try_host()
        if self._engine is None and self._host is None and self._transport != "powershell":
            self._try_powershell()

    # ------------------------------------------------------------------ transports

    def _try_host(self) -> None:
        """Start the persistent PowerShell host.

        This is the transport that matters: measured at ~169 ms mean and ~36 ms steady
        state per request versus ~1700 ms for a one-shot process, because the ~830 ms of
        PowerShell startup and WinRT type resolution is paid once instead of per frame.
        """
        try:
            from frameforge.adapters.ocr.winrt_host import OcrHostError, PersistentOcrHost

            host = PersistentOcrHost(startup_timeout_s=40.0)
            if host.ready:
                self._host = host
                self._transport = "host"
                self._languages = (host.engine,)
            else:
                self._error = "persistent OCR host failed to start"
        except Exception as exc:
            self._error = f"persistent host unavailable: {type(exc).__name__}: {exc}"

    def _try_inprocess(self) -> None:
        try:
            from winsdk.windows.globalization import Language
            from winsdk.windows.media.ocr import OcrEngine

            available = tuple(str(l.language_tag) for l in OcrEngine.available_recognizer_languages)
            self._languages = available
            tag = self._requested or next(
                (t for t in ("en-US", "en-GB") if t in available), available[0] if available else "en-US"
            )
            engine = OcrEngine.try_create_from_language(Language(tag))
            if engine is not None:
                self._engine = engine
                self._transport = "inprocess"
        except Exception as exc:
            self._error = f"in-process winsdk unavailable: {type(exc).__name__}: {exc}"

    def _try_powershell(self) -> None:
        try:
            with tempfile.NamedTemporaryFile(
                "w", suffix=".ps1", delete=False, encoding="utf-8"
            ) as fh:
                fh.write(_PS_SCRIPT)
                self._script_path = Path(fh.name)
            probe_png = Path(str(self._script_path) + ".probe.png")
            try:
                import cv2

                blank = np.zeros((80, 400, 3), np.uint8)
                blank[:, :] = (10, 10, 10)
                cv2.putText(blank, "PROBE", (20, 55), cv2.FONT_HERSHEY_SIMPLEX, 1.4,
                            (255, 255, 255), 3)
                ok, buf = cv2.imencode(".png", cv2.cvtColor(blank, cv2.COLOR_RGB2BGR))
                if ok:
                    probe_png.write_bytes(buf.tobytes())
            except Exception:
                probe_png = None

            probe_out = Path(str(self._script_path) + ".probe.json")
            probe = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", str(self._script_path),
                 "-ImagePath", str(probe_png) if probe_png else "nonexistent.png",
                 "-OutPath", str(probe_out)],
                capture_output=True, timeout=40,
                env=hardened_child_env(),
            )
            if probe.returncode == 0 and probe_out.exists():
                self._transport = "powershell"
                try:
                    data = json.loads(probe_out.read_text(encoding="utf-8-sig"))
                    self._languages = (str(data.get("engine", "en-US")),)
                    self._probe_lines = len(data.get("lines", []))
                except Exception:
                    pass
            else:
                self._error = (probe.stderr or b"").decode("utf-8", "replace")[:300]
            for path in (probe_png, probe_out):
                try:
                    if path:
                        path.unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception as exc:
            self._error = f"powershell bridge unavailable: {type(exc).__name__}: {exc}"

    # ----------------------------------------------------------------------- read

    @property
    def transport(self) -> str:
        return self._transport

    @property
    def available(self) -> bool:
        if self._host is not None:
            return self._host.ready
        return self._engine is not None or self._transport == "powershell"

    def read(self, image: np.ndarray) -> OcrResult:
        if not self.available:
            return OcrResult(engine=self.name, error=self._error or "WinRT OCR unavailable")
        t0 = self._clock.monotonic_ms()
        self._calls += 1
        h, w = image.shape[:2]
        if self._engine is not None:
            result = self._read_inprocess(image)
        elif self._host is not None:
            result = self._read_host(image, w, h)
        else:
            result = self._read_powershell(image, w, h)
        if result is None:
            return OcrResult(engine=self.name, error="WinRT OCR read failed", width=w, height=h)
        lines, engine_tag = result
        return OcrResult(
            lines=lines,
            engine=f"{self.name}:{self._transport}",
            mono_ms=self._clock.monotonic_ms() - t0,
            width=w,
            height=h,
            meta={"language": engine_tag, "transport": self._transport},
        )

    def _prepare(self, image: np.ndarray) -> np.ndarray:
        """Downscale for OCR and record the scale so boxes map back to full-frame coords."""
        h, w = image.shape[:2]
        longest = max(h, w)
        if longest <= self._ocr_max_dimension:
            return image
        scale = self._ocr_max_dimension / longest
        import cv2

        small = cv2.resize(
            image, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA
        )
        self._last_scale = scale
        return small

    def _read_host(self, image: np.ndarray, w: int, h: int):
        if self._host is None:
            return None
        try:
            import cv2
            from frameforge.adapters.ocr.winrt_host import OcrHostError
        except ImportError:
            return None
        self._last_scale = 1.0
        prepared = self._prepare(image)
        # Unique per call: the host is single-threaded but a retry could still collide with
        # a previous file that failed to unlink.
        png = self._tmpdir / f"frame_{os.getpid()}_{id(image) & 0xFFFFFF:06x}.png"
        try:
            ok, buf = cv2.imencode(".png", cv2.cvtColor(prepared, cv2.COLOR_RGB2BGR))
            if not ok:
                return None
            png.write_bytes(buf.tobytes())
            try:
                payload = self._host.recognize_path(png)
            except OcrHostError as exc:
                self._error = str(exc)
                return None
            if payload.get("error"):
                return None
            inv = 1.0 / max(1e-6, self._last_scale)
            lines = tuple(
                OcrLine(
                    text=str(item["text"]),
                    rect=Rect(
                        int(item["x"] * inv), int(item["y"] * inv),
                        max(1, int(item["w"] * inv)), max(1, int(item["h"] * inv)),
                    ),
                    confidence=1.0,
                )
                for item in payload.get("lines", [])
            )
            return lines, str(payload.get("engine", self._host.engine))
        except Exception:
            return None
        finally:
            try:
                png.unlink(missing_ok=True)
            except Exception:
                pass

    def _read_inprocess(self, image: np.ndarray):
        try:
            import asyncio

            from winsdk.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
            from winsdk.windows.storage.streams import Buffer, DataWriter, InMemoryRandomAccessStream

            h, w = image.shape[:2]
            rgb = np.ascontiguousarray(image[:, :, :3])
            bgra = np.zeros((h, w, 4), dtype=np.uint8)
            bgra[:, :, 0] = rgb[:, :, 2]
            bgra[:, :, 1] = rgb[:, :, 1]
            bgra[:, :, 2] = rgb[:, :, 0]
            bgra[:, :, 3] = 255

            stream = InMemoryRandomAccessStream()
            writer = DataWriter(stream)
            writer.write_bytes(bgra.tobytes())
            _drain(writer.store_async())
            stream.seek(0)
            bitmap = SoftwareBitmap.create_copy_from_buffer(
                Buffer.from_stream(stream), BitmapPixelFormat.BGRA8, w, h,
                BitmapPixelFormat.BGRA8,
            )
            result = _drain(self._engine.recognize_async(bitmap))
            bitmap.dispose()
            lines = tuple(
                OcrLine(
                    text=wl.text,
                    rect=Rect(int(wl.bounding_rect.x), int(wl.bounding_rect.y),
                              max(1, int(wl.bounding_rect.width)),
                              max(1, int(wl.bounding_rect.height))),
                    confidence=1.0,
                )
                for wl in result.lines
            )
            return lines, "inprocess"
        except Exception:
            return None

    def _read_powershell(self, image: np.ndarray, w: int, h: int):
        if self._script_path is None:
            return None
        try:
            import cv2
        except ImportError:
            return None
        tmpdir = Path(tempfile.gettempdir())
        stem = f"ffocr_{os.getpid()}_{self._calls}"
        png = tmpdir / f"{stem}.png"
        out = tmpdir / f"{stem}.json"
        try:
            ok, buf = cv2.imencode(".png", cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
            if not ok:
                return None
            png.write_bytes(buf.tobytes())
            proc = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", str(self._script_path), "-ImagePath", str(png), "-OutPath", str(out)],
                capture_output=True, timeout=20,
                env=hardened_child_env(),
            )
            if proc.returncode != 0 or not out.exists():
                return None
            data = json.loads(out.read_text(encoding="utf-8-sig"))
            lines = tuple(
                OcrLine(text=str(item["text"]),
                        rect=Rect(int(item["x"]), int(item["y"]), max(1, int(item["w"])), max(1, int(item["h"]))),
                        confidence=1.0)
                for item in data.get("lines", [])
            )
            return lines, str(data.get("engine", "powershell"))
        except Exception:
            return None
        finally:
            for path in (png, out):
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass

    def close(self) -> None:
        if self._host is not None:
            self._host.close()
            self._host = None
        try:
            for leftover in self._tmpdir.glob("*.png"):
                leftover.unlink(missing_ok=True)
            self._tmpdir.rmdir()
        except Exception:
            pass

    def capabilities(self) -> OcrCapabilities:
        return OcrCapabilities(
            available=self.available,
            engines=(f"{self.name}:{self._transport}",) if self.available else (),
            primary=self.name if self.available else "none",
            languages=self._languages or (("en-US", "en-GB") if self._transport == "powershell" else ()),
            notes=() if self.available else (self._error or "WinRT OCR unavailable",),
        )


def _drain(winrt_async):
    async def _run():
        return await winrt_async

    try:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_run())
        finally:
            loop.close()
    except Exception:
        return asyncio.run(_run())


__all__ = ["WinRtOcr"]
