# Hoppdelay

Fördröjd videouppspelning för simhoppsträning. En kamera filmar svikten, TV:n visar bilden med
fördröjning (standard 30 s) så att hopparen hinner upp ur bassängen och se sitt hopp. Allt styrs
från ett tangentbord/presentationsklickare eller från en iPhone via en egen webbsida.

## Funktioner

- Fördröjd bild på HDMI, skalas automatiskt till skärmen (max 1080p), liggande eller stående.
- Hela passet spelas in på disken (upp till 40 % av disken, äldsta minuten raderas först).
- Spola, pausa, ändra delay och rotera – från tangentbord eller telefon.
- Repris på telefonen medan TV:n fortsätter: slowmotion (½×, ¼×, ⅒×) och bild för bild.
- Spara klipp som MP4, byt namn, radera, och spara direkt i Bilder på iPhone.
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

Kameran anges i `CAM` överst i `hoppdelay.py` och i `hoppdelay.service`. Hitta sökvägen med `ls /dev/v4l/by-id/`.

Logg: `sudo journalctl -u hoppdelay -n 30 --no-pager`

### Hotspot

```
sudo nmcli dev wifi hotspot ifname wlp3s0 con-name Hoppdelay ssid Hoppdelay password svikt3meter && sudo nmcli con modify Hoppdelay connection.autoconnect yes connection.autoconnect-priority 100
```

Byt lösenord efter behov. Hemma når du datorn via nätverkskabel, eller genom att ansluta till Hoppdelay-nätet.

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
