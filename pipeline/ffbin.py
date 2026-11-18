"""Find ffmpeg: system binary, else the static one from the pip package imageio-ffmpeg."""
import shutil
def ffmpeg():
    p = shutil.which("ffmpeg")
    if p:
        return p
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()
FFMPEG = ffmpeg()
