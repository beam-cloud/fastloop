from beta9 import Image, PythonVersion, function

image = (
    Image(python_version=PythonVersion.Python312)
    .add_python_packages(["whisperx", "yt-dlp"])
    .add_commands(["apt update && apt install ffmpeg -y"])
)


@function(cpu=1, memory="16Gi", gpu="A10G", image=image)
def transcribe(video_url: str) -> str:
    import glob
    import os
    import tempfile

    import whisperx
    import yt_dlp

    with tempfile.TemporaryDirectory() as tmpdir:
        outtmpl = os.path.join(tmpdir, "%(title)s.%(ext)s")
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": outtmpl,
            "quiet": False,
            "verbose": True,
            "cookiefile": "cookies.txt",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([video_url])
        except Exception as e:
            return f"yt-dlp failed: {e}"

        # Find the mp3 file in tmpdir
        mp3_files = glob.glob(os.path.join(tmpdir, "*.mp3"))
        if not mp3_files:
            # Print all files for debugging
            all_files = os.listdir(tmpdir)
            return f"No mp3 file found. Files in tmpdir: {all_files}"

        audio_path = mp3_files[0]

        # Transcribe audio
        model = whisperx.load_model("large-v3", device="cuda", compute_type="float16")
        result = model.transcribe(audio_path)
        return result["text"]


if __name__ == "__main__":
    video_url = transcribe.remote("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    print(video_url)
