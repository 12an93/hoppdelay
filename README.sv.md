# Hoppdelay

*[English](README.md)*

Fördröjd videouppspelning för simhoppsträning. En kamera filmar svikten, TV:n visar bilden med
fördröjning (standard 30 s) så att hopparen hinner upp ur bassängen och se sitt hopp. Allt styrs
från ett tangentbord/presentationsklickare eller från en iPhone via en egen webbsida.

## Funktioner

- Fördröjd bild på HDMI, skalas automatiskt till skärmen (max 1080p), liggande eller stående.
- Hela passet spelas in på disken (upp till 40 % av disken, äldsta minuten raderas först).
- Spola, pausa, ändra delay och rotera – från tangentbord eller telefon.
- Repris på telefonen medan TV:n fortsätter: slowmotion (½×, ¼×, ⅒×) och bild för bild.
- Mät ett hopp genom att markera upphopp, topp, öppning och vattenkontakt: flygtid, högsta punkt
  över upphoppet och höjd över vattnet vid öppningen – räknat från svikthöjden, ingen spårning behövs.
- Ritverktyg på reprisen: linje, vinkel och kalibrering mot en känd längd för avstånd i meter.
- Jämför två sparade hopp sida vid sida eller överlagda, synkade på upphoppet, bild för bild.
- Spara klipp som MP4 med hoppare, hopp och höjd i namnet; filtrera, byt namn, radera och spara direkt
  i Bilder på iPhone.
- En eller två kameror (t.ex. 1 m och 3 m, eller från sidan och framifrån). TV-layouter: kamera 1,
  kamera 2, sida vid sida, bild i bild.
- Visa en repris i ett hörn av TV:n medan den fördröjda bilden fortsätter.
- Automatisk lista över hopp: rita en zon i luften framför svikten; varje hopp genom den listas
  (och kan sparas automatiskt).
- StroMotion-bild: hopparen inklistrad var N:e bild längs banan, i en enda bild.
- Automatisk analys av ett hopp: bana, högsta punkt, avstånd ut från svikten, antal varv och varv per sekund.
- Valfri AI-kroppsanalys (RTMPose): skelett på reprisen, höft- och knävinklar, när hopparen öppnar,
  varv räknade på bålen (fungerar även i kort position) och kroppslinjen vid vattnet.
- Valfri AI-feedback skriven från mätvärdena, via valfri OpenAI-kompatibel tjänst (t.ex. Ollama).
- Eget Wi-Fi (hotspot) – kräver inget nät i simhallen.

## Hårdvara

- Mini-PC med Intel-grafik (testad: ASUS VivoMini VM65) och HDMI till TV.
- USB-kamera med MJPEG upp till 1080p i 30–60 fps (testad: Jabra PanaCast 20, med Intelligent Zoom avstängt i
  Jabra Direct). Största storleken används, sedan högsta bildfrekvens upp till 60 fps
  (kameror som bara erbjuder 90 eller 120 fps, som vissa med global slutare, körs i det). Autofokus låses 10 s efter
  start, så den inte jagar när en hoppare passerar.
- **PanaCast 20 måste anslutas med en USB 2-kabel.** På USB 3 ger den MJPEG bara i 4K.
- Valfritt: en andra USB-kamera, och tangentbord eller presentationsklickare med USB-dongel.

## Installation (Debian 13, utan skrivbord)

BIOS: *Restore AC Power Loss → Power On* så att datorn startar när strömmen kopplas in.

```
sudo sed -i 's/^GRUB_TIMEOUT=.*/GRUB_TIMEOUT=0/' /etc/default/grub && sudo update-grub && echo 'FSCKFIX=yes' | sudo tee -a /etc/default/rcS
sudo apt install -y avahi-daemon openssl iw dnsmasq-base v4l-utils python3-evdev python3-gi gir1.2-gstreamer-1.0 gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly intel-media-va-driver python3-opencv python3-numpy
sudo install -m755 hoppdelay.py /usr/local/bin/hoppdelay.py && sudo install -m644 hoppdelay.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now hoppdelay
```

Har datorn ett skrivbord installerat: `sudo systemctl set-default multi-user.target`.

Alla USB-kameror med MJPEG upp till 1080p i minst 30 fps används (högst två). Vill du välja, sätt
`HOPPDELAY_CAMS=/dev/v4l/by-id/...,/dev/v4l/by-id/...` i `hoppdelay.service` (lista kamerorna med
`ls /dev/v4l/by-id/`). Kameror utan MJPEG upp till 1080p hoppas över med en rad i loggen.

Logg: `sudo journalctl -u hoppdelay -n 30 --no-pager`

### Hotspot

Välj ett eget lösenord (minst 8 tecken) i stället för `change-me-123`:

```
sudo nmcli dev wifi hotspot ifname wlp3s0 con-name Hoppdelay ssid Hoppdelay password change-me-123 && sudo nmcli con modify Hoppdelay connection.autoconnect yes connection.autoconnect-priority 100
```

Wi-Fi-kortet kan heta något annat än `wlp3s0` – kolla med `nmcli dev`. Hemma når du datorn via nätverkskabel, eller genom att ansluta till Hoppdelay-nätet.

### iPhone

1. Anslut till Wi-Fi *Hoppdelay* och öppna `http://hoppdelay.local` (eller `http://10.42.0.1`).
2. Följ rutan överst: hämta certifikatet, installera profilen, slå på *Hoppdelay-CA* under
   Inställningar → Allmänt → Om → Certifikatinställningar.
3. Öppna `https://hoppdelay.local` och lägg den på hemskärmen.

HTTPS behövs för att iPhone ska kunna spara klipp direkt i Bilder. Certifikaten skapas automatiskt i
`/etc/hoppdelay` vid första start.

## Tangentbord

| Tangent | Funktion |
|---|---|
| ↑ / ↓ | Delay +5 s / −5 s |
| ← / PageUp | Bakåt 5 s |
| → / PageDown | Framåt 5 s |
| Mellanslag / B | Paus / spela |
| Enter / Esc | Tillbaka till vanlig delay |
| R | Rotera kamera 1 90° |
| L | Nästa TV-layout (två kameror) |

## Mätning

Markera **Upphopp** och **Vatten** (och gärna **Topp** och **Öppning**) medan du stegar bild för bild,
och välj svikthöjd. Siffrorna räknas från tyngdpunktens kastbana, med antagandet att den faller ungefär
svikthöjden från upphopp till vattenkontakt. Med 30 fps är en bild ±0,033 s, vilket ger ungefär
±0,1–0,2 m på höjden; med 60 fps är den ±0,017 s och felet halveras. Sidan visar kamerans verkliga
bildfrekvens – sjunker den under vad kameran ska ge är det oftast för lite ljus.

## Automatiska hopp

Ta en repris från kamera 1, välj verktyget **Zon** och tryck två hörn av en ruta i luften precis framför
svikten, där bara hopparen passerar (inte svikten, vattnet eller läktaren). Varje hopp genom rutan dyker
upp under **Hopp idag** med några sekunder före och efter; tryck **Visa** för att öppna det som repris.
Med **Spara automatiskt** på sparas varje hopp också som klipp.

## Automatisk analys

**Analysera bana och rotation** hittar hopparen mot bakgrunden i varje bild (kameran får inte flyttas)
och ritar banan på reprisen. Markera **Upphopp** och **Vatten** först och kalibrera en gång för meter.
Rotationen räknas från kroppens längdaxel, så den fungerar bra i rak och pik men sämre i kort position –
sidan säger till när den är osäker.

## AI-kroppsanalys (valfritt)

**Analysera kroppen (AI)** kör [RTMPose](https://github.com/open-mmlab/mmpose/tree/main/projects/rtmpose)
(Apache-2.0) på processorn, några sekunder per hopp. Hopparen klipps ut och vrids upprätt innan modellen
ser henne, eftersom pose-modeller är tränade på stående människor. Installera en gång, med internet:

```
sudo apt install -y python3-pip && sudo pip install --break-system-packages onnxruntime
curl -fsSLo /tmp/rtm.zip https://download.openmmlab.com/mmpose/v1/projects/rtmposev1/onnx_sdk/rtmpose-m_simcc-body7_pt-body7_420e-256x192-e48f03d0_20230504.zip && sudo mkdir -p /var/lib/hoppdelay/models && sudo python3 -c "import zipfile; z=zipfile.ZipFile('/tmp/rtm.zip'); open('/var/lib/hoppdelay/models/rtmpose-m.onnx','wb').write(z.read([n for n in z.namelist() if n.endswith('end2end.onnx')][0]))" && sudo systemctl restart hoppdelay
```

Knappen dyker upp när modellen finns på plats. Sidan visar tätaste höftvinkel, mest böjda knä, när höften
överstiger 150° (ett tryck sätter det som markeringen **Öppning**), varv räknade på bålen och kroppslinjen
vid vattnet. Det är en uppskattning: stäm av mot videon.

## AI-feedback (valfritt)

Ange en OpenAI-kompatibel chattjänst i `hoppdelay.service`, till exempel Ollama på en annan dator:

```
Environment=HOPPDELAY_LLM_URL=http://192.168.1.10:11434/v1/chat/completions
Environment=HOPPDELAY_LLM_MODEL=qwen3:8b
```

(`HOPPDELAY_LLM_KEY` för tjänster som kräver nyckel.) Bara mätvärdena skickas, aldrig video. Datorn måste
nå tjänsten, vilket oftast betyder att det inte fungerar i simhallen.

## Licens

Skapad av Jesper ([@Tolvers2026](https://github.com/12an93)). Videor och bilder från Hoppdelay har en svag
@Tolvers2026-märkning i nedre högra hörnet.


[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/deed.sv): fri att använda, dela och
ändra i icke-kommersiellt syfte – klubbar, tränare och skolor är välkomna. Ange upphovspersonen och dela
ändringar under samma licens. Det är inte tillåtet att sälja den eller använda den i en kommersiell produkt.
