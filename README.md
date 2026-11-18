# Historic Shorts Agent 🏛️✏️

Vollautomatische Produktion von ~30-Sekunden-Geschichts-Shorts im Strichmännchen-Stil mit Voice-Over.
Du gibst ein Thema (oder gar nichts – dann nimmt der Agent das nächste aus `topics.md`), Claude
recherchiert, schreibt das Skript, erzeugt die Stimme, animiert, prüft die Bilder und rendert die MP4.

```
Thema → Recherche (Web) → Skript-JSON → Voice-Over (ElevenLabs/OpenAI) → Strichmännchen-Render → QA → MP4
```

## Im Browser starten (ohne lokale Installation)
1. Diesen Ordner als GitHub-Repo hochladen (z. B. `historic-shorts-agent`).
2. In **Claude Code im Web** (claude.ai/code) das Repo öffnen.
3. In den Umgebungs-Einstellungen hinterlegen:
   - `ELEVENLABS_API_KEY` (empfohlen, liefert exakte Wort-Timings für die Captions) **oder** `OPENAI_API_KEY`
   - Netzwerkzugriff für `api.elevenlabs.io` bzw. `api.openai.com` erlauben
4. Prompt: **„Mach das nächste Video.“** – oder **„Mach ein Short über den Great Emu War.“**
5. Fertig liegt unter `out/<slug>/<slug>.mp4` (+ `meta.json` mit Titel/Beschreibung/Tags, `captions.srt`).

**Voll automatisch:** In Claude Code einen geplanten Task anlegen, z. B. täglich 2×
„Mach das nächste Video aus topics.md“ → bei 60 Videos/Monat einfach `topics.md` gefüllt halten.

## Anpassen
| Was | Wo |
|---|---|
| Kanal-Handle im Abspann | `config.json` → `handle` |
| Stimme / Tempo | `config.json` → `tts` (ElevenLabs-Voice-ID, `speed`) |
| Musik | `.mp3` in `assets/music/` legen (lizenzfrei!) |
| Stil-, Humor- & Längenregeln | `CLAUDE.md` |
| Neue Props/Objekte | `pipeline/render.py` |

## Manuell testen
```bash
python3 pipeline/make_short.py scripts/romulus-remus.json --preview   # Kontaktbogen in 1 s
python3 pipeline/make_short.py scripts/romulus-remus.json             # volles Video (~15 s Renderzeit)
```
Ohne API-Key entsteht ein stummer Entwurf mit geschätztem Timing – gut zum Layout-Testen.

Verbrauch Voice-Over: ca. 450–550 Zeichen pro Video → 60 Videos ≈ 30.000 Zeichen/Monat.
