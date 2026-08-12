import json
import uuid
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse

app = FastAPI()

WORKDIR = Path("/tmp/ffmpeg_jobs")
WORKDIR.mkdir(parents=True, exist_ok=True)

# --- Encoding quality configuration -----------------------------------------
# CRF: lower = better quality / larger file. 18 is visually near-transparent,
# 23 is the x264 default (noticeably lossy on fine detail and subtitle edges).
# Preset: slower = better compression efficiency at the same CRF, but more CPU.
DEFAULT_CRF = 16
DEFAULT_PRESET = "slow"
DEFAULT_AUDIO_BITRATE = "192k"
DEFAULT_FPS = 30

# Audio mixing defaults for CASE 2 (video with its own audio + narration).
# The original track is attenuated and further ducked while narration plays,
# so speech stays intelligible without discarding the source atmosphere.
DEFAULT_KEEP_ORIGINAL_AUDIO = True
DEFAULT_ORIGINAL_VOLUME = 0.35   # static attenuation applied to source audio
DEFAULT_DUCK_RATIO = 8           # sidechain compression ratio while narrating
DEFAULT_DUCK_THRESHOLD = 0.03    # narration level that triggers ducking


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
        # High profile + level 4.0 keeps broad device compatibility while
        # allowing CABAC and 8x8 transform, which improve efficiency.
        "-profile:v", "high",
        "-level", "4.0",
    ]


@app.get("/")
def root():
    # EasyPanel health check endpoint
    return {"ok": True}


@app.get("/health")
def health():
    # Manual health check endpoint
    return {"status": "ok"}

@app.post("/get-duration")
async def get_duration(video: UploadFile = File(...)):
    # Create a unique folder for this probe job
    job_id = str(uuid.uuid4())
    job_dir = WORKDIR / f"probe_{job_id}"
    job_dir.mkdir(parents=True, exist_ok=True)

    video_path = job_dir / "input_video"

    try:
        # Save uploaded video file
        with open(video_path, "wb") as buffer:
            shutil.copyfileobj(video.file, buffer)

        # Use ffprobe to read media duration
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
                detail={
                    "message": "ffprobe failed",
                    "stderr": result.stderr
                }
            )

        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])

        return {
            "duration": duration,
            "duration_rounded": round(duration),
            "duration_ms": int(duration * 1000)
        }

    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=504,
            detail="ffprobe timed out."
        )

    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))

@app.post("/render-video")
async def render_video(
    video: Optional[UploadFile] = File(None),
    image: Optional[UploadFile] = File(None),
    audio: Optional[UploadFile] = File(None),
    subtitles: Optional[UploadFile] = File(None),
    crf: int = Form(DEFAULT_CRF),
    preset: str = Form(DEFAULT_PRESET),
    audio_bitrate: str = Form(DEFAULT_AUDIO_BITRATE),
    keep_original_audio: bool = Form(DEFAULT_KEEP_ORIGINAL_AUDIO),
    original_volume: float = Form(DEFAULT_ORIGINAL_VOLUME)
):
    # Create a unique folder per render job
    job_id = str(uuid.uuid4())
    job_dir = WORKDIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    video_path = job_dir / "input.mp4"
    image_path = job_dir / "input_image.png"
    audio_path = job_dir / "voice.mp3"
    subtitles_path = job_dir / "subtitles.ass"
    output_path = job_dir / "output.mp4"

    try:
        if not video and not image:
            raise HTTPException(
                status_code=400,
                detail="You must provide either a video or an image."
            )

        # Save uploaded video if provided
        if video:
            with open(video_path, "wb") as buffer:
                shutil.copyfileobj(video.file, buffer)

        # Save uploaded image if provided
        if image:
            with open(image_path, "wb") as buffer:
                shutil.copyfileobj(image.file, buffer)

        # Save uploaded audio if provided
        if audio:
            with open(audio_path, "wb") as buffer:
                shutil.copyfileobj(audio.file, buffer)

        # Save uploaded ASS subtitles if provided
        if subtitles:
            with open(subtitles_path, "wb") as buffer:
                shutil.copyfileobj(subtitles.file, buffer)

        command = ["ffmpeg", "-y"]

        # CASE 1:
        # If an image is provided, create a video from the image.
        # The output duration is audio duration + 2 seconds.
        if image:
            if not audio:
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

            if subtitles:
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
        # If a video is provided and audio is provided, replace the video audio.
        elif video and audio:
            # Decide whether the source audio can and should be preserved.
            source_has_audio = has_audio_stream(video_path)
            mix_audio = keep_original_audio and source_has_audio

            # Measure both inputs so the output can span the longer of the two
            # instead of being truncated by -shortest.
            video_duration = probe_duration(video_path)
            audio_duration = probe_duration(audio_path)
            target_duration = max(video_duration, audio_duration)

            video_pad = max(0.0, target_duration - video_duration)
            audio_pad = max(0.0, target_duration - audio_duration)

            # -vf and -filter_complex cannot coexist on one output, so the
            # video chain is declared inside filter_complex when mixing.
            video_chain = "[0:v]format=yuv420p"

            if subtitles:
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

            # Pad whichever audio track ends early so both span the full output.
            narration_pad = (
                f",apad=pad_dur={audio_pad:.3f}" if audio_pad > 0.05 else ""
            )
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
                command += [
                    "-filter_complex", f"{video_chain};{audio_chain}",
                    "-map", "[vout]",
                    "-map", "[aout]",
                ]
            else:
                # No usable source audio: fall back to narration only.
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
                # Explicit duration replaces -shortest: the output now spans
                # the longer input instead of being cut to the shorter one.
                "-t", f"{target_duration:.3f}",
                "-movflags", "+faststart",
                str(output_path)
            ]

        # CASE 3:
        # If a video is provided without audio, keep its original audio.
        else:
            if subtitles:
                # Subtitles must be burned in, so a re-encode is unavoidable.
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
                    # source audio is passed through untouched: no extra
                    # generation of lossy encoding, original volume preserved.
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
            timeout=900
        )

        if result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail={
                    "message": "FFmpeg failed",
                    "command": " ".join(command),
                    "stderr": result.stderr
                }
            )

        return FileResponse(
            path=output_path,
            media_type="video/mp4",
            filename="output.mp4"
        )

    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=504,
            detail="FFmpeg render timed out."
        )

    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))
