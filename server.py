import os, time, random, textwrap, tempfile, sys
from pathlib import Path

import requests
from flask import Flask, jsonify
from PIL import Image, ImageDraw, ImageFont
from moviepy.editor import (
    VideoFileClip, ImageClip, AudioFileClip,
    CompositeVideoClip, concatenate_videoclips, vfx
)

# --- ENV VARS ---
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '').strip()
ELEVENLABS_API_KEY = os.getenv('ELEVENLABS_API_KEY', '').strip()
PEXELS_API_KEY = os.getenv('PEXELS_API_KEY', '').strip()

# --- SETTINGS ---
W, H, FPS = 1080, 1920, 30
OUT_DIR = Path('static/output'); OUT_DIR.mkdir(parents=True, exist_ok=True)
MUSIC_DIR = Path('static/music'); MUSIC_DIR.mkdir(parents=True, exist_ok=True)
TMP_DIR = Path(tempfile.gettempdir()) / 'ai_video_creator_web'; TMP_DIR.mkdir(parents=True, exist_ok=True)

PEXELS_QUERIES = [
    'rain window','sea waves','city night','forest light','river sunrise',
    'lonely road','clouds moving','mountain fog','ocean','silhouette walking'
]

app = Flask(__name__)

# -------------------------- Utils --------------------------
def next_output_path():
    i = 1
    while True:
        p = OUT_DIR / f'video_{i:03d}.mp4'
        if not p.exists():
            return p
        i += 1

def _pick_font():
    # Artık TTF aramıyoruz; bitmap fonta düşeceğiz
    return None

    for p in candidates:
        if os.path.exists(p):
            return p
    raise RuntimeError('Sunucuda uygun TrueType font bulunamadı.')

# --------------------- Story Generation ---------------------
def generate_story():
    """
    OPENAI_API_KEY (sk-...) varsa OpenAI'den metin alır,
    yoksa lokal (hazır) Türkçe metin döner.
    """
    prompt = (
        'Türkçe, kısa, duygusal bir ilham metni yaz. 60-75 saniyelik, '
        'sade ve vurucu. Son cümlede "Bugün yeniden dene." gibi çağrı olsun.'
    )
    if OPENAI_API_KEY and OPENAI_API_KEY.startswith('sk-'):
        try:
            headers = {'Authorization': f'Bearer {OPENAI_API_KEY}', 'Content-Type': 'application/json'}
            body = {
                'model': 'gpt-4o-mini',
                'messages': [
                    {'role': 'system', 'content': 'You are a skilled Turkish copywriter for inspirational videos.'},
                    {'role': 'user', 'content': prompt}
                ],
                'temperature': 0.8
            }
            r = requests.post('https://api.openai.com/v1/chat/completions', headers=headers, json=body, timeout=60)
            r.raise_for_status()
            return r.json()['choices'][0]['message']['content'].strip()
        except Exception as e:
            print('OpenAI kullanılamadı, lokal metne düşülüyor:', e, file=sys.stderr)

    # Fallback local text
    return (
        'Bazen her şey üst üste gelir ve insan susar.\n'
        'Ama suskunluk, vazgeçtiğin anlamına gelmez.\n\n'
        'Bir kahve molası kadar kısa bir anda bile, hayatın yönü değişebilir.\n'
        'Bugün kimse seni alkışlamasa da, küçük bir adım at.\n\n'
        'Çünkü sabır, görünmeyen bir tohumdur.\n'
        'Doğru zaman geldiğinde, en derin yerden filiz verir.\n\n'
        'Derin bir nefes al. Bu kez daha sakin, daha kararlı ol.\n'
        'Bugün yeniden dene.'
    )

# ------------------------- TTS ------------------------------
def elevenlabs_tts(text: str):
    """
    Önce ElevenLabs ile ses üretir. 401/başka bir hata olursa otomatik gTTS fallback.
    gTTS Türkçe destekli ve anahtarsızdır.
    """
    out_path = TMP_DIR / "voice.mp3"

    # --- ElevenLabs (ana) ---
    if ELEVENLABS_API_KEY:
        try:
            # İsteğe bağlı olarak Belgin/Cem aramak mümkün; basitlik için direkt default voice kullanıyoruz
            voice_id = "21m00Tcm4TlvDq8ikWAM"  # Rachel (genelde tüm hesaplarda vardır)
            url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
            headers = {
                "xi-api-key": ELEVENLABS_API_KEY,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg"  # ÖNEMLİ
            }
            payload = {
                "text": text,
                "model_id": "eleven_multilingual_v2",
                "voice_settings": {
                    "stability": 0.5,
                    "similarity_boost": 0.7,
                    "style": 0.6,
                    "use_speaker_boost": True
                }
            }
            r = requests.post(url, headers=headers, json=payload, timeout=120)
            r.raise_for_status()
            with open(out_path, "wb") as f:
                f.write(r.content)
            return out_path
        except Exception as e:
            print("ElevenLabs başarısız (devam: gTTS):", e, file=sys.stderr)

    # --- gTTS (fallback) ---
    try:
        from gtts import gTTS
        tts = gTTS(text=text, lang="tr")
        tts.save(out_path)
        return out_path
    except Exception as ee:
        raise RuntimeError(f"TTS başarısız: ElevenLabs ve gTTS kullanılamadı. Ayrıntı: {ee}")

# --------------------- Pexels background --------------------
def fetch_pexels_videos(count: int = 3, query: str | None = None):
    if not PEXELS_API_KEY:
        raise RuntimeError("PEXELS_API_KEY tanımlı değil.")
    if query is None:
        query = random.choice(PEXELS_QUERIES)

    headers = {"Authorization": PEXELS_API_KEY}
    params = {"query": query, "per_page": 10, "orientation": "portrait"}
    r = requests.get("https://api.pexels.com/videos/search", headers=headers, params=params, timeout=30)
    r.raise_for_status()
    vids = r.json().get("videos", [])

    random.shuffle(vids)
    clips = []
    for v in vids[:count]:
        files = sorted(v.get("video_files", []), key=lambda x: x.get("height", 0), reverse=True)
        if not files:
            continue
        link = files[0]["link"]
        filename = TMP_DIR / f"bg_{v['id']}.mp4"   # FIXED: f-string içinde kaçış yok
        with requests.get(link, stream=True, timeout=120) as s:
            s.raise_for_status()
            with open(filename, "wb") as f:
                for chunk in s.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
        clips.append(filename)
        if len(clips) >= count:
            break
    return clips

# ---------------------- Captions (PIL) ----------------------
def text_to_caption_images(text: str, width: int = W):
    # TrueType fontu zorunlu kılmadan, Pillow'un yerleşik bitmap fontunu kullan
    font = ImageFont.load_default()

    parts = [p.strip() for p in text.split("\n") if p.strip()]
    caption_images = []

    for p in parts:
        # Bitmap font daha küçük olduğundan biraz daha geniş saralım
        wrapped = textwrap.fill(p, width=34)
        lines = wrapped.split("\n")

        # Basit yükseklik hesabı (bitmap font için satır başına ~22px)
        line_h = 22
        padding_top = 40
        padding_bottom = 40
        line_gap = 6
        height = padding_top + len(lines) * (line_h + line_gap) + padding_bottom

        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 110))
        img = Image.alpha_composite(img, overlay)
        draw = ImageDraw.Draw(img)

        y = padding_top
        for line in lines:
            w, _ = draw.textsize(line, font=font)
            x = (width - w) // 2
            draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))
            y += line_h + line_gap

        caption_images.append(img)

    return caption_images


# ---------------------- Assemble Video ----------------------
def assemble_video(story_text, voice_path, bg_paths, music_path=None, out_path=None):
    out_path = out_path or next_output_path()

    voice_audio = AudioFileClip(str(voice_path))
    voice_dur = voice_audio.duration
    need_total = voice_dur + 1.5

    # Background klipleri topla
    bg_clips = []
    for p in bg_paths:
        try:
            c = VideoFileClip(str(p)).fx(vfx.resize, height=H)
            c = c.crop(width=W, height=H, x_center=c.w / 2, y_center=c.h / 2)
            bg_clips.append(c)
        except Exception as e:
            print("BG okunamadı:", p, e, file=sys.stderr)

    # Hiç video bulunamazsa siyah zemin
    if not bg_clips:
        img = Image.new("RGB", (W, H), (0, 0, 0))
        tmp = TMP_DIR / "black.png"
        img.save(tmp)
        bg_clips = [ImageClip(str(tmp)).set_duration(need_total)]

    merged = concatenate_videoclips(bg_clips, method="compose")
    if merged.duration < need_total:
        loops = int(need_total // merged.duration) + 1
        merged = concatenate_videoclips([merged] * loops, method="compose")
    merged = merged.subclip(0, need_total).fx(vfx.fadein, 0.6).fx(vfx.fadeout, 0.8)

    # Altyazı görselleri
    caps = text_to_caption_images(story_text, width=W)
    caption_total = voice_dur * 0.85
    per = caption_total / max(1, len(caps))

    clips = [merged.set_audio(voice_audio)]
    t = 0.4
    for img in caps:
        tmp = TMP_DIR / f"cap_{int(time.time() * 1000)}.png"
        img.save(tmp)
        ic = ImageClip(str(tmp)).set_position(("center", "center")).set_duration(per).fx(vfx.fadein, 0.3).fx(vfx.fadeout, 0.3)
        ic = ic.set_start(t)
        clips.append(ic)
        t += per

    # (opsiyonel) müzik miks
    try:
        from moviepy.audio.AudioClip import CompositeAudioClip
        tracks = [voice_audio.volumex(1.0)]
        if music_path and os.path.exists(music_path):
            music = AudioFileClip(str(music_path)).volumex(0.15).set_duration(merged.duration)
            tracks.append(music)
        final_audio = CompositeAudioClip(tracks)
        comp = CompositeVideoClip(clips, size=(W, H)).set_audio(final_audio)
    except Exception as e:
        print("Müzik miks uyarı:", e, file=sys.stderr)
        comp = CompositeVideoClip(clips, size=(W, H)).set_audio(voice_audio)

    comp.set_fps(FPS).write_videofile(str(out_path), fps=FPS, codec="libx264", audio_codec="aac", threads=4)
    return out_path

# ------------------------- Routes ---------------------------
@app.route('/', methods=['GET'])
def index():
    files = sorted([p.name for p in OUT_DIR.glob('video_*.mp4')])
    files_html = ''.join([f"<a href='/static/output/{f}' target='_blank'>{f}</a>" for f in files[::-1]])
    keys_hint = '' if (os.getenv('ELEVENLABS_API_KEY') and os.getenv('PEXELS_API_KEY')) else '<p class="warn">Uyarı: ELEVENLABS_API_KEY ve PEXELS_API_KEY tanımlı değil.</p>'

    html = """<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Video Creator</title>
<style>
body{font-family:system-ui;max-width:860px;margin:40px auto;padding:0 16px}
.btn{padding:12px 18px;border:0;border-radius:10px;background:#111;color:#fff;font-weight:600;cursor:pointer}
.btn[disabled]{opacity:.5;cursor:not-allowed}
.card{border:1px solid #eee;border-radius:14px;padding:18px;margin:12px 0;box-shadow:0 8px 30px rgba(0,0,0,.04)}
.log{white-space:pre-wrap;background:#0a0a0a;color:#c7f5c4;border-radius:10px;padding:12px;font-size:13px;min-height:120px}
.files a{display:block;margin:6px 0}
.warn{color:#b65a00;font-weight:600}
</style>
</head>
<body>
<h1>AI Video Creator — Turkish Shorts</h1>
<p>Tek tuşla: <b>Metin → Ses → Görsel → Müzik → Video</b> (9:16)</p>

<div class="card">
  <p><b>1) Ortam değişkenleri</b></p>
  <ul>
    <li>OPENAI_API_KEY (opsiyonel)</li>
    <li>ELEVENLABS_API_KEY (zorunlu)</li>
    <li>PEXELS_API_KEY (zorunlu)</li>
  </ul>
""" + keys_hint + """
</div>

<div class="card">
  <p><b>2) Video üret</b></p>
  <button class="btn" id="gen">Yeni Video Oluştur</button>
  <div id="log" class="log" style="margin-top:12px">Hazır.</div>
</div>

<div class="card">
  <p><b>3) Çıktılar</b></p>
  <div class="files" id="files">""" + files_html + """</div>
</div>

<script>
const gen=document.getElementById('gen');
const log=document.getElementById('log');
const filesDiv=document.getElementById('files');
gen.onclick=async()=>{
  gen.disabled=true;
  log.textContent="⏳ Üretim başlıyor...";
  try{
    const r=await fetch('/generate',{method:'POST'});
    const data=await r.json();
    if(!data.ok){
      log.textContent="❌ Hata: "+data.error;
    }else{
      log.textContent="✅ Bitti: "+data.file;
      const a=document.createElement('a');
      a.href=data.file;
      a.textContent="İndir: "+data.file.split('/').pop();
      filesDiv.prepend(a);
    }
  }catch(e){
    log.textContent="❌ Beklenmeyen hata: "+e;
  }finally{
    gen.disabled=false;
  }
};
</script>
</body>
</html>"""
    return html

@app.route('/generate', methods=['POST'])
def generate():
    if not ELEVENLABS_API_KEY or not PEXELS_API_KEY:
        return {'ok': False, 'error': 'Lütfen ELEVENLABS_API_KEY ve PEXELS_API_KEY değişkenlerini tanımlayın.'}, 400
    try:
        story = generate_story()
        voice = elevenlabs_tts(story)
        bgs = fetch_pexels_videos(count=3)
        # (Opsiyonel) MUSIC_DIR içine mp3 eklerseniz otomatik miksleyebilirsiniz.
        music = None
        # mp3s = list(MUSIC_DIR.glob("*.mp3"))
        # if mp3s: music = random.choice(mp3s)
        out = assemble_video(story, voice, bgs, music_path=music)
        return {'ok': True, 'file': f"/static/output/{Path(out).name}"}
    except Exception as e:
        return {'ok': False, 'error': str(e)}, 500

# ------------------------- Main -----------------------------
if __name__ == '__main__':
    port = int(os.getenv('PORT', '8080'))
    app.run(host='0.0.0.0', port=port)

