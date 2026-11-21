# Oyun İzleyici

Oyun oynarken canlı **FPS, 1% / 0.1% low, frametime, GPU / CPU / RAM** değerlerini gösteren bir panel; her oyun oturumunu kaydedip **oyun başına toplam süre ve ortalamaları**, ayrıca **iki PC'nin karşılaştırmasını** sunar.

- **Linux** (CachyOS / Arch tabanlı, AMD · NVIDIA · Intel): FPS için [MangoHud](https://github.com/flightlessmango/MangoHud) kayıtları, sensörler için `/sys` ve `/proc`.
- **Windows 10/11** (NVIDIA / Intel / AMD): FPS için Intel'in [PresentMon](https://github.com/GameTechDev/PresentMon)'u, sensörler için `psutil`, `nvidia-smi` ve WMI.

> **Durum:** Linux sürümü günlük kullanımda. Windows sürümü yeni: mantığı sahte PresentMon ile uçtan uca test edildi ama gerçek Windows'ta henüz az denendi. Sorun olursa `tani.bat` çıktısıyla issue aç.

## Özellikler
- Dikey HUD paneli (taşınır, boyutlanır, saydamlaşır, tema rengi ya da KDE "Edna" temasıyla uyumlu hazır tema); üstte saat ve tarih şeridi; tek ekranda sadece tepsi simgesi.
- Oyun algılama ve **oturum kaydı** (SQLite, günlük yedekler, bozulmaya karşı onarım).
- Oyun Geçmişi penceresi: oyun başına toplam, tek tek oturumlar, not ekleme, CSV dışa aktarma.
- **Gerçek takılmaları gösteren** low değerleri: menü / yükleme ekranı patlamaları ayıklanır, kısa takılmalar korunur.
- İsteğe bağlı özel GitHub deposuyla **PC'ler arası senkron ve karşılaştırma** (laptop ↔ masaüstü gibi).
- Linux: oyun içi gösterge (MangoHud) modları: gizli / tek satır / detaylı.

## Kurulum

### Linux (CachyOS / Arch)
```bash
sudo pacman -S --needed python pyside6 mangohud lib32-mangohud git pciutils libnotify
git clone https://github.com/cdokuro/oyun-izleyici.git && cd oyun-izleyici
bash kur.sh "PC Adı"
```
Güncellemek için `git pull && bash kur.sh`.

### Windows
1. Depoyu indir (**Code → Download ZIP**) ya da `git clone`.
2. `kur.bat` dosyasına çift tıkla (ya da `powershell -ExecutionPolicy Bypass -File kur.ps1 -PcAdi "Laptop"`).
3. Betik Python'u (yoksa winget ile), PySide6 / psutil'i ve **sabit sürüm + SHA256 doğrulamalı** PresentMon'u kurar; Başlat menüsü ve otomatik başlatma kısayolu ekler.
4. PresentMon'un yönetici olmadan çalışması için kullanıcıyı **Performance Log Users** grubuna ekler (bir kez UAC sorar). Sonrasında **Windows oturumunu kapatıp aç**.
5. Sorun olursa `%LOCALAPPDATA%\oyun-izleyici\tani.bat` sensörleri ve PresentMon'u dener.

## Ayarlar (`makine.conf`)
Linux: `~/.config/oyun-izleyici/makine.conf` · Windows: `%APPDATA%\oyun-izleyici\makine.conf`

| Anahtar | Anlamı |
|---|---|
| `ad` | Listelerde görünen PC adı |
| `mod` | `otomatik` (2+ ekran = panel, tek ekran = tepsi) · `panel` · `tepsi` |
| `baslik` | Oyun yokken panelin altındaki yazı |
| `engelle` | Windows: oyun sayılmayacak programlar (`foo.exe, bar.exe`) |
| `gpu_adi`, `cpu_adi`, `ram_etiketi` | Otomatik algılanan adları ezmek için |
| `senkron` | `acik` / `kapali` |

## Veriler
Oturumlar yerelde SQLite dosyasında tutulur (Linux: `~/.local/share/oyun-izleyici/oyunlar.db`, Windows: `%LOCALAPPDATA%\oyun-izleyici\oyunlar.db`) ve her gün yedeklenir. Hiçbir veri internete gönderilmez; GitHub senkronunu sen kurarsan **kendi özel depona** yazılır.

## PC'ler arası karşılaştırma (isteğe bağlı)
Kendi **özel** GitHub deponu aç, her PC'de kurulumu depo adıyla çalıştır:
```bash
OI_KAYIT_DEPOSU=sahip/depo bash kur.sh            # Linux
powershell -File kur.ps1 -KayitDeposu sahip/depo   # Windows
```
Betik bir SSH anahtarı üretir; ekrana yazdığı genel anahtarı depoya *yazma izinli deploy key* olarak eklemen yeterli. Her PC yalnızca kendi `makineler/<kimlik>/` klasörüne yazar.

## Bilinen sınırlar
- Windows'ta işlemci gücü (W) ve fan devri okunamaz; işlemci sıcaklığı ACPI ısıl bölgesinden gelir (her dizüstüde yok).
- Windows'ta oyun içi gösterge yok (panel ayrı penceredir; kenarlıksız pencereli oyunlarda görünür).
- Windows'ta AMD / Intel ekran kartlarının sıcaklık / güç değerleri sürücüsüz okunamaz (NVIDIA tam desteklenir).
- Linux'ta birden fazla ekran kartı (iGPU + dGPU) varsa MangoHud kaydı `pci_dev` ile dGPU'ya sabitlenir (AMD için otomatik).

## Lisans
Henüz belirlenmedi.
