"""Persistent WinRT OCR host.

A PowerShell process that starts once and then serves OCR requests over stdin/stdout.

Why this exists, with the measurement that motivated it: on the development machine a
one-shot PowerShell OCR call costs ~1700 ms, of which ~830 ms is fixed overhead -
578 ms just to spawn PowerShell, plus ~250 ms resolving the WinRT projection types. None
of that is recoverable by caching, because it is paid again on every process launch. A
resident host pays it once, leaving only the actual recognition cost per frame.

Protocol: one image path per stdin line, one JSON object per stdout line. Any error is
returned as a JSON object with an ``error`` field rather than by dying, so a single bad
frame cannot take the host (and therefore the run) down. The host exits when stdin
closes, which is how shutdown is signalled.

The host is a *supervisor* concern, not a perception one: if it dies, OCR degrades to
unavailable and the run continues with vision-only signals.
"""

from __future__ import annotations

import json
import os

from frameforge.actions.safety import hardened_child_env
import subprocess
import tempfile
import threading
from pathlib import Path

#: The resident host script. Reads lines of the form ``<image_path>``, writes one JSON
#: object per line. `READY` is emitted once the WinRT types are resolved so the client
#: knows the fixed startup cost has already been paid.
HOST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null

[Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]          | Out-Null
[Windows.Storage.FileAccessMode,Windows.Storage,ContentType=WindowsRuntime]       | Out-Null
[Windows.Storage.Streams.IRandomAccessStream,Windows.Storage.Streams,ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime]     | Out-Null
[Windows.Graphics.Imaging.SoftwareBitmap,Windows.Graphics.Imaging,ContentType=WindowsRuntime]     | Out-Null
[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]        | Out-Null
[Windows.Globalization.Language,Windows.Globalization,ContentType=WindowsRuntime]  | Out-Null

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and
    $_.GetParameters().Count -eq 1 -and
    $_.IsGenericMethod -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]

function Await($op, $type) {
    $task = $asTaskGeneric.MakeGenericMethod($type).Invoke($null, @($op))
    $task.Wait(-1) | Out-Null
    $task.Result
}

# Resolve the engine once. Recreating it per request costs measurably and gains nothing.
$engine = $null
$engineTag = 'unknown'
foreach ($l in @('en-US','en-GB')) {
    try {
        $lang = New-Object Windows.Globalization.Language $l
        $c = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
        if ($null -ne $c) { $engine = $c; $engineTag = $l; break }
    } catch { }
}
if ($null -eq $engine) {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
    if ($null -ne $engine) { $engineTag = $engine.RecognizerLanguage.LanguageTag }
}
if ($null -eq $engine) { Write-Output '{"error":"no OCR engine available"}'; exit 1 }

# Warm the pipeline once so the first real request is not an outlier.
try {
    $tmpBmp = New-Object Windows.Graphics.Imaging.SoftwareBitmap 32, 32, 0, 0
    $tmpBmp.Dispose()
} catch { }

[Console]::Out.WriteLine('{"ready":true,"engine":"' + $engineTag + '"}')
[Console]::Out.Flush()

while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { break }
    $line = $line.Trim()
    if ($line.Length -eq 0) { continue }
    if ($line -eq '__quit__') { break }

    try {
        $file    = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($line)) ([Windows.Storage.StorageFile])
        $stream  = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap  = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        $sw.Stop()
        $recognizeMs = $sw.Elapsed.TotalMilliseconds
        $bitmap.Dispose()
        $stream.Dispose()

        # PowerShell's WinRT projection flattens a single-element collection into a
        # scalar, so a one-word line yields an Object[] where a Rect is expected and
        # [double] conversion throws. Every field is therefore read through @(...)[0],
        # which is correct for both the scalar and the single-element-array case.
        $lines = @()
        foreach ($wl in $result.Lines) {
            $words = @($wl.Words)
            $wr = $wl.BoundingRect
            if ($words.Count -gt 0) { $wr = ($words | Select-Object -First 1).BoundingRect }
            $x  = [double](@($wr.X)     | Select-Object -First 1)
            $y  = [double](@($wr.Y)     | Select-Object -First 1)
            $ww = [double](@($wr.Width) | Select-Object -First 1)
            $hh = [double](@($wr.Height)| Select-Object -First 1)
            if ($ww -lt 1) { $ww = [math]::Max(8, $words.Count * 9) }
            if ($hh -lt 1) { $hh = 16 }
            $lines += ,([ordered]@{
                text = [string]$wl.Text
                x = [int][math]::Round($x)
                y = [int][math]::Round($y)
                w = [int][math]::Round($ww)
                h = [int][math]::Round($hh)
            })
        }
        $payload = [ordered]@{ lines = $lines; recognize_ms = [math]::Round($recognizeMs, 1); engine = $engineTag }
        [Console]::Out.WriteLine(($payload | ConvertTo-Json -Depth 5 -Compress))
        [Console]::Out.Flush()
    } catch {
        [Console]::Out.WriteLine(('{"error":"' + ($_.Exception.Message -replace '"',"'") + '"}'))
        [Console]::Out.Flush()
    }
}
"""


class OcrHostError(RuntimeError):
    """The persistent OCR host failed or is unavailable."""


class PersistentOcrHost:
    """A resident PowerShell WinRT OCR process, driven one request at a time.

    Thread-safe via a lock: OCR is inherently serial (one WinRT engine), and allowing two
    callers to interleave requests on the same stdin would desynchronise responses. The
    lock turns a subtle corruption bug into a clean wait.
    """

    def __init__(self, *, startup_timeout_s: float = 30.0) -> None:
        self._proc: subprocess.Popen | None = None
        self._script_path: str | None = None
        self._lock = threading.Lock()
        self._engine = "unknown"
        self._ready = False
        self._startup_error: str | None = None
        self.requests = 0
        self.recognize_ms_total = 0.0
        self._start(startup_timeout_s)

    def _start(self, timeout_s: float) -> None:
        # Write the script to a file and run it with -File, leaving stdin open. Feeding
        # the script through stdin instead would close the pipe, and then there would be
        # no channel left to send requests on.
        try:
            fd, script_path = tempfile.mkstemp(suffix=".ps1")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(HOST_SCRIPT)
            self._script_path = script_path
        except Exception as exc:
            self._startup_error = f"could not stage host script: {exc}"
            self._proc = None
            return

        self._proc = subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", script_path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        
            env=hardened_child_env(),)

        line = self._read_line(timeout_s)
        if line is None:
            self._startup_error = "OCR host did not report ready"
            self.close()
            return
        try:
            payload = json.loads(line)
        except ValueError:
            self._startup_error = f"unparseable handshake: {line[:120]}"
            self.close()
            return
        if "error" in payload:
            self._startup_error = str(payload["error"])
            self.close()
            return
        self._engine = str(payload.get("engine", "unknown"))
        self._ready = True

    def _read_line(self, timeout_s: float) -> str | None:
        """Read one response line with a timeout.

        Readline on a pipe can block indefinitely, so a watchdog thread is used to bound
        it. A hung OCR call must not hang a run forever.
        """
        result: list[str | None] = [None]

        def _reader() -> None:
            try:
                assert self._proc is not None and self._proc.stdout is not None
                result[0] = self._proc.stdout.readline()
            except Exception:
                result[0] = None

        thread = threading.Thread(target=_reader, daemon=True)
        thread.start()
        thread.join(timeout_s)
        if thread.is_alive():
            return None
        return result[0]

    # ----------------------------------------------------------------- public API

    @property
    def ready(self) -> bool:
        return self._ready and self._proc is not None and self._proc.poll() is None

    @property
    def engine(self) -> str:
        return self._engine

    @property
    def mean_recognize_ms(self) -> float:
        return self.recognize_ms_total / self.requests if self.requests else 0.0

    def recognize_path(self, image_path: Path, *, timeout_s: float = 15.0) -> dict:
        """Run OCR on an already-written PNG. Returns the host's JSON payload."""
        if not self.ready:
            msg = self._startup_error or "OCR host not ready"
            raise OcrHostError(msg)
        assert self._proc is not None and self._proc.stdin is not None
        with self._lock:
            try:
                self._proc.stdin.write(f"{image_path}\n")
                self._proc.stdin.flush()
            except Exception as exc:
                self._ready = False
                msg = f"OCR host stdin closed: {exc}"
                raise OcrHostError(msg) from exc
            line = self._read_line(timeout_s)
            if line is None:
                self._ready = False
                msg = "OCR host timed out or died"
                raise OcrHostError(msg)
        try:
            payload = json.loads(line)
        except ValueError as exc:
            msg = f"unparseable OCR response: {line[:160]}"
            raise OcrHostError(msg) from exc
        self.requests += 1
        if not payload.get("error"):
            self.recognize_ms_total += float(payload.get("recognize_ms", 0.0))
        return payload

    def close(self) -> None:
        proc, self._proc = self._proc, None
        self._ready = False
        if proc is None:
            return
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.write("__quit__\n")
                proc.stdin.flush()
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        if self._script_path:
            try:
                os.unlink(self._script_path)
            except Exception:
                pass
            self._script_path = None

    def __enter__(self) -> PersistentOcrHost:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = ["HOST_SCRIPT", "OcrHostError", "PersistentOcrHost"]
