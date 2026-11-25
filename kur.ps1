<#
.SYNOPSIS
  Oyun Izleyici - Windows kurulum / guncelleme betigi.

.DESCRIPTION
  1. Python 3.10+ bulur (yoksa winget ile kurar) ve %LOCALAPPDATA%\oyun-izleyici\venv icine PySide6 + psutil kurar
  2. Programi %LOCALAPPDATA%\oyun-izleyici\app\oyun_izleyici.pyw olarak kopyalar (eskisini yedekler)
  3. PresentMon'u (Intel, MIT) sabit surum + SHA256 dogrulamasiyla indirir
  4. Kullaniciyi "Performance Log Users" grubuna ekler (PresentMon'un yonetici olmadan calismasi icin; bir kez UAC sorar)
  5. Baslat menusu + Windows baslangic kisayolu olusturur, programi baslatir

.PARAMETER PcAdi
  Gecmis / karsilastirma listelerinde gorunecek PC adi (ilk kurulumda).

.PARAMETER KayitDeposu
  Istege bagli: PC'ler arasi karsilastirma icin ozel GitHub deposu ("sahip/depo"). git + SSH anahtari kurar.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File kur.ps1 -PcAdi "Laptop"
#>
param(
    [string]$PcAdi = "",
    [string]$KayitDeposu = ""
)

$ErrorActionPreference = "Stop"
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

# ---- ayarlar -----------------------------------------------------------------
$RepoSlug     = "cdokuro/oyun-izleyici"
$PresentMonUrl    = "https://github.com/GameTechDev/PresentMon/releases/download/v2.6.0/PresentMon-2.6.0-x64.exe"
$PresentMonSha256 = "b2a706bc6ad475749e3b7e3409263aa1e6906d45bdcf993f6dbc0f660188f1af"

$Data   = Join-Path $env:LOCALAPPDATA "oyun-izleyici"
$Roam   = Join-Path $env:APPDATA "oyun-izleyici"
$Venv   = Join-Path $Data "venv"
$AppDir = Join-Path $Data "app"
$BinDir = Join-Path $Data "bin"
$AppFile   = Join-Path $AppDir "oyun_izleyici.pyw"
$PmExe     = Join-Path $BinDir "PresentMon.exe"
$VenvPy    = Join-Path $Venv "Scripts\python.exe"
$VenvPyW   = Join-Path $Venv "Scripts\pythonw.exe"
$Here = $PSScriptRoot
if (-not $Here) { $Here = (Get-Location).Path }
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"

function Say($m)  { Write-Host "==> $m" -ForegroundColor Green }
function Warn($m) { Write-Host "!!  $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "HATA: $m" -ForegroundColor Red; exit 1 }

# ---- 1. Python ---------------------------------------------------------------
function Get-PyVersion([string]$exe, [string[]]$pre) {
    try {
        $out = & $exe @pre -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) { return ([string]$out).Trim() }
    } catch {}
    return $null
}

function Find-Python {
    $cands = @(
        @{ exe = "py";      pre = @("-3.13") }, @{ exe = "py"; pre = @("-3.12") },
        @{ exe = "py";      pre = @("-3.11") }, @{ exe = "py"; pre = @("-3.10") },
        @{ exe = "python";  pre = @() },        @{ exe = "python3"; pre = @() }
    )
    foreach ($c in $cands) {
        if (-not (Get-Command $c.exe -ErrorAction SilentlyContinue)) { continue }
        $v = Get-PyVersion $c.exe $c.pre
        if (-not $v) { continue }
        $parts = $v.Split(".")
        $major = [int]$parts[0]; $minor = [int]$parts[1]
        if ($major -eq 3 -and $minor -ge 10 -and $minor -le 14) { return $c }
    }
    return $null
}

Say "Python araniyor"
$py = Find-Python
if (-not $py) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Say "Python 3.12 winget ile kuruluyor (kullanici duzeyinde)"
        winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements | Out-Host
        $guess = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
        if (Test-Path $guess) { $py = @{ exe = $guess; pre = @() } } else { $py = Find-Python }
    }
}
if (-not $py) {
    Fail "Python 3.10+ bulunamadi. https://www.python.org/downloads/ adresinden kurup (Add python.exe to PATH) bu betigi tekrar calistir."
}

# ---- 2. Sanal ortam + paketler -------------------------------------------------
New-Item -ItemType Directory -Force -Path $Data, $Roam, $AppDir, $BinDir | Out-Null
if (-not (Test-Path $VenvPy)) {
    Say "Sanal ortam olusturuluyor: $Venv"
    $pyExe = $py.exe
    $pyPre = $py.pre
    & $pyExe @pyPre -m venv $Venv
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $VenvPy)) { Fail "Sanal ortam olusturulamadi." }
}
Say "PySide6 ve psutil kuruluyor / guncelleniyor (ilk seferde birkac dakika surebilir)"
& $VenvPy -m pip install --disable-pip-version-check --quiet --upgrade pip
& $VenvPy -m pip install --disable-pip-version-check --quiet --upgrade PySide6-Essentials psutil
if ($LASTEXITCODE -ne 0) { Fail "pip paketleri kurulamadi (internet baglantisini kontrol et)." }

# ---- 3. Program ----------------------------------------------------------------
$localProg = Join-Path $Here "program\oyun-izleyici"
Say "Program kuruluyor: $AppFile"
if (Test-Path $AppFile) {
    Copy-Item $AppFile "$AppFile.yedek-$Stamp"
    # en fazla 5 yedek tut
    Get-ChildItem $AppDir -Filter "oyun_izleyici.pyw.yedek-*" | Sort-Object Name -Descending | Select-Object -Skip 5 | Remove-Item -Force -ErrorAction SilentlyContinue
}
$tmpProg = Join-Path $env:TEMP "oyun-izleyici-$Stamp.py"
if (Test-Path $localProg) {
    Copy-Item $localProg $tmpProg -Force
} else {
    Invoke-WebRequest -UseBasicParsing -Uri "https://raw.githubusercontent.com/$RepoSlug/main/program/oyun-izleyici" -OutFile $tmpProg
}
if ((Get-Item $tmpProg).Length -lt 50000) { Fail "Program dosyasi eksik/bozuk gorunuyor." }

# calisan eski kopyayi kapat (dosya kilidi olmasin)
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*oyun_izleyici.pyw*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 700
Move-Item $tmpProg $AppFile -Force
$DaemonFile = Join-Path $AppDir "pc_izleyici.py"
$localDaemon = Join-Path $Here "program\pc_izleyici.py"
if (Test-Path $localDaemon) {
    Copy-Item $localDaemon $DaemonFile -Force
} else {
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "https://raw.githubusercontent.com/$RepoSlug/main/program/pc_izleyici.py" -OutFile $DaemonFile
    } catch { Warn "pc_izleyici.py indirilemedi (normal modda calisacak): $_" }
}

# ---- 4. PresentMon -------------------------------------------------------------
$needPm = $true
if (Test-Path $PmExe) {
    if ((Get-FileHash $PmExe -Algorithm SHA256).Hash.ToLower() -eq $PresentMonSha256) { $needPm = $false }
}
if ($needPm) {
    Say "PresentMon 2.6.0 indiriliyor (Intel / MIT, SHA256 dogrulanir)"
    $tmpPm = Join-Path $env:TEMP "PresentMon-$Stamp.exe"
    Invoke-WebRequest -UseBasicParsing -Uri $PresentMonUrl -OutFile $tmpPm
    $hash = (Get-FileHash $tmpPm -Algorithm SHA256).Hash.ToLower()
    if ($hash -ne $PresentMonSha256) {
        Remove-Item $tmpPm -Force -ErrorAction SilentlyContinue
        Fail "PresentMon SHA256 uyusmuyor (beklenen $PresentMonSha256, gelen $hash). Kurulum durduruldu."
    }
    Get-Process -Name "PresentMon*" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Move-Item $tmpPm $PmExe -Force
}

# ---- 5. PC ayari ---------------------------------------------------------------
$conf = Join-Path $Roam "makine.conf"
if ($PcAdi -and -not (Test-Path $conf)) {
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($conf, "[makine]`r`nad = $PcAdi`r`n", $utf8)
}

# ---- 6. PresentMon yetkisi (Performance Log Users) --------------------------------
$groupSid = "S-1-5-32-559"
$inGroup = (((whoami /groups) -join "`n") -match $groupSid)
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$needRelogin = $false
if (-not $inGroup -and -not $isAdmin) {
    Say "PresentMon icin 'Performance Log Users' grubuna ekleniyorsun (bir kez UAC onayi istenir)"
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $cmd = "Add-LocalGroupMember -SID '$groupSid' -Member '$me' -ErrorAction Stop"
    try {
        Start-Process powershell.exe -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList @("-NoProfile", "-Command", $cmd)
        $needRelogin = $true
        try {
            $names = @(Get-LocalGroupMember -SID $groupSid -ErrorAction Stop | ForEach-Object { $_.Name })
            if ($names -notcontains $me) { Warn "Gruba ekleme dogrulanamadi (UAC reddedilmis olabilir). Gerekirse yonetici PowerShell'de: Add-LocalGroupMember -SID '$groupSid' -Member '$me'" }
        } catch {}
    } catch {
        Warn "Gruba eklenemedi ($($_.Exception.Message)). Programi yonetici olarak calistirmalisin ya da bu komutu yonetici PowerShell'de calistir:"
        Warn "  Add-LocalGroupMember -SID '$groupSid' -Member '$me'"
    }
}

# ---- 7. Kisayollar -------------------------------------------------------------
function New-Shortcut([string]$path) {
    $ws = New-Object -ComObject WScript.Shell
    $lnk = $ws.CreateShortcut($path)
    $lnk.TargetPath = $VenvPyW
    $lnk.Arguments = '"' + $AppFile + '"'
    $lnk.WorkingDirectory = $Data
    $lnk.Description = "Oyun Izleyici"
    $lnk.Save()
}
Say "Baslat menusu ve otomatik baslatma kisayollari"
New-Shortcut (Join-Path ([Environment]::GetFolderPath("Programs")) "Oyun Izleyici.lnk")
New-Shortcut (Join-Path ([Environment]::GetFolderPath("Startup"))  "Oyun Izleyici.lnk")

$taniBat = Join-Path $Data "tani.bat"
$taniText = "@echo off`r`nchcp 65001 >nul`r`n`"$VenvPy`" `"$AppFile`" --tani`r`npause`r`n"
[System.IO.File]::WriteAllText($taniBat, $taniText, (New-Object System.Text.UTF8Encoding($false)))

# ---- 8. Istege bagli: ozel kayit deposu (PC karsilastirma) ----------------------------
if ($KayitDeposu) {
    Say "Kayit deposu ayarlaniyor: $KayitDeposu"
    $git = (Get-Command git -ErrorAction SilentlyContinue)
    if (-not $git -and (Get-Command winget -ErrorAction SilentlyContinue)) {
        winget install -e --id Git.Git --scope user --accept-package-agreements --accept-source-agreements | Out-Host
        foreach ($g in @("$env:LOCALAPPDATA\Programs\Git\cmd\git.exe", "$env:ProgramFiles\Git\cmd\git.exe")) {
            if (Test-Path $g) { $git = Get-Item $g; break }
        }
    }
    if (-not $git) {
        Warn "git bulunamadi: https://git-scm.com/download/win adresinden kurup betigi -KayitDeposu ile tekrar calistir."
    } else {
        $gitExe = if ($git.Source) { $git.Source } else { $git.FullName }
        $depo = Join-Path $Data "depo"
        $sshKeygen = Join-Path $env:SystemRoot "System32\OpenSSH\ssh-keygen.exe"
        $key = Join-Path $env:USERPROFILE ".ssh\oyun_kayitlari_ed25519"
        if (-not (Test-Path (Join-Path $depo ".git"))) {
            New-Item -ItemType Directory -Force -Path (Split-Path $key) | Out-Null
            if (-not (Test-Path $key)) { & $sshKeygen -q -t ed25519 -N '""' -C "oyun-izleyici@$env:COMPUTERNAME" -f $key }
            $keyFwd = $key.Replace("\", "/")
            $sshCmd = "ssh -i `"$keyFwd`" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
            $env:GIT_SSH_COMMAND = $sshCmd
            & $gitExe clone -q "git@github.com:$KayitDeposu.git" $depo 2>$null
            if ($LASTEXITCODE -ne 0 -or -not (Test-Path (Join-Path $depo ".git"))) {
                Warn "Bu PC'nin depoya erisimi yok. Su genel anahtari depoya YAZMA izinli 'deploy key' olarak ekle:"
                Write-Host ""
                Get-Content "$key.pub" | Write-Host
                Write-Host ""
                Write-Host "  https://github.com/$KayitDeposu/settings/keys  (Allow write access isaretli)"
                Write-Host "Ekledikten sonra bu betigi ayni parametreyle tekrar calistir."
            } else {
                & $gitExe -C $depo config core.sshCommand $sshCmd
                Say "Kayit deposu baglandi."
            }
        } else {
            Say "Kayit deposu zaten kurulu."
        }
    }
}

# ---- 9. Baslat ---------------------------------------------------------------------
if ($env:OI_KOMUTA_URL) {
    $kConf = Join-Path $Roam "makine.conf"
    $kLines = if (Test-Path $kConf) { Get-Content $kConf } else { @() }
    $kLines = @($kLines | Where-Object { $_ -notmatch '^\s*komuta_url\s*=' }) + ("komuta_url = " + $env:OI_KOMUTA_URL)
    Set-Content -Path $kConf -Value $kLines -Encoding UTF8
}
if ($env:OI_KOMUTA_KEY) {
    Set-Content -Path (Join-Path $Roam "komuta.key") -Value $env:OI_KOMUTA_KEY -Encoding UTF8 -NoNewline
}
Say "Oyun Izleyici baslatiliyor"
Start-Process -FilePath $VenvPyW -ArgumentList ('"' + $AppFile + '"') -WorkingDirectory $Data

Write-Host ""
Say "Kurulum tamam."
Write-Host "   Program : $AppFile"
Write-Host "   Ayarlar : $conf   (ad, mod=otomatik|panel|tepsi, engelle=program.exe, ...)"
Write-Host "   Kayitlar: $Data\oyunlar.db"
Write-Host "   Tanilama: $taniBat   (sensorleri ve PresentMon'u dener; sorun olursa ciktisini paylas)"
if ($needRelogin) {
    Write-Host ""
    Warn "PresentMon yetkisi icin Windows oturumunu bir kez KAPATIP ACMALISIN (FPS yakalama o zamana kadar calismaz)."
}
