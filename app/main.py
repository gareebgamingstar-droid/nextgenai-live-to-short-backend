import os
import uuid
import json
import subprocess
from pathlib import Path

from fastapi import FastAPI, UploadFile, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

BASE = Path("/tmp/nextgenai")
UPLOADS = BASE / "uploads"
JOBS = BASE / "jobs"
OUTPUTS = BASE / "outputs"

for folder in (UPLOADS, JOBS, OUTPUTS):
    folder.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="NextGenAI Live to Short API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def run_cmd(cmd):
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(result.stdout[-5000:])

    return result.stdout


def get_duration(video_path):
    output = run_cmd([
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json",
        str(video_path),
    ])

    return float(json.loads(output)["format"]["duration"])


def make_windows(duration):
    if duration <= 20:
        return [(0, duration)]

    count = min(8, max(3, int(duration / 90)))
    window = min(60, max(15, duration / count))

    if count == 1:
        return [(0, window)]

    step = (duration - window) / (count - 1)

    return [
        (max(0, i * step), min(window, duration - i * step))
        for i in range(count)
    ]


def process_video(job_id, source):
    job_dir = JOBS / job_id
    output_dir = OUTPUTS / job_id

    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        duration = get_duration(source)
        windows = make_windows(duration)

        highlights = []

        for index, (start, length) in enumerate(windows, 1):

            output_file = output_dir / f"short_{index}.mp4"

            run_cmd([
                "ffmpeg",
                "-y",
                "-ss", str(start),
                "-i", str(source),
                "-t", str(length),

                "-vf",
                "scale=1080:1920:force_original_aspect_ratio=increase,"
                "crop=1080:1920",

                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "23",

                "-c:a", "aac",
                "-b:a", "128k",

                "-movflags", "+faststart",

                str(output_file),
            ])

            highlights.append({
                "id": f"{job_id}-{index}",
                "title": f"AI Highlight #{index}",
                "start": round(start, 2),
                "duration": round(length, 2),
                "video_url": f"/api/jobs/{job_id}/files/{output_file.name}",
            })

            progress = int(index / len(windows) * 100)

            (job_dir / "progress.json").write_text(
                json.dumps({
                    "status": "processing",
                    "progress": progress,
                })
            )

        (job_dir / "result.json").write_text(
            json.dumps({
                "status": "completed",
                "progress": 100,
                "highlights": highlights,
            })
        )

    except Exception as error:

        (job_dir / "result.json").write_text(
            json.dumps({
                "status": "error",
                "error": str(error),
            })
        )

    finally:
        try:
            source.unlink(missing_ok=True)
        except Exception:
            pass


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "service": "NextGenAI Live to Short API",
    }


@app.post("/api/upload")
async def upload(
    file: UploadFile,
    background_tasks: BackgroundTasks,
):

    extension = Path(file.filename or "").suffix.lower()

    allowed = {
        ".mp4",
        ".mov",
        ".mkv",
        ".webm",
        ".m4v",
    }

    if extension not in allowed:
        raise HTTPException(
            status_code=400,
            detail="Unsupported video format",
        )

    job_id = uuid.uuid4().hex

    source = UPLOADS / f"{job_id}{extension}"

    with source.open("wb") as output:

        while True:
            chunk = await file.read(1024 * 1024)

            if not chunk:
                break

            output.write(chunk)

    job_dir = JOBS / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    (job_dir / "progress.json").write_text(
        json.dumps({
            "status": "queued",
            "progress": 0,
        })
    )

    background_tasks.add_task(
        process_video,
        job_id,
        source,
    )

    return {
        "job_id": job_id,
        "status": "queued",
    }


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):

    job_dir = JOBS / job_id

    if not job_dir.exists():
        raise HTTPException(
            status_code=404,
            detail="Job not found",
        )

    result_file = job_dir / "result.json"

    if result_file.exists():
        return json.loads(result_file.read_text())

    progress_file = job_dir / "progress.json"

    if progress_file.exists():
        return json.loads(progress_file.read_text())

    return {
        "status": "queued",
        "progress": 0,
    }


@app.get("/api/jobs/{job_id}/files/{filename}")
def download_file(job_id: str, filename: str):

    file_path = OUTPUTS / job_id / filename

    if not file_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Video file not found",
        )

    return FileResponse(
        file_path,
        media_type="video/mp4",
        filename=filename,
    )
