import uuid
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse

app = FastAPI()

WORKDIR = Path("/tmp/ffmpeg_jobs")
WORKDIR.mkdir(parents=True, exist_ok=True)


@app.get("/")
def root():
    # EasyPanel health check endpoint
    return {"ok": True}


@app.get("/health")
def health():
    # Manual health check endpoint
    return {"status": "ok"}


@app.post("/render-video")
async def render_video(
    video: Optional[UploadFile] = File(None),
    image: Optional[UploadFile] = File(None),
    audio: Optional[UploadFile] = File(None),
    subtitles: Optional[UploadFile] = File(None)
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

            video_filter = (
                "scale=1080:1920:force_original_aspect_ratio=decrease,"
                "pad=1080:1920:(ow-iw)/2:(oh-ih)/2,"
                "format=yuv420p"
            )

            if subtitles:
                ass_path = str(subtitles_path).replace("\\", "\\\\").replace(":", "\\:")
                video_filter += f",ass={ass_path}"

            command += [
                "-vf", video_filter,
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-c:v", "libx264",
                "-c:a", "aac",
                "-af", "apad=pad_dur=2",
                "-shortest",
                "-movflags", "+faststart",
                str(output_path)
            ]

        # CASE 2:
        # If a video is provided and audio is provided, replace the video audio.
        elif video and audio:
            video_filter = "format=yuv420p"

            if subtitles:
                ass_path = str(subtitles_path).replace("\\", "\\\\").replace(":", "\\:")
                video_filter += f",ass={ass_path}"

            command += [
                "-i", str(video_path),
                "-i", str(audio_path),
                "-vf", video_filter,
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-c:v", "libx264",
                "-c:a", "aac",
                "-af", "apad",
                "-shortest",
                "-movflags", "+faststart",
                str(output_path)
            ]

        # CASE 3:
        # If a video is provided without audio, keep its original audio.
        else:
            video_filter = "format=yuv420p"

            if subtitles:
                ass_path = str(subtitles_path).replace("\\", "\\\\").replace(":", "\\:")
                video_filter += f",ass={ass_path}"

            command += [
                "-i", str(video_path),
                "-vf", video_filter,
                "-map", "0:v:0",
                "-map", "0:a?",
                "-c:v", "libx264",
                "-c:a", "aac",
                "-movflags", "+faststart",
                str(output_path)
            ]

        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=300
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
