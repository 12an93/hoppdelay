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
- Eget Wi-Fi (hotspot) – kräver inget nät i simhallen.

## Hårdvara

- Mini-PC med Intel-grafik (testad: ASUS VivoMini VM65) och HDMI till TV.
- USB-kamera med MJPEG 1080p30 (testad: Jabra PanaCast 20, med Intelligent Zoom avstängt i Jabra Direct).
- **PanaCast 20 måste anslutas med en USB 2-kabel.** På USB 3 ger den MJPEG bara i 4K.
- Valfritt: tangentbord eller presentationsklickare med USB-dongel.

## Installation (Debian 13, utan skrivbord)

BIOS: *Restore AC Power Loss → Power On* så att datorn startar när strömmen kopplas in.

```
sudo sed -i 's/^GRUB_TIMEOUT=.*/GRUB_TIMEOUT=0/' /etc/default/grub && sudo update-grub && echo 'FSCKFIX=yes' | sudo tee -a /etc/default/rcS
sudo apt install -y avahi-daemon openssl iw dnsmasq-base v4l-utils python3-evdev python3-gi gir1.2-gstreamer-1.0 gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly intel-media-va-driver
sudo install -m755 hoppdelay.py /usr/local/bin/hoppdelay.py && sudo install -m644 hoppdelay.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now hoppdelay
```

Har datorn ett skrivbord installerat: `sudo systemctl set-default multi-user.target`.

Den första USB-kameran används. Vill du välja en annan, sätt `HOPPDELAY_CAM` i `hoppdelay.service`
(lista kamerorna med `ls /dev/v4l/by-id/`).

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
| R | Rotera 90° |

## Mätning

Markera **Upphopp** och **Vatten** (och gärna **Topp** och **Öppning**) medan du stegar bild för bild,
och välj svikthöjd. Siffrorna räknas från tyngdpunktens kastbana, med antagandet att den faller ungefär
svikthöjden från upphopp till vattenkontakt. Med 30 fps är en bild ±0,03 s, vilket ger ungefär
±0,1–0,2 m på höjden. En 60 fps-kamera halverar det.

## Licens

[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/deed.sv): fri att använda, dela och
ändra i icke-kommersiellt syfte – klubbar, tränare och skolor är välkomna. Ange upphovspersonen och dela
ändringar under samma licens. Det är inte tillåtet att sälja den eller använda den i en kommersiell produkt.
