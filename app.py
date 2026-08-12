import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse

app = FastAPI()

WORKDIR = Path("/tmp/ffmpeg_jobs")
WORKDIR.mkdir(parents=True, exist_ok=True)

# Public base URL of this service, used to build download links.
# Set it in EasyPanel as an environment variable.
BASE_URL = os.environ.get(
    "BASE_URL",
    "https://ffmpeg-ffmpeg-service.rc7vf1.easypanel.host"
).rstrip("/")

# --- Encoding quality configuration -----------------------------------------
# CRF: lower = better quality / larger file. 18 is visually near-transparent,
# 23 is the x264 default (noticeably lossy on fine detail and subtitle edges).
DEFAULT_CRF = 16
DEFAULT_PRESET = "slow"
DEFAULT_AUDIO_BITRATE = "192k"
DEFAULT_FPS = 30

# Audio mixing defaults for CASE 2 (video with its own audio + narration).
DEFAULT_KEEP_ORIGINAL_AUDIO = True
DEFAULT_ORIGINAL_VOLUME = 0.35
DEFAULT_DUCK_RATIO = 8
DEFAULT_DUCK_THRESHOLD = 0.03

# --- Download and retention limits ------------------------------------------
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024   # 2 GB hard ceiling per file
DOWNLOAD_TIMEOUT = 300                         # seconds per remote fetch
JOB_RETENTION_SECONDS = 3600                   # delete finished jobs after 1h
RENDER_TIMEOUT = 1800                          # seconds for the ffmpeg process


# --- Helpers ----------------------------------------------------------------

# Matches the file id in the common Google Drive link shapes:
#   /file/d/<ID>/view      /open?id=<ID>      /uc?id=<ID>
DRIVE_ID_PATTERNS = [
    re.compile(r"/file/d/([A-Za-z0-9_-]{10,})"),
    re.compile(r"[?&]id=([A-Za-z0-9_-]{10,})"),
]


def normalize_drive_url(url: str) -> str:
    """Rewrite a Google Drive sharing link into a direct-download link.

    Sharing links point at an HTML viewer page, not the file itself. Any other
    URL is returned untouched, so non-Drive sources are unaffected.
    """
    if "drive.google.com" not in url and "docs.google.com" not in url:
        return url

    # Already an API media request: leave it alone, it is served with a token.
    if "googleapis.com" in url or "alt=media" in url:
        return url

    for pattern in DRIVE_ID_PATTERNS:
        match = pattern.search(url)
        if match:
            file_id = match.group(1)
            return f"https://drive.google.com/uc?export=download&id={file_id}"

    return url


def download_to_path(url: str, destination: Path, headers: Optional[dict] = None):
    """Stream a remote file to disk without loading it fully into memory.

    Streaming matters here: a 70 MB video read with .content would sit in RAM,
    which is exactly the failure mode this endpoint exists to avoid.
    """
    total = 0
    url = normalize_drive_url(url)

    try:
        with httpx.stream(
            "GET",
            url,
            headers=headers or {},
            follow_redirects=True,
            timeout=DOWNLOAD_TIMEOUT
        ) as response:
            response.raise_for_status()

            content_type = response.headers.get("content-type", "")
            # Google Drive returns an HTML interstitial instead of the file when
            # the link is not a true direct-download URL. Fail loudly here
            # rather than handing FFmpeg a web page and getting a cryptic error.
            if content_type.startswith("text/html"):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "The URL returned HTML, not a media file. "
                        "For Google Drive use a direct download link "
                        "(uc?export=download&id=FILE_ID) or supply an "
                        "Authorization header."
                    )
                )

            with open(destination, "wb") as buffer:
                for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail=f"Remote file exceeds {MAX_DOWNLOAD_BYTES} bytes."
                        )
                    buffer.write(chunk)

    except httpx.HTTPStatusError as error:
        raise HTTPException(
            status_code=400,
            detail=f"Download failed with status {error.response.status_code}: {url}"
        )
    except httpx.RequestError as error:
        raise HTTPException(
            status_code=400,
            detail=f"Could not reach the URL: {error}"
        )

    if total == 0:
        raise HTTPException(status_code=400, detail=f"Downloaded an empty file: {url}")


def save_upload(upload: UploadFile, destination: Path):
    """Persist a multipart upload to disk."""
    with open(destination, "wb") as buffer:
        shutil.copyfileobj(upload.file, buffer)


def resolve_input(
    upload: Optional[UploadFile],
    url: Optional[str],
    destination: Path,
    headers: Optional[dict] = None
) -> bool:
    """Materialise an input from either a multipart upload or a URL.

    Returns True if the input was provided, False if both sources were empty.
    Uploads win when both are present, so existing workflows keep working.
    """
    if upload is not None:
        save_upload(upload, destination)
        # A zero-byte part is treated as absent: some HTTP clients send empty
        # fields rather than omitting them, which would otherwise select the
        # wrong render branch.
        if destination.stat().st_size == 0:
            destination.unlink(missing_ok=True)
            return False
        return True

    if url:
        download_to_path(url, destination, headers)
        return True

    return False


def parse_headers(raw: Optional[str]) -> Optional[dict]:
    """Parse a JSON string of HTTP headers used for authenticated downloads."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError
        return {str(k): str(v) for k, v in parsed.items()}
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail="download_headers must be a JSON object of header name/value pairs."
        )


def probe_duration(path) -> float:
    """Return container duration in seconds, or 0.0 if it cannot be read."""
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            str(path)
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60
    )
    try:
        return float(probe.stdout.strip())
    except (ValueError, AttributeError):
        return 0.0


def has_audio_stream(path) -> bool:
    """Return True if the given media file contains at least one audio stream."""
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "a",
            "-show_entries", "stream=index",
            "-of", "csv=p=0",
            str(path)
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60
    )
    return bool(probe.stdout.strip())


def build_video_encode_opts(crf: int, preset: str) -> list:
    """Return the libx264 encoding flags used by every render branch."""
    return [
        "-c:v", "libx264",
        "-preset", preset,
        "-crf", str(crf),
        "-profile:v", "high",
        "-level", "4.0",
    ]


def cleanup_old_jobs():
    """Delete job folders older than the retention window.

    Without this, every render leaves its inputs and output on disk until the
    container is redeployed, which fills the filesystem quickly at 70 MB a job.
    """
    cutoff = time.time() - JOB_RETENTION_SECONDS
    for folder in WORKDIR.iterdir():
        try:
            if folder.is_dir() and folder.stat().st_mtime < cutoff:
                shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            continue


def start_cleanup_loop():
    """Run the cleanup sweep periodically in a background thread."""
    def loop():
        while True:
            time.sleep(600)
            cleanup_old_jobs()

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()


@app.on_event("startup")
def on_startup():
    cleanup_old_jobs()
    start_cleanup_loop()


# --- Endpoints --------------------------------------------------------------

@app.get("/")
def root():
    # EasyPanel health check endpoint
    return {"ok": True}


@app.get("/health")
def health():
    # Manual health check endpoint
    return {"status": "ok"}


@app.get("/download/{job_id}")
def download(job_id: str):
    """Serve a rendered file by job id.

    The id is validated as a UUID so a crafted value cannot escape WORKDIR.
    """
    try:
        uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job id.")

    output_path = WORKDIR / job_id / "output.mp4"
    if not output_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Job not found or already cleaned up."
        )

    return FileResponse(
        path=output_path,
        media_type="video/mp4",
        filename="output.mp4"
    )


@app.post("/get-duration")
async def get_duration(
    video: Optional[UploadFile] = File(None),
    video_url: Optional[str] = Form(None),
    download_headers: Optional[str] = Form(None)
):
    job_id = str(uuid.uuid4())
    job_dir = WORKDIR / f"probe_{job_id}"
    job_dir.mkdir(parents=True, exist_ok=True)

    video_path = job_dir / "input_video"
    headers = parse_headers(download_headers)

    try:
        if not resolve_input(video, video_url, video_path, headers):
            raise HTTPException(
                status_code=400,
                detail="Provide either a video file or a video_url."
            )

        command = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json",
            str(video_path)
        ]

        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60
        )

        if result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail={"message": "ffprobe failed", "stderr": result.stderr}
            )

        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])

        return {
            "duration": duration,
            "duration_rounded": round(duration),
            "duration_ms": int(duration * 1000)
        }

    except HTTPException:
        raise
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="ffprobe timed out.")
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))
    finally:
        # Probe jobs keep nothing, so remove them immediately.
        shutil.rmtree(job_dir, ignore_errors=True)


@app.post("/render-video")
async def render_video(
    video: Optional[UploadFile] = File(None),
    image: Optional[UploadFile] = File(None),
    audio: Optional[UploadFile] = File(None),
    subtitles: Optional[UploadFile] = File(None),
    video_url: Optional[str] = Form(None),
    image_url: Optional[str] = Form(None),
    audio_url: Optional[str] = Form(None),
    subtitles_url: Optional[str] = Form(None),
    download_headers: Optional[str] = Form(None),
    crf: int = Form(DEFAULT_CRF),
    preset: str = Form(DEFAULT_PRESET),
    audio_bitrate: str = Form(DEFAULT_AUDIO_BITRATE),
    keep_original_audio: bool = Form(DEFAULT_KEEP_ORIGINAL_AUDIO),
    original_volume: float = Form(DEFAULT_ORIGINAL_VOLUME),
    return_file: bool = Form(False)
):
    job_id = str(uuid.uuid4())
    job_dir = WORKDIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    video_path = job_dir / "input.mp4"
    image_path = job_dir / "input_image.png"
    audio_path = job_dir / "voice.mp3"
    subtitles_path = job_dir / "subtitles.ass"
    output_path = job_dir / "output.mp4"

    try:
        headers = parse_headers(download_headers)

        # Each input can arrive as a multipart upload or as a URL to fetch.
        has_video = resolve_input(video, video_url, video_path, headers)
        has_image = resolve_input(image, image_url, image_path, headers)
        has_audio = resolve_input(audio, audio_url, audio_path, headers)
        has_subtitles = resolve_input(subtitles, subtitles_url, subtitles_path, headers)

        if not has_video and not has_image:
            raise HTTPException(
                status_code=400,
                detail="You must provide either a video or an image."
            )

        command = ["ffmpeg", "-y"]

        # CASE 1:
        # If an image is provided, create a video from the image.
        if has_image:
            if not has_audio:
                raise HTTPException(
                    status_code=400,
                    detail="If you provide an image, you must also provide audio."
                )

            command += [
                "-loop", "1",
                "-i", str(image_path),
                "-i", str(audio_path),
                "-t", "999999"
            ]

            # lanczos gives noticeably cleaner edges than the default bicubic,
            # which matters most for text and high-contrast line art.
            video_filter = (
                "scale=1080:1920:force_original_aspect_ratio=decrease:flags=lanczos,"
                "pad=1080:1920:(ow-iw)/2:(oh-ih)/2,"
                "format=yuv420p"
            )

            if has_subtitles:
                ass_path = str(subtitles_path).replace("\\", "\\\\").replace(":", "\\:")
                video_filter += f",ass={ass_path}"

            command += [
                "-vf", video_filter,
                "-r", str(DEFAULT_FPS),
                "-map", "0:v:0",
                "-map", "1:a:0",
            ]
            command += build_video_encode_opts(crf, preset)
            command += [
                "-c:a", "aac",
                "-b:a", audio_bitrate,
                "-af", "apad=pad_dur=2",
                "-shortest",
                "-movflags", "+faststart",
                str(output_path)
            ]

        # CASE 2:
        # Video plus narration: mix or replace the audio, burn subtitles.
        elif has_video and has_audio:
            source_has_audio = has_audio_stream(video_path)
            mix_audio = keep_original_audio and source_has_audio

            # Measure both inputs so the output spans the longer of the two
            # instead of being truncated by -shortest.
            video_duration = probe_duration(video_path)
            audio_duration = probe_duration(audio_path)
            target_duration = max(video_duration, audio_duration)

            video_pad = max(0.0, target_duration - video_duration)
            audio_pad = max(0.0, target_duration - audio_duration)

            # -vf and -filter_complex cannot coexist on one output, so the
            # video chain is declared inside filter_complex.
            video_chain = "[0:v]format=yuv420p"

            if has_subtitles:
                ass_path = str(subtitles_path).replace("\\", "\\\\").replace(":", "\\:")
                video_chain += f",ass={ass_path}"

            # Narration outlasts the footage: hold the final frame rather than
            # cutting the voice off mid-sentence.
            if video_pad > 0.05:
                video_chain += f",tpad=stop_mode=clone:stop_duration={video_pad:.3f}"

            video_chain += "[vout]"

            command += [
                "-i", str(video_path),
                "-i", str(audio_path),
            ]

            source_audio_pad = (
                f",apad=pad_dur={video_pad:.3f}" if video_pad > 0.05 else ""
            )

            if mix_audio:
                # asplit is required because a stream cannot be consumed twice:
                # the narration feeds both the sidechain trigger and the mix.
                # normalize=0 keeps amix from halving the output level.
                audio_chain = (
                    f"[1:a]apad=pad_dur={audio_pad:.3f},asplit=2[narr_a][narr_b];"
                    if audio_pad > 0.05
                    else "[1:a]asplit=2[narr_a][narr_b];"
                )
                audio_chain += (
                    f"[0:a]volume={original_volume}{source_audio_pad}[bg];"
                    f"[bg][narr_a]sidechaincompress="
                    f"threshold={DEFAULT_DUCK_THRESHOLD}:"
                    f"ratio={DEFAULT_DUCK_RATIO}:"
                    f"attack=20:release=400[ducked];"
                    f"[ducked][narr_b]amix=inputs=2:"
                    f"duration=longest:normalize=0[aout]"
                )
            else:
                narration_pad = (
                    f",apad=pad_dur={audio_pad:.3f}" if audio_pad > 0.05 else ""
                )
                audio_chain = f"[1:a]anull{narration_pad}[aout]"

            command += [
                "-filter_complex", f"{video_chain};{audio_chain}",
                "-map", "[vout]",
                "-map", "[aout]",
            ]
            command += build_video_encode_opts(crf, preset)
            command += [
                "-c:a", "aac",
                "-b:a", audio_bitrate,
                # Explicit duration replaces -shortest: the output spans the
                # longer input instead of being cut to the shorter one.
                "-t", f"{target_duration:.3f}",
                "-movflags", "+faststart",
                str(output_path)
            ]

        # CASE 3:
        # Video only: keep its original audio untouched.
        else:
            if has_subtitles:
                # Subtitles must be burned in, so a video re-encode is required.
                ass_path = str(subtitles_path).replace("\\", "\\\\").replace(":", "\\:")
                video_filter = f"format=yuv420p,ass={ass_path}"

                command += [
                    "-i", str(video_path),
                    "-vf", video_filter,
                    "-map", "0:v:0",
                    "-map", "0:a?",
                ]
                command += build_video_encode_opts(crf, preset)
                command += [
                    # Burning subtitles only touches the video stream, so the
                    # source audio is passed through untouched.
                    "-c:a", "copy",
                    "-movflags", "+faststart",
                    str(output_path)
                ]
            else:
                # Nothing modifies the pixels here, so re-encoding would only
                # add a generation of loss. Remux instead: zero quality cost.
                command += [
                    "-i", str(video_path),
                    "-map", "0:v:0",
                    "-map", "0:a?",
                    "-c", "copy",
                    "-movflags", "+faststart",
                    str(output_path)
                ]

        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=RENDER_TIMEOUT
        )

        if result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail={
                    "message": "FFmpeg failed",
                    "command": " ".join(command),
                    "stderr": result.stderr[-4000:]
                }
            )

        # Inputs are no longer needed once the render succeeds; dropping them
        # immediately keeps disk usage roughly one output per job.
        for leftover in (video_path, image_path, audio_path, subtitles_path):
            leftover.unlink(missing_ok=True)

        if return_file:
            return FileResponse(
                path=output_path,
                media_type="video/mp4",
                filename="output.mp4"
            )

        # Default response: a small JSON payload with a URL. n8n never has to
        # hold the rendered video in memory.
        return {
            "job_id": job_id,
            "download_url": f"{BASE_URL}/download/{job_id}",
            "size_bytes": output_path.stat().st_size,
            "duration": probe_duration(output_path),
            "expires_in_seconds": JOB_RETENTION_SECONDS
        }

    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except subprocess.TimeoutExpired:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=504, detail="FFmpeg render timed out.")
    except Exception as error:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(error))
