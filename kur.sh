#!/usr/bin/env bash
# =============================================================================
# Oyun İzleyici — kurulum / güncelleme (CachyOS / Arch tabanlı her PC için)
#
#   bash kur.sh                 -> kur veya güncelle (PC adı = hostname)
#   bash kur.sh "Laptop"        -> listelerde görünecek PC adını belirle (ilk kurulumda)
#   Güncelleme: aynı komut; betik kendini de günceller.
#
# Ne yapar:
#   1. Programı ~/.local/bin/oyun-izleyici olarak kurar (eskisini yedekler)
#   2. Kayıt deposunu ~/.local/share/oyun-izleyici/depo altına klonlar (GitHub senkronu)
#   3. MangoHud'u her oyunda otomatik FPS kaydı yazacak şekilde ayarlar (HUD görünümüne dokunmaz)
#   4. Uygulama menüsüne ve otomatik başlatmaya ekler, programı yeniden başlatır
# =============================================================================
# Tüm betik bir fonksiyonun içinde: bash dosyayı çalıştırmadan önce tamamen okur, böylece
# betik çalışırken depodan kendi yeni sürümü çekilse bile karışıklık olmaz.
main() {
set -euo pipefail

REPO_SLUG="${OI_KAYIT_DEPOSU:-}"   # isteğe bağlı özel kayıt deposu: OI_KAYIT_DEPOSU=sahip/depo bash kur.sh
REPO_HTTPS="https://github.com/${REPO_SLUG}.git"
REPO_SSH="git@github.com:${REPO_SLUG}.git"
DATA="$HOME/.local/share/oyun-izleyici"
DEPO="$DATA/depo"
CONF_DIR="$HOME/.config/oyun-izleyici"
DEPLOY_KEY="$HOME/.ssh/oyun_kayitlari_ed25519"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PC_ADI="${1:-}"
TS="$(date +%Y%m%d-%H%M%S)"

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mHATA:\033[0m %s\n' "$*" >&2; exit 1; }

# --- 1. Bağımlılıklar --------------------------------------------------------
say "Bağımlılıklar kontrol ediliyor"
missing=()
command -v python3 >/dev/null || missing+=(python)
python3 -c "import PySide6" 2>/dev/null || missing+=(pyside6)
python3 -c "import psutil" 2>/dev/null || missing+=(python-psutil)
command -v mangohud >/dev/null || missing+=(mangohud)
if command -v pacman >/dev/null; then pacman -Q lib32-mangohud >/dev/null 2>&1 || missing+=(lib32-mangohud); fi
command -v git >/dev/null || missing+=(git)
command -v lspci >/dev/null || missing+=(pciutils)
command -v notify-send >/dev/null || missing+=(libnotify)
opt_missing=()
if [[ -z "${HYPRLAND_INSTANCE_SIGNATURE:-}" ]]; then
    if [[ -n "${WAYLAND_DISPLAY:-}" && "${XDG_CURRENT_DESKTOP,,}" == *kde* ]]; then
        command -v kdotool >/dev/null || opt_missing+=(kdotool)
    else
        command -v xdotool >/dev/null || opt_missing+=(xdotool)
    fi
fi
if ((${#missing[@]} || ${#opt_missing[@]})); then
    warn "Eksik bağımlılıklar tespit edildi:"
    ((${#missing[@]})) && echo "   • Gerekli : ${missing[*]}"
    ((${#opt_missing[@]})) && echo "   • Önerilen (pencere/web takibi): ${opt_missing[*]}"
    echo
    ans=""
    if [[ -t 0 ]]; then
        read -rp "Bağımlılıklar için kurulum komutları gösterilip kurulsun mu? [E/h]: " ans
    fi
    pac_list=("${missing[@]}")
    aur_list=()
    for op in "${opt_missing[@]}"; do
        if [[ "$op" == "kdotool" ]]; then aur_list+=(kdotool); else pac_list+=("$op"); fi
    done
    echo
    echo "Kurulum komutları:"
    ((${#pac_list[@]})) && echo "   sudo pacman -S --needed ${pac_list[*]}"
    if ((${#aur_list[@]})); then
        if command -v paru >/dev/null; then
            echo "   paru -S --needed ${aur_list[*]}"
        else
            echo "   yay -S --needed ${aur_list[*]}"
        fi
    fi
    echo
    if [[ "${ans,,}" =~ ^(e|evet|y|yes|)$ && -t 0 ]]; then
        if ((${#pac_list[@]})) && command -v pacman >/dev/null; then
            sudo pacman -S --needed "${pac_list[@]}" || true
        fi
        if ((${#aur_list[@]})); then
            if command -v paru >/dev/null; then
                paru -S --needed "${aur_list[@]}" || true
            elif command -v yay >/dev/null; then
                yay -S --needed "${aur_list[@]}" || true
            fi
        fi
    elif ((${#missing[@]})); then
        die "Gerekli paketleri yukarıdaki komutla kurup betiği tekrar çalıştır."
    fi
fi

[[ -f "$SRC/program/oyun-izleyici" ]] || die "program/oyun-izleyici bulunamadı ($SRC)"

# --- 2. Kayıt deposu (GitHub) ------------------------------------------------
mkdir -p "$DATA" "$CONF_DIR"
if [[ ! -d "$DEPO/.git" && -z "$REPO_SLUG" ]]; then
    say "GitHub senkronu yok (isteğe bağlı: OI_KAYIT_DEPOSU=sahip/depo ile tekrar çalıştır)"
elif [[ ! -d "$DEPO/.git" ]]; then
    say "Kayıt deposu klonlanıyor: $REPO_SLUG"
    if command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then
        gh repo clone "$REPO_SLUG" "$DEPO" -- -q
        gh auth setup-git >/dev/null 2>&1 || true
    elif [[ "$(cd "$SRC" && git remote get-url origin 2>/dev/null || true)" == *"$REPO_SLUG"* ]] && \
         git -C "$SRC" ls-remote -q origin >/dev/null 2>&1; then
        # Betik zaten çalışan bir klondan çalıştırılıyor: aynı erişimi kullan
        git clone -q "$(git -C "$SRC" remote get-url origin)" "$DEPO"
        ssh_cmd="$(git -C "$SRC" config core.sshCommand || true)"
        [[ -n "$ssh_cmd" ]] && git -C "$DEPO" config core.sshCommand "$ssh_cmd"
    else
        # gh yoksa: bu depoya özel bir SSH anahtarı (deploy key) kullan
        if [[ ! -f "$DEPLOY_KEY" ]]; then
            ssh-keygen -q -t ed25519 -N "" -C "oyun-izleyici@$(hostname)" -f "$DEPLOY_KEY"
        fi
        ssh_cmd="ssh -i $DEPLOY_KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
        if ! GIT_SSH_COMMAND="$ssh_cmd" git clone -q "$REPO_SSH" "$DEPO" 2>/dev/null; then
            warn "Bu PC'nin depoya erişimi yok. Aşağıdaki anahtarı depoya YAZMA izinli 'deploy key' olarak ekle:"
            echo
            cat "$DEPLOY_KEY.pub"
            echo
            echo "  gh yüklü başka bir PC'den:  gh repo deploy-key add <anahtar.pub> --repo $REPO_SLUG --allow-write --title $(hostname)"
            echo "  ya da web: https://github.com/$REPO_SLUG/settings/keys"
            echo "Ekledikten sonra bu betiği tekrar çalıştır."
            exit 2
        fi
        git -C "$DEPO" config core.sshCommand "$ssh_cmd"
    fi
else
    say "Kayıt deposu mevcut, güncelleniyor"
    before="$(sha1sum "$DEPO/kur.sh" 2>/dev/null || true)"
    git -C "$DEPO" pull -q --rebase --autostash || warn "Depo güncellenemedi (internet?) — devam ediliyor"
    if [[ "$SRC" == "$DEPO" && -z "${OI_REEXEC:-}" && "$(sha1sum "$DEPO/kur.sh")" != "$before" ]]; then
        say "Kurulum betiği güncellendi, yeni sürümle devam ediliyor"
        OI_REEXEC=1 exec bash "$DEPO/kur.sh" "$@"
    fi
fi

# --- 3. Program --------------------------------------------------------------
say "Program kuruluyor: ~/.local/bin/oyun-izleyici"
mkdir -p "$HOME/.local/bin"
if [[ -f "$HOME/.local/bin/oyun-izleyici" ]] && ! cmp -s "$SRC/program/oyun-izleyici" "$HOME/.local/bin/oyun-izleyici"; then
    cp -p "$HOME/.local/bin/oyun-izleyici" "$HOME/.local/bin/oyun-izleyici.yedek-$TS"
fi
install -m 755 "$SRC/program/oyun-izleyici" "$HOME/.local/bin/oyun-izleyici"
if [[ -f "$SRC/program/pc_izleyici.py" ]]; then install -m 644 "$SRC/program/pc_izleyici.py" "$HOME/.local/bin/pc_izleyici.py"; fi

# PC adı (sadece ilk kurulumda; sonra makine.conf'tan değiştirilebilir)
if [[ -n "$PC_ADI" && ! -f "$CONF_DIR/makine.conf" ]]; then
    printf '[makine]\nad = %s\n' "$PC_ADI" > "$CONF_DIR/makine.conf"
fi

# --- 4. MangoHud: her oyunda otomatik kayıt -----------------------------------
say "MangoHud otomatik kayıt ayarlanıyor"
MH_DIR="$HOME/.config/MangoHud"; MH="$MH_DIR/MangoHud.conf"
mkdir -p "$MH_DIR"
if [[ -f "$MH" ]]; then
    python3 - "$MH" "$TS" <<'PY'
import sys, re, shutil
path, ts = sys.argv[1], sys.argv[2]
raw = open(path, encoding="utf-8").read()
lines = raw.splitlines()
had_no_display = any(re.match(r"^\s*no_display\s*(=.*)?$", l) for l in lines)
drop = re.compile(r"^\s*(no_display|output_folder|autostart_log|log_duration|log_interval|blacklist)\s*(=.*)?$")
out = [l for l in lines if not drop.match(l) and "Oyun İzleyici" not in l]
if had_no_display:
    # no_display açıkken MangoHud kayıt başlatmıyor: HUD'u şeffaf yaparak gizle
    out = [l for l in out if not re.match(r"^\s*(legacy_layout|alpha|background_alpha)\s*=", l)]
    out += ["legacy_layout=0", "alpha=0", "background_alpha=0"]
out += ["# --- Oyun İzleyici: otomatik FPS kaydı (kur.sh) ---",
        "blacklist=kwin_wayland,kwin_wayland_wrapper,plasmashell,Xwayland,krunner,ksplashqml,systemsettings,dolphin,konsole",
        "output_folder=/tmp/mangohud_live", "log_interval=100", "autostart_log=1"]
new_raw = "\n".join(out) + "\n"
if new_raw != raw:
    shutil.copy2(path, f"{path}.yedek-{ts}")
    open(path, "w", encoding="utf-8").write(new_raw)
PY
else
    cat > "$MH" <<'EOF'
# Oyun İzleyici: HUD görünmez ama FPS kaydı canlı yazılır (Shift_R+F12 ile gizle/göster)
legacy_layout=0
alpha=0
background_alpha=0
hud_no_margin
toggle_hud=Shift_R+F12
# --- Oyun İzleyici: otomatik FPS kaydı (kur.sh) ---
blacklist=kwin_wayland,kwin_wayland_wrapper,plasmashell,Xwayland,krunner,ksplashqml,systemsettings,dolphin,konsole
output_folder=/tmp/mangohud_live
log_interval=100
autostart_log=1
EOF
fi
mkdir -p "$HOME/.config/environment.d"
echo "MANGOHUD=1" > "$HOME/.config/environment.d/mangohud.conf"
# Açık oturuma da hemen uygula (environment.d normalde sadece oturum açılırken okunur)
MH_SESSION="var"
if [[ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ]]; then
    dbus-update-activation-environment --systemd MANGOHUD=1 >/dev/null 2>&1 || \
        systemctl --user set-environment MANGOHUD=1 >/dev/null 2>&1 || true
fi
shell_pid="$(pgrep -u "$USER" -x plasmashell | head -1 || true)"
[[ -z "$shell_pid" ]] && shell_pid="$(pgrep -u "$USER" -x gnome-shell | head -1 || true)"
if [[ -n "$shell_pid" ]] && ! tr '\0' '\n' < "/proc/$shell_pid/environ" 2>/dev/null | grep -qx 'MANGOHUD=1'; then
    MH_SESSION="yok"
fi
mkdir -p "$HOME/.config/user-tmpfiles.d"
echo 'd /tmp/mangohud_live 0755 - - - -' > "$HOME/.config/user-tmpfiles.d/mangohud-live.conf"
systemctl --user enable systemd-tmpfiles-setup.service >/dev/null 2>&1 || true
mkdir -p /tmp/mangohud_live

# --- 5. Menü + otomatik başlatma ----------------------------------------------
say "Uygulama menüsü ve otomatik başlatma"
mkdir -p "$HOME/.local/share/applications" "$HOME/.config/autostart"
write_desktop() {  # $1 = hedef dosya, $2 = Exec satırı, $3 = ek satırlar
    cat > "$1" <<EOF
[Desktop Entry]
Type=Application
Name=Oyun İzleyici
Comment=Canlı FPS / donanım izleyici, oyun geçmişi ve PC karşılaştırma
Exec=$2
Icon=utilities-system-monitor
Terminal=false
Categories=System;Monitor;Game;
$3
EOF
}
write_desktop "$HOME/.local/share/applications/oyun-izleyici.desktop" "$HOME/.local/bin/oyun-izleyici" ""
write_desktop "$HOME/.config/autostart/oyun-izleyici.desktop" \
    "sh -c \"sleep 5; exec $HOME/.local/bin/oyun-izleyici\"" "X-KDE-autostart-phase=2"

# --- 6. Yeniden başlat ---------------------------------------------------------
if [[ -z "${WAYLAND_DISPLAY:-}${DISPLAY:-}" && -n "$shell_pid" && -r "/proc/$shell_pid/environ" ]]; then
    while IFS='=' read -r k v; do
        case "$k" in
            DISPLAY|WAYLAND_DISPLAY|XAUTHORITY|XDG_RUNTIME_DIR|DBUS_SESSION_BUS_ADDRESS)
                export "$k=$v"
                ;;
        esac
    done < <(tr '\0' '\n' < "/proc/$shell_pid/environ" 2>/dev/null)
fi
if [[ -n "${WAYLAND_DISPLAY:-}${DISPLAY:-}" ]]; then
    pkill -f "$HOME/.local/bin/oyun-izleyici" 2>/dev/null || true
    sleep 1
    if command -v systemd-run >/dev/null 2>&1 && systemctl --user is-system-running >/dev/null 2>&1; then
        systemd-run --user --collect \
            --setenv=WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-}" \
            --setenv=DISPLAY="${DISPLAY:-:0}" \
            --setenv=XAUTHORITY="${XAUTHORITY:-}" \
            --setenv=XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}" \
            --setenv=DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$(id -u)/bus}" \
            "$HOME/.local/bin/oyun-izleyici" >/dev/null 2>&1 || \
            setsid -f "$HOME/.local/bin/oyun-izleyici" >/dev/null 2>&1 < /dev/null
    else
        setsid -f "$HOME/.local/bin/oyun-izleyici" >/dev/null 2>&1 < /dev/null
    fi
    say "Oyun İzleyici başlatıldı"
else
    say "Grafik oturum bulunamadı: bir sonraki girişte otomatik başlayacak"
fi

echo
say "Kurulum tamam."
echo "   • Ayarlar: $CONF_DIR/makine.conf  (ad, mod=otomatik|panel|tepsi)"
echo "   • Kayıtlar: $DATA/oyunlar.db  (+ yedekler/, GitHub: $REPO_SLUG)"
echo "   • MangoHud değişikliği yeni açılan oyunlarda geçerli."
if [[ "$MH_SESSION" == "yok" ]]; then
    echo
    warn "Bu oturum MANGOHUD=1 ayarından ÖNCE açılmış: oyunlarda FPS kaydı için bir kez oturumu kapatıp aç."
    if pgrep -x steam >/dev/null; then
        steam_pid="$(pgrep -x steam | head -1)"
        if ! tr '\0' '\n' < "/proc/$steam_pid/environ" 2>/dev/null | grep -qx 'MANGOHUD=1'; then
            echo "   Hemen denemek için: Steam'den tamamen çık, sonra terminalde:  MANGOHUD=1 steam &>/dev/null & disown"
        fi
    else
        echo "   Ya da oturumu kapatmadan: Steam'i terminalden aç:  MANGOHUD=1 steam &>/dev/null & disown"
    fi
fi
}
main "$@"
exit $?
