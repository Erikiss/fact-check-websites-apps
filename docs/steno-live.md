# Steno-Live: Kurzschrift live lesen und Fehler markieren

`factcheck-steno` beobachtet einen Bildschirmbereich – zum Beispiel das WebEx-Whiteboard oder die geteilte Folie –
und schreibt dir direkt darüber, was dort in Kurzschrift (DEK) steht: die Übertragung jedes Wortes, was
verdächtig oder falsch aussieht (rot, mit einem Kreis an der genauen Stelle), wie es richtig wäre und welche
anderen Lesarten in Frage kommen. Kein Screenshot, kein Kopieren in einen Chat: sobald die Lehrkraft einen Strich
fertig hat, geht das Bild nach etwa einer halben Sekunde ans Modell, und die ersten Markierungen erscheinen, während
die Antwort noch läuft.

Zwei Stufen arbeiten zusammen:

| Stufe | Modell (Standard) | Wann | Wofür |
|---|---|---|---|
| ⚡ schnell | Claude Sonnet 5.5, effort `low`, adaptive Thinking | jedes neue Bild | live mitlesen, schnelle aber deutlich genauere Erstlesung |
| 🔍 genau | Claude Opus 5.5, effort `high`, adaptive Thinking | einmal pro Bild, das 1,5 s still steht | gründliche Zeichen-für-Zeichen-Prüfung; ersetzt das Overlay |

Das Ergebnis muss nicht perfekt sein – es soll schnell sein und dir zeigen, wo du hinschauen musst. Darum zerlegt das
Modell jedes Zeichen erst visuell (`strokes`) und gibt zu jeder Lesung eine Konfidenz und bis zu drei Alternativen an.

## Schnellstart (Windows)

1. Abhängigkeiten installieren: `uv sync`
2. `.env` aus `.env.example` anlegen und API-Schlüssel eintragen:
   - Für Claude-Modelle (Standard: `claude-sonnet-5-5`, `claude-opus-5-5`): `ANTHROPIC_API_KEY=`
   - Für Gemini-Modelle (z. B. `gemini-2.5-flash`, `gemini-2.5-pro`, `neon`, `argon`): `GEMINI_API_KEY=`
   - Beide Anbieter können auch gemischt werden (z. B. schnell = `gemini-2.5-flash`, tief = `claude-opus-5-5`).
3. Starten: `uv run factcheck-steno`
4. Den Bereich mit der Steno-Schrift aufziehen – nur Tafel/Folie, **nicht** das Kamerabild der Lehrkraft (Bewegung
   dort würde ständig neue Analysen auslösen). Der Bereich wird gemerkt.

Ohne Schlüssel und ohne Kosten ausprobieren (die Oberfläche funktioniert, die „Lesungen“ sind Platzhalter):
`uv run factcheck-steno --fake`

## Bedienung

**Steuerleiste** (oben, mit der Maus verschiebbar): `⬚ Bereich` neu wählen · `⏸ Pause`/`▶ Live` ·
`⚡ Jetzt` (sofort schnell analysieren) · `🔍 Genau` (sofort gründlich prüfen) · `👁 Overlay` ein/aus ·
`☰ Details` · `✕` beenden. Das Hinweisfeld wird jedem Bild mitgegeben, z. B. „Finde den Fehler“,
„Eilschrift“ oder „das Wort heißt Haus – was ist falsch?“. Die Statuszeile zeigt Stufe, Dauer und die heutigen
Kosten.

**Overlay** (durchklickbar, du kannst WebEx normal bedienen):

- grün = gelesenes Wort, sieht korrekt aus · rot = falsch oder verdächtig · gestrichelt = unsicher (< 50 %)
- Beschriftung: `✗ geschrieben → gemeint · Konfidenz`, darunter was falsch ist, `richtig: …` und `oder: …`
- roter Kreis = die genaue Stelle (z. B. die zu runde Verbindung)
- Banner über dem Bereich: die Lösung in einem Satz, plus Stufe, Modell und Dauer
- verblasst = das Bild hat sich seit dieser Analyse geändert, eine neue läuft

**Details-Fenster**: vollständige Übertragung, visuelle Zeichenzerlegung (`strokes`), alle Fehler mit Erklärung und
Alternativen, schnelle und genaue Antwort untereinander (neuestes Bild zuerst).

**Hotkeys** – unter Windows systemweit, also auch wenn WebEx den Fokus hat (in `.env` änderbar):

| Taste | Aktion |
|---|---|
| Strg+Alt+S | jetzt schnell analysieren |
| Strg+Alt+D | jetzt genau prüfen |
| Strg+Alt+R | Bereich neu wählen |
| Strg+Alt+O | Overlay ein/aus |
| Strg+Alt+P | Pause/Live |

**Einzelbild** – zum Ausprobieren mit eigenen Screenshots, oder um das Wissen/den Prompt zu verbessern:

```
uv run factcheck-steno image screenshot.png                      # schnelle Stufe
uv run factcheck-steno image screenshot.png --deep --hint "Finde den Fehler"
uv run factcheck-steno image screenshot.png --fake               # ohne Kosten
```

Die Markierungen erscheinen im Terminal, sobald sie gestreamt werden, und neben dem Bild landet eine annotierte
Kopie (`screenshot.steno-fast.png`, `screenshot.steno-deep.png`). Am Ende stehen die gemessenen Zeiten (erste
Ausgabe, erste Markierung, gesamt) und die Kosten – so siehst du, wie schnell es bei dir wirklich ist.

## Wie es funktioniert – und warum es schnell ist

1. **Änderungserkennung lokal** (4× pro Sekunde, kostet nichts): Aus jedem Bild wird ein kleines Graustufen-Abbild
   gemacht. Gesendet wird nur, wenn sich das Bild seit der letzten Analyse geändert hat und ~0,5 s still steht
   (Strich fertig) – oder spätestens alle 2 s, solange noch gezeichnet wird. Bei Dauerbewegung verdoppelt sich der
   Abstand bis auf 16 s. Eine leere Tafel wird nie gesendet.
2. **Bild passend skalieren**: Das Bild wird vorab genau so skaliert, wie die API es erwartet (standardmäßig
   hochauflösend bis `2576` px / `4784` Bild-Tokens; sehr kleine Ausschnitte werden sanft bis `896` px vergrößert).
   Dadurch zeigen die Koordinaten des Modells 1:1 auf das Bild, und feine DEK-Striche bleiben scharf.
3. **Streaming**: Das Modell antwortet in einem festen JSON-Schema (structured outputs); jedes Wort wird gezeichnet,
   sobald sein JSON-Objekt vollständig ist – nicht erst am Ende der Antwort.
4. **Prompt-Caching**: System-Prompt und DEK-Wissen sind bei jedem Bild identisch und werden gecacht; pro Bild kommen
   nur das Bild, der Hinweis und (ab dem zweiten bestätigten Deep-Frame) ein kurzer Themenkontext dazu.
5. **Neuestes Bild gewinnt**: Läuft schon eine Analyse, wartet höchstens ein Bild – ein neueres ersetzt es. Ändert
   sich das Bild, wird eine laufende genaue Prüfung des alten Bildes abgebrochen.
6. **Overlay unsichtbar für Aufnahmen** (Windows 10 2004+, `SetWindowDisplayAffinity`): Das Modell liest nie die
   eigenen Markierungen mit. Teilst du selbst deinen Bildschirm, sehen die anderen das Overlay ebenfalls nicht.

## Genauer machen

- **Wissen**: `src/factcheck/steno/knowledge/dek.md` enthält die DEK-Regeln nach Wiener Urkunde 1968 (Vokalandeutung
  inkl. `ä`, `ö`, `ü`, Mitlautformen, Kürzel, An-/Auslautzeichen, Unterscheidungszeichen und typische Verwechslungen).
  Am meisten bringt zusätzlich eine eigene Datei mit dem Material deines Kurses (eure Kürzelliste, typische Aufgaben):
  `STENO_KNOWLEDGE_FILE=pfad/zu/kurs.md`.
- **Hinweisfeld** nutzen – das gesuchte Wort, das Thema der Stunde oder die Aufgabenstellung hilft enorm.
- **Bereich eng ziehen**: mehr Pixel pro Zeichen. Wenn es noch schneller/günstiger sein soll:
  `STENO_MAX_IMAGE_EDGE=1568`, `STENO_MAX_IMAGE_TOKENS=1568` oder `STENO_FAST_THINKING=off` / `STENO_FAST_MODEL=claude-haiku-5-5`.
- **Noch gründlicher**: `STENO_FAST_EFFORT=medium` oder `STENO_DEEP_EFFORT=xhigh` (spürbar langsamer).
- Grenzen: Feinheiten wie Verstärkung oder eine halbe Stufe sind in einem komprimierten Videobild schwer zu sehen,
  und das Modell ist kein geprüfter Steno-Korrektor. Deshalb Konfidenz, Zeichenzerlegung und Alternativen – bitte selbst nachsehen.

## Kosten

Grobe Werte pro Analyse (hochauflösendes Bild ~2.000–3.500 Bild-Tokens, gecachter System-Prompt):

| Stufe | ca. pro Analyse |
|---|---|
| ⚡ Sonnet 5.5 (effort `low`, adaptive Thinking) | 0,01–0,03 $ |
| 🔍 Opus 5.5 (effort `high`, adaptive Thinking) | 0,05–0,15 $ |
| ⚡ Gemini 2.5 Flash (schnell, günstig) | 0,0005–0,002 $ |
| 🔍 Gemini 2.5 Pro (gründlich) | 0,005–0,02 $ |
| *(optional sparsam: Haiku 5.5, 1568 Tokens)* | *0,0003–0,0006 $* |

Jede Analyse wird mit ihren echten Token-Zahlen in `budget_ledger.jsonl` eingetragen (Zweck `steno-fast` /
`steno-deep`). Bremsen:

- `STENO_DAILY_BUDGET_USD` (Standard 20 $) – Tageslimit für alle Steno-Analysen;
- `STENO_DEEP_BUDGET_SHARE` (Standard 0,8) – die genaue Stufe hört bei 80 % auf, der Rest bleibt der schnellen;
- `TOTAL_BUDGET_USD` – projektweiter harter Deckel über alle Tage (Standard 100 $);
- `STENO_DEEP_AUTO=false` – genaue Prüfung nur noch per Knopf/Hotkey; `STENO_DEEP_MODEL=` (leer) schaltet sie ab.

## Einstellungen (`.env`)

| Variable | Standard | Bedeutung |
|---|---|---|
| `STENO_FAST_MODEL` | `claude-sonnet-5-5` | Modell der schnellen Stufe |
| `STENO_FAST_EFFORT` / `STENO_FAST_THINKING` | `low` / `adaptive` | Gründlichkeit der schnellen Stufe |
| `STENO_FAST_MAX_TOKENS` | `4096` | Ausgabe- + Denkbudget der schnellen Stufe |
| `STENO_DEEP_MODEL` | `claude-opus-5-5` | Modell der genauen Stufe (leer = aus) |
| `STENO_DEEP_EFFORT` / `STENO_DEEP_THINKING` | `high` / `adaptive` | Gründlichkeit der genauen Stufe |
| `STENO_DEEP_MAX_TOKENS` | `16000` | Ausgabe- + Denkbudget der genauen Stufe |
| `STENO_DEEP_AUTO` | `true` | genaue Stufe automatisch bei stillem Bild |
| `STENO_DEEP_AFTER_S` | `1.5` | so lange muss das Bild still stehen |
| `STENO_FPS` | `4` | Bilder pro Sekunde für die Änderungserkennung |
| `STENO_SETTLE_S` / `STENO_MAX_WAIT_S` / `STENO_MIN_INTERVAL_S` | `0.45` / `2.0` / `0.8` | Auslöse-Zeiten |
| `STENO_CHANGE_THRESHOLD` | `0.004` | Anteil geänderter Pixel, ab dem ein Bild „neu“ ist |
| `STENO_MAX_IMAGE_EDGE` / `STENO_MAX_IMAGE_TOKENS` | `2576` / `4784` | Bildgröße fürs Modell (hochauflösend) |
| `STENO_MIN_IMAGE_EDGE` | `896` | sanftes Hochskalieren (max. 2×) sehr kleiner Ausschnitte |
| `STENO_FALLBACKS` | `true` | serverseitiger Refusal-Fallback (nur Opus/Sonnet) |
| `STENO_DAILY_BUDGET_USD` / `STENO_DEEP_BUDGET_SHARE` | `20.0` / `0.8` | Kostenbremsen |
| `STENO_HINT` | leer | Vorbelegung des Hinweisfelds |
| `STENO_KNOWLEDGE_FILE` | leer | eigene Wissensdatei statt `knowledge/dek.md` |
| `STENO_SAVE_FRAMES` | `false` | jedes gesendete Bild unter `logs/steno-frames/` speichern |
| `STENO_FAKE` | `false` | Platzhalter-Analysen ohne API (0 $) |
| `STENO_HOTKEY_*` | `ctrl+alt+s/d/r/o/p` | Hotkeys (`NOW`, `DEEP`, `REGION`, `OVERLAY`, `PAUSE`) |

## Datenschutz

Die Bildausschnitte gehen an die Anthropic API. Antworten und Metadaten jeder Anfrage stehen in
`logs/llm-steno-*.jsonl` (nicht im Git), die Bilder selbst nur mit `STENO_SAVE_FRAMES=true`. Prüfe, ob die
Kursunterlagen an einen externen Dienst gegeben werden dürfen.

## Andere Systeme und Tablet

- **macOS/Linux** laufen, aber das Overlay lässt sich dort nicht aus Bildschirmaufnahmen ausschließen: Es wird für
  den Moment der Aufnahme kurz ausgeblendet (Status „Overlay wird kurz ausgeblendet“), und Hotkeys gehen nur, wenn
  ein Fenster der App den Fokus hat. Unter Linux nur X11 (Wayland erlaubt keine Bildschirmaufnahme ohne Portal).
- **Tablet (Android)**: eigene App mit derselben Analyse, siehe [`steno-android.md`](steno-android.md). Eine reine
  Browser-Variante geht auf Tablets nicht: Chrome für Android (und Safari auf dem iPad) kann keine Bildschirme
  aufnehmen.

## Fehlersuche

| Meldung / Problem | Lösung |
|---|---|
| „Keine Zugangsdaten …“ | `ANTHROPIC_API_KEY` in `.env` eintragen (oder `--fake`) |
| Live pausiert sich nach Fehlern | nach 3 fehlgeschlagenen Analysen in Folge; Ursache steht in der Statuszeile |
| „… Tagesbudget … erreicht“ | `STENO_DAILY_BUDGET_USD` erhöhen oder `STENO_DEEP_AUTO=false` |
| Overlay sitzt versetzt | Bereich neu wählen; bei mehreren Monitoren mit verschiedener Skalierung bitte melden |
| Hotkey reagiert nicht | ist von einem anderen Programm belegt (siehe Log) – anderen in `.env` setzen |
| Zu viele Analysen | Kamera oder animierte Elemente im Bereich? Bereich enger ziehen |

## Code

`src/factcheck/steno/`: `frames.py` (wann senden) · `imaging.py` (Bild vorbereiten) · `vision.py` (Modellaufruf,
Fake, Budget) · `jsonstream.py` (Streaming-Parser) · `engine.py` (Hintergrund-Ausführung) · `prompts.py` +
`knowledge/dek.md` · `models.py` · `layout.py`/`render.py` (Beschriftung) · `ui/` (Qt-Fenster, Windows-Hotkeys) ·
`cli.py`. Tests: `tests/test_steno_*.py` (ohne Netz, 0 $).
