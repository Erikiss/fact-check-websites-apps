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
| ⚡ schnell | Claude Haiku 5.5, effort `low`, ohne Thinking | jedes neue Bild | live mitlesen, erste Vermutung |
| 🔍 genau | Claude Opus 5.5, effort `low` | einmal pro Bild, das 2,5 s still steht | zweite, gründlichere Meinung; ersetzt das Overlay |

Das Ergebnis muss nicht perfekt sein – es soll schnell sein und dir zeigen, wo du hinschauen musst. Darum gibt das
Modell zu jeder Lesung eine Konfidenz und bis zu drei Alternativen an.

## Schnellstart (Windows)

1. Abhängigkeiten installieren: `uv sync`
2. `.env` aus `.env.example` anlegen und `ANTHROPIC_API_KEY=` eintragen. Der Live-Modus ruft die Messages API
   direkt auf und streamt die Antwort. Der Abo-Weg des Faktencheckers (Agent SDK) startet pro Anfrage einen eigenen
   Prozess und wäre für „live“ zu langsam.
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

**Details-Fenster**: vollständige Übertragung, alle Fehler mit Erklärung und Alternativen, schnelle und genaue
Antwort untereinander (neuestes Bild zuerst).

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
2. **Bild passend skalieren**: Das Bild wird vorab genau so verkleinert, wie die API es sonst selbst täte. Dadurch
   zeigen die Koordinaten des Modells 1:1 auf das Bild, und das Overlay sitzt an der richtigen Stelle.
3. **Streaming**: Das Modell antwortet in einem festen JSON-Schema (structured outputs); jedes Wort wird gezeichnet,
   sobald sein JSON-Objekt vollständig ist – nicht erst am Ende der Antwort.
4. **Prompt-Caching**: System-Prompt und DEK-Wissen sind bei jedem Bild identisch und werden gecacht; pro Bild kommen
   nur das Bild und der Hinweis dazu.
5. **Neuestes Bild gewinnt**: Läuft schon eine Analyse, wartet höchstens ein Bild – ein neueres ersetzt es. Ändert
   sich das Bild, wird eine laufende genaue Prüfung des alten Bildes abgebrochen.
6. **Overlay unsichtbar für Aufnahmen** (Windows 10 2004+, `SetWindowDisplayAffinity`): Das Modell liest nie die
   eigenen Markierungen mit. Teilst du selbst deinen Bildschirm, sehen die anderen das Overlay ebenfalls nicht.

## Genauer machen

- **Wissen**: `src/factcheck/steno/knowledge/dek.md` enthält die DEK-Grundregeln (Vokalandeutung durch enge/weite
  Verbindung, Hoch-/Tiefstellung, Verstärkung), Zeichengrößen, Kürzel und einen Katalog typischer Fehler. Am meisten
  bringt eine eigene Datei mit dem Material deines Kurses (eure Kürzelliste, typische Aufgaben):
  `STENO_KNOWLEDGE_FILE=pfad/zu/kurs.md`.
- **Hinweisfeld** nutzen – das gesuchte Wort oder die Aufgabenstellung hilft enorm.
- **Bereich eng ziehen**: mehr Pixel pro Zeichen. Bei sehr feinen Details: `STENO_MAX_IMAGE_EDGE=2576` und
  `STENO_MAX_IMAGE_TOKENS=4784` (die 5.x-Modelle können hochauflösende Bilder; kostet etwas mehr Zeit und Geld).
- **Schnelle Stufe gründlicher**: `STENO_FAST_EFFORT=medium` oder `STENO_FAST_THINKING=adaptive` (langsamer).
- Grenzen: Feinheiten wie Verstärkung oder eine halbe Stufe sind in einem komprimierten Videobild schwer zu sehen,
  und das Modell ist kein geprüfter Steno-Korrektor. Deshalb Konfidenz und Alternativen – bitte selbst nachsehen.

## Kosten

Grobe Werte pro Analyse (Bild ~1.200 Bild-Tokens, gecachter System-Prompt):

| Stufe | ca. pro Analyse |
|---|---|
| ⚡ Haiku 5.5 | 0,0003–0,0006 $ |
| 🔍 Opus 5.5 (effort low) | 0,02–0,05 $ |

Jede Analyse wird mit ihren echten Token-Zahlen in `budget_ledger.jsonl` eingetragen (Zweck `steno-fast` /
`steno-deep`). Bremsen:

- `STENO_DAILY_BUDGET_USD` (Standard 2 $) – Tageslimit für alle Steno-Analysen;
- `STENO_DEEP_BUDGET_SHARE` (Standard 0,8) – die genaue Stufe hört bei 80 % auf, der Rest bleibt der schnellen;
- `TOTAL_BUDGET_USD` – projektweiter harter Deckel (Standard 10 $; für regelmäßige Nutzung erhöhen);
- `STENO_DEEP_AUTO=false` – genaue Prüfung nur noch per Knopf/Hotkey; `STENO_DEEP_MODEL=` (leer) schaltet sie ab.

## Einstellungen (`.env`)

| Variable | Standard | Bedeutung |
|---|---|---|
| `STENO_FAST_MODEL` | `claude-haiku-5-5` | Modell der schnellen Stufe |
| `STENO_FAST_EFFORT` / `STENO_FAST_THINKING` | `low` / `off` | Gründlichkeit der schnellen Stufe |
| `STENO_DEEP_MODEL` | `claude-opus-5-5` | Modell der genauen Stufe (leer = aus) |
| `STENO_DEEP_EFFORT` / `STENO_DEEP_THINKING` | `low` / `adaptive` | Gründlichkeit der genauen Stufe |
| `STENO_DEEP_AUTO` | `true` | genaue Stufe automatisch bei stillem Bild |
| `STENO_DEEP_AFTER_S` | `2.5` | so lange muss das Bild still stehen |
| `STENO_FPS` | `4` | Bilder pro Sekunde für die Änderungserkennung |
| `STENO_SETTLE_S` / `STENO_MAX_WAIT_S` / `STENO_MIN_INTERVAL_S` | `0.45` / `2.0` / `0.8` | Auslöse-Zeiten |
| `STENO_CHANGE_THRESHOLD` | `0.004` | Anteil geänderter Pixel, ab dem ein Bild „neu“ ist |
| `STENO_MAX_IMAGE_EDGE` / `STENO_MAX_IMAGE_TOKENS` | `1568` / `1568` | Bildgröße fürs Modell |
| `STENO_FALLBACKS` | `true` | serverseitiger Refusal-Fallback (nur Opus/Sonnet) |
| `STENO_DAILY_BUDGET_USD` / `STENO_DEEP_BUDGET_SHARE` | `2.0` / `0.8` | Kostenbremsen |
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
- **Tablet**: noch nicht. Der naheliegende nächste Schritt wäre eine Browser-Variante (Bildschirmfreigabe +
  Zeichenfläche); die Analyse-Teile (`imaging`, `frames`, `vision`, `models`) sind dafür bereits Qt-frei.

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
