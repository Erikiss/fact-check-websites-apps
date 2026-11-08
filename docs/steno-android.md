# Steno-Live auf dem Android-Tablet

Dieselbe Live-Analyse wie am PC (`factcheck-steno`), direkt auf dem Tablet: Steno-Live nimmt den Bildschirm auf,
während WebEx läuft, liest die Kurzschrift im gewählten Bereich und zeichnet Übertragung, Fehler und Alternativen
über die WebEx-App. Prompt, DEK-Wissen und Antwortformat sind exakt dieselben wie am PC (gemeinsame Dateien in
`src/factcheck/steno/knowledge/`), ebenso die beiden Stufen: ⚡ Claude Haiku 5.5 für jedes neue Bild, 🔍 Claude Opus 5.5
für eine zweite Meinung, sobald das Bild still steht.

Für Android 10 und neuer (z. B. Galaxy Tab S6 Lite).

## Installieren

1. Auf dem Tablet im Browser öffnen:
   `https://github.com/Erikiss/fact-check-websites-apps/releases/download/steno-live-latest/steno-live.apk`
   Den Link gibt es, sobald der GitHub-Workflow „Android (Steno-Live)“ einmal gelaufen ist (unter *Actions* im Repo).
   Ist das Repo privat, vorher im Browser bei GitHub anmelden.
2. Die heruntergeladene Datei antippen → *Installieren*. Beim ersten Mal fragt Android, ob der Browser „unbekannte
   Apps installieren“ darf → erlauben. Play Protect warnt eventuell vor einem unbekannten Entwickler → *Trotzdem
   installieren*.
3. Updates: die neue APK einfach drüber installieren. Alle Builds sind mit demselben Schlüssel signiert, deine
   Einstellungen bleiben erhalten.

## Einrichten (einmal)

1. App **Steno-Live** öffnen.
2. **Claude-API-Schlüssel** eintragen und *Einstellungen speichern*. Der Schlüssel wird mit einem Schlüssel aus dem
   Android-Keystore verschlüsselt gespeichert und nur an `api.anthropic.com` gesendet.
3. **„Über anderen Apps einblenden“ erlauben** (der Knopf in der App führt direkt zur Einstellung).
4. Optional: Hinweis ans Modell („Finde den Fehler“), **eigenes Kursmaterial** (z. B. eure Kürzelliste – wird dem
   DEK-Wissen angehängt), Tages- und Gesamtlimit (Standard 20 $ / 100 $).

Ohne Schlüssel und ohne Kosten ausprobieren: *Fake-Modus* einschalten – die Oberfläche läuft, die „Lesungen“ sind
Platzhalter.

## Benutzen

1. In der App **▶ Starten**. Android fragt nach der Bildschirmaufnahme → *Jetzt starten* (bei jedem Start; das
   verlangt Android so). Unter Android 14+ wird automatisch der ganze Bildschirm aufgenommen.
2. Die App tritt in den Hintergrund. **WebEx öffnen** – am besten im Querformat, die Tafel möglichst groß.
3. Beim ersten Mal den **Bereich aufziehen**: nur Tafel/Folie, nicht das Kamerabild (Bewegung dort löst ständig neue
   Analysen aus). Der Bereich wird pro Ausrichtung gemerkt.
4. Die **schwebende Leiste** (am „⠿ Steno“ verschiebbar):

   | Knopf | Wirkung |
   |---|---|
   | ⏸ Pause / ▶ Live | automatische Analyse anhalten / fortsetzen |
   | ⚡ Jetzt | sofort schnell analysieren (geht auch pausiert) |
   | 🔍 Genau | sofort gründlich mit dem stärkeren Modell prüfen |
   | ⬚ Bereich | Bereich neu wählen |
   | 👁 An/Aus | Overlay ein-/ausblenden |
   | ☰ | Details: Übertragung, alle Fehler mit Erklärung, Alternativen |
   | ⚙ | Einstellungen öffnen |
   | ✕ | beenden |

5. **Overlay**: grün = gelesenes Wort, rot = vermuteter Fehler (Kreis = genaue Stelle), gestrichelt = unsicher.
   Darüber ein Banner mit der Lösung in einem Satz. Verblasst = das Bild hat sich geändert, eine neue Analyse läuft.
6. **Beenden**: ✕ in der Leiste, *Stopp* in der Benachrichtigung oder *■ Stoppen* in der App.

## Unterschiede zur PC-Version

- Android kann das eigene Overlay nicht aus der Bildschirmaufnahme herausnehmen. Darum wird es für jedes Bild, das ans
  Modell geht, etwa 0,2 Sekunden ausgeblendet (kurzes Flackern) – so liest das Modell nie die eigenen Markierungen.
- Das Overlay ist leicht durchscheinend (80 %): Ab Android 12 gehen Berührungen sonst nicht durch fremde Overlays
  hindurch, und WebEx wäre unter dem Overlay nicht mehr bedienbar.
- Keine Tastenkürzel – alles über die schwebende Leiste.
- Eigenes Kostenbuch auf dem Tablet (unabhängig vom PC). Die Kosten von heute stehen in der Leiste und in der App.

## Wenn etwas nicht klappt

Die App ist automatisch gebaut und geprüft (Unit-Tests, Android-Lint), aber **noch nicht auf einem echten Gerät
getestet**. Wenn etwas hakt: in der App unter *Protokoll* → *Kopieren* und die Zeilen schicken.

| Problem | Lösung |
|---|---|
| Leiste/Overlay erscheinen nicht | Berechtigung „Über anderen Apps einblenden“ prüfen |
| Markierungen fehlen, Tafel in Screenshots schwarz | WebEx blockiert Bildschirmaufnahmen – dann geht es auf dem Tablet nicht |
| „API-Schlüssel ungültig“ | Schlüssel neu eintragen und speichern |
| „… Tagesbudget … erreicht“ | Limit in der App erhöhen |
| Live pausiert sich selbst | nach 3 fehlgeschlagenen Analysen in Folge; Grund steht in der Leiste und im Protokoll |
| Bereich passt nach dem Drehen nicht | ⬚ Bereich neu wählen |
| Nach dem Sperren des Bildschirms ist alles weg | Android beendet die Bildschirmaufnahme beim Sperren – in der App neu starten |

## Selbst bauen

```
cd android
./gradlew :core:test :app:assembleDebug     # JDK 17+, Android SDK (Plattform 36)
```

Oder den Ordner `android/` in Android Studio öffnen. Die APK liegt danach unter
`android/app/build/outputs/apk/debug/app-debug.apk`.

## Technik in Kürze

- `android/core` (reines Kotlin, auf der JVM getestet): Bildgrößen-Regel der API, Änderungserkennung, Streaming-JSON-
  Parser, Markierungen, Beschriftung, Kostenbuch, Ablaufsteuerung (schnell/genau, neuestes Bild gewinnt) und der
  Claude-Aufruf über das offizielle Java-SDK `com.anthropic:anthropic-java` (Streaming, strukturierte Ausgabe,
  gecachter System-Prompt, Refusal-Fallback für Opus). Die Tests prüfen den echten SDK-Client gegen einen lokalen
  Test-Server, der wie die API streamt.
- `android/app`: Vordergrunddienst mit MediaProjection → VirtualDisplay → ImageReader (4 Vergleichsbilder pro
  Sekunde), Overlay-Fenster, Bereichsauswahl, schwebende Leiste, Einstellungen.
- Datenschutz: Bildausschnitte gehen an die Anthropic API; Protokoll und Kostenbuch bleiben auf dem Tablet.
