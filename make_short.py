#!/usr/bin/env python3
"""
Script JSON  →  finished vertical short (1080x1920, 30 fps, H.264 + AAC).

  python3 pipeline/make_short.py scripts/romulus.json            # full render
  python3 pipeline/make_short.py scripts/romulus.json --preview  # contact sheet only (fast QA)
  python3 pipeline/make_short.py scripts/romulus.json --only 3   # re-render one scene, then re-assemble

Output: out/<slug>/<slug>.mp4 (+ preview.png, captions.srt, meta.json)
"""
import argparse, glob, hashlib, json, os, random, shutil, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import render, tts
from ffbin import FFMPEG
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def load_cfg():
    p = os.path.join(ROOT, "config.json")
    return json.load(open(p)) if os.path.exists(p) else {}

def run(cmd):
    subprocess.check_call(cmd)

def srt_time(t):
    h, r = divmod(t, 3600); m, s = divmod(r, 60)
    return f"{int(h):02}:{int(m):02}:{int(s):02},{int((s % 1) * 1000):03}"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("script")
    ap.add_argument("--preview", action="store_true", help="only render a contact sheet of key frames")
    ap.add_argument("--only", type=int, nargs="*", help="re-render only these scene indices")
    args = ap.parse_args()

    cfg = load_cfg()
    sc = json.load(open(args.script))
    slug = sc.get("slug") or os.path.splitext(os.path.basename(args.script))[0]
    od = os.path.join(ROOT, "out", slug)
    os.makedirs(os.path.join(od, "scenes"), exist_ok=True)
    os.makedirs(os.path.join(od, "audio"), exist_ok=True)
    handle = sc.get("handle", cfg.get("handle"))
    scenes = sc["scenes"]

    # 1) voice-over per scene (cached by text hash) → durations + word timings
    timing = []
    for i, s in enumerate(scenes):
        text = s["narration"].strip()
        h = hashlib.md5(json.dumps([text, cfg.get("tts", {}), os.environ.get("TTS_PROVIDER")]).encode()).hexdigest()[:10]
        wav = os.path.join(od, "audio", f"{i:02}_{h}.wav")
        meta = wav[:-4] + ".json"
        if os.path.exists(meta):
            d, words = json.load(open(meta))
        else:
            d, words = tts.synth(text, wav, cfg)
            json.dump([d, words], open(meta, "w"))
        dur = round((d + s.get("pad", 0.22)) * render.FPS) / render.FPS
        timing.append(dict(wav=wav if os.path.exists(wav) else None, dur=dur, words=words))
    total = sum(t["dur"] for t in timing)
    print(f"[{slug}] {len(scenes)} scenes, {total:.1f}s total")

    # 2) preview contact sheet (two frames per scene)
    sheet_frames = []
    for i, (s, tm) in enumerate(zip(scenes, timing)):
        for frac in (0.25, 0.8):
            t = tm["dur"] * frac
            im = render.render_frame(s, t, int(t * render.FPS), tm["words"], tm["dur"], handle, i == len(scenes) - 1)
            sheet_frames.append(im.resize((270, 480)))
    cols = 6
    rows = (len(sheet_frames) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * 270, rows * 480), (40, 40, 40))
    for k, im in enumerate(sheet_frames):
        sheet.paste(im, ((k % cols) * 270, (k // cols) * 480))
    sheet.save(os.path.join(od, "preview.png"))
    if args.preview:
        print("preview:", os.path.join(od, "preview.png"))
        return

    # 3) render scenes (one mp4 each)
    for i, (s, tm) in enumerate(zip(scenes, timing)):
        path = os.path.join(od, "scenes", f"{i:02}.mp4")
        if args.only is not None and i not in args.only and os.path.exists(path):
            continue
        n = render.render_scene(s, tm["dur"], tm["words"], path, handle, i == len(scenes) - 1)
        print(f"  scene {i}: {n} frames")

    # 4) video concat (stream copy)
    lst = os.path.join(od, "scenes.txt")
    with open(lst, "w") as f:
        for i in range(len(scenes)):
            f.write(f"file 'scenes/{i:02}.mp4'\n")
    video = os.path.join(od, "video_only.mp4")
    run([FFMPEG, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", video])

    # 5) audio: each scene's voice padded/trimmed to its exact scene length, then concat
    parts = []
    for i, tm in enumerate(timing):
        p = os.path.join(od, "audio", f"seg{i:02}.wav")
        if tm["wav"]:
            run([FFMPEG, "-y", "-loglevel", "error", "-i", tm["wav"], "-af", f"apad,atrim=0:{tm['dur']:.4f}",
                 "-ar", "44100", "-ac", "1", p])
        else:
            run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                 "-t", f"{tm['dur']:.4f}", p])
        parts.append(p)
    alst = os.path.join(od, "audio.txt")
    with open(alst, "w") as f:
        for p in parts:
            f.write(f"file '{os.path.abspath(p)}'\n")
    voice = os.path.join(od, "voice.wav")
    run([FFMPEG, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", alst, "-c", "copy", voice])

    # optional music bed: any file in assets/music/, looped, ducked low under the voice
    music = sorted(glob.glob(os.path.join(ROOT, "assets", "music", "*.mp3")) + glob.glob(os.path.join(ROOT, "assets", "music", "*.wav")))
    final = os.path.join(od, f"{slug}.mp4")
    if music:
        track = sc.get("music") and os.path.join(ROOT, "assets", "music", sc["music"]) or random.choice(music)
        vol = cfg.get("music_volume", 0.10)
        run([FFMPEG, "-y", "-loglevel", "error", "-i", video, "-i", voice, "-stream_loop", "-1", "-i", track,
             "-filter_complex",
             f"[2:a]volume={vol},afade=t=out:st={total - 1.0:.2f}:d=1.0[m];[1:a][m]amix=inputs=2:duration=first:normalize=0,loudnorm=I=-14:TP=-1.5[a]",
             "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-t", f"{total:.3f}", final])
    else:
        run([FFMPEG, "-y", "-loglevel", "error", "-i", video, "-i", voice, "-map", "0:v", "-map", "1:a",
             "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-t", f"{total:.3f}", final])

    # 6) sidecar files: srt captions + upload metadata
    with open(os.path.join(od, "captions.srt"), "w") as f:
        off, k = 0.0, 1
        for tm in timing:
            for w in tm["words"]:
                f.write(f"{k}\n{srt_time(off + w['start'])} --> {srt_time(off + w['end'])}\n{w['word']}\n\n"); k += 1
            off += tm["dur"]
    json.dump(dict(title=sc.get("yt_title", sc.get("title")), description=sc.get("yt_description", ""),
                   tags=sc.get("tags", []), duration=round(total, 2), sources=sc.get("sources", [])),
              open(os.path.join(od, "meta.json"), "w"), indent=2, ensure_ascii=False)
    for tmp in (video,):
        os.remove(tmp)
    print("done:", final)

if __name__ == "__main__":
    main()
