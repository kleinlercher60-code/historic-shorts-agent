"""
Voice-over for Historic Shorts. One TTS call per scene, so scene length = audio length.

Providers (set TTS_PROVIDER or config.json "tts.provider"):
  elevenlabs  – needs ELEVENLABS_API_KEY; returns exact word timings (best captions)
  openai      – needs OPENAI_API_KEY; word timings are estimated
  none        – silent draft, durations estimated from word count

Only the Python standard library is used (urllib), plus ffmpeg (system or imageio-ffmpeg).
"""
import base64, json, os, re, subprocess, urllib.request, wave
from ffbin import FFMPEG

def _post(url, headers, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()

def duration(path):
    with wave.open(path) as w:          # all voice files are converted to wav first
        return w.getnframes() / w.getframerate()

def to_wav(src, dst):
    subprocess.check_call([FFMPEG, "-y", "-loglevel", "error", "-i", src, "-ar", "44100", "-ac", "1", dst])

def estimate_words(text, total, lead=0.0):
    """Spread words over the clip proportional to their length (+ pauses at punctuation)."""
    words = text.split()
    weights = [len(re.sub(r"\W", "", w)) + 2 + (3 if w[-1:] in ".!?" else 1.5 if w[-1:] in ",;:" else 0) for w in words]
    span = max(0.1, total - lead - 0.1)
    out, acc, tot = [], lead, sum(weights)
    for w, k in zip(words, weights):
        d = span * k / tot
        out.append(dict(word=w, start=acc, end=acc + d * 0.92))
        acc += d
    return out

def _chars_to_words(text, chars, starts, ends):
    words, cur, s0 = [], "", None
    for ch, s, e in zip(chars, starts, ends):
        if ch.isspace():
            if cur:
                words.append(dict(word=cur, start=s0, end=last_e))
                cur = ""
            continue
        if not cur:
            s0 = s
        cur += ch
        last_e = e
    if cur:
        words.append(dict(word=cur, start=s0, end=last_e))
    return words

def synth(text, out_wav, cfg):
    """Returns (duration_seconds, words[]) and writes out_wav (or nothing for provider=none)."""
    tcfg = cfg.get("tts", {})
    provider = os.environ.get("TTS_PROVIDER", tcfg.get("provider", "elevenlabs"))
    if provider == "elevenlabs" and not os.environ.get("ELEVENLABS_API_KEY"):
        provider = "openai" if os.environ.get("OPENAI_API_KEY") else "none"
    if provider == "openai" and not os.environ.get("OPENAI_API_KEY"):
        provider = "none"

    if provider == "elevenlabs":
        voice = tcfg.get("elevenlabs_voice_id", "nPczCjzI2devNBz1zQrb")  # "Brian" – calm narrator
        body = dict(text=text, model_id=tcfg.get("elevenlabs_model", "eleven_multilingual_v2"),
                    voice_settings=dict(stability=0.42, similarity_boost=0.8, style=0.35, use_speaker_boost=True,
                                        speed=tcfg.get("speed", 1.08)))
        raw = _post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice}/with-timestamps?output_format=mp3_44100_128",
                    {"xi-api-key": os.environ["ELEVENLABS_API_KEY"], "Content-Type": "application/json"}, body)
        js = json.loads(raw)
        mp3 = out_wav[:-4] + ".mp3"
        open(mp3, "wb").write(base64.b64decode(js["audio_base64"]))
        to_wav(mp3, out_wav)
        al = js.get("alignment") or js.get("normalized_alignment")
        words = _chars_to_words(text, al["characters"], al["character_start_times_seconds"], al["character_end_times_seconds"])
        return duration(out_wav), words

    if provider == "openai":
        body = dict(model=tcfg.get("openai_model", "gpt-4o-mini-tts"), voice=tcfg.get("openai_voice", "ash"),
                    input=text, response_format="wav",
                    instructions=tcfg.get("openai_instructions",
                        "Dry, deadpan documentary narrator with comedic timing. Brisk pace, tiny pause before punchlines."))
        raw = _post("https://api.openai.com/v1/audio/speech",
                    {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}", "Content-Type": "application/json"}, body)
        tmp = out_wav[:-4] + ".src.wav"
        open(tmp, "wb").write(raw)
        to_wav(tmp, out_wav)
        d = duration(out_wav)
        return d, estimate_words(text, d, lead=0.05)

    # provider == none → silent draft
    n = len(text.split())
    d = n / 3.0 + 0.3   # ~3 words/s ≈ narrator at speed 1.08
    return d, estimate_words(text, d, lead=0.1)
