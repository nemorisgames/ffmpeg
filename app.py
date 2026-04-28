import uuid
import shutil
import subprocess
from pathlib import Path

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
    video: UploadFile = File(...),
    audio: UploadFile = File(...),
    subtitles: UploadFile = File(...)
):
    # Create a unique folder per render job
    job_id = str(uuid.uuid4())
    job_dir = WORKDIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    video_path = job_dir / "input.mp4"
    audio_path = job_dir / "voice.mp3"
    subtitles_path = job_dir / "subtitles.ass"
    output_path = job_dir / "output.mp4"

    try:
        # Save uploaded video
        with open(video_path, "wb") as buffer:
            shutil.copyfileobj(video.file, buffer)

        # Save uploaded audio
        with open(audio_path, "wb") as buffer:
            shutil.copyfileobj(audio.file, buffer)

        # Save uploaded ASS subtitles
        with open(subtitles_path, "wb") as buffer:
            shutil.copyfileobj(subtitles.file, buffer)

        # Escape subtitle path for FFmpeg ASS filter
        ass_path = str(subtitles_path).replace("\\", "\\\\").replace(":", "\\:")

        # Burn ASS karaoke subtitles, replace audio, and keep the original video duration
        command = [
            "ffmpeg",
            "-y",
            "-i", str(video_path),
            "-i", str(audio_path),
            "-vf", f"ass={ass_path}",
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-c:v", "libx264",
            "-c:a", "aac",
            "-af", "apad",
            str(output_path)
        ]

        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        if result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail={
                    "message": "FFmpeg failed",
                    "stderr": result.stderr
                }
            )

        return FileResponse(
            path=output_path,
            media_type="video/mp4",
            filename="output.mp4"
        )

    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))
