# RSI-Binance

Bot trading **Binance Spot multi-pair** berbasis konfirmasi RSI 15 menit dengan konteks RSI 1 jam dan 4 jam. Repository ini menyediakan mode simulasi (DRY RUN/paper), Binance Spot Testnet, live trading, backtest, dashboard read-only, notifikasi Telegram opsional, serta penyimpanan state untuk pemulihan setelah restart.

> [!WARNING]
> Trading aset kripto memiliki risiko tinggi dan dapat menyebabkan kehilangan seluruh modal. Software ini bukan nasihat keuangan dan tidak menjamin keuntungan. **Mulai dari DRY RUN, lanjutkan ke Testnet, dan jangan gunakan live trading sebelum memahami seluruh risiko serta memverifikasi perilaku bot.**

## Daftar isi

- [Fitur](#fitur)
- [Cara kerja strategi](#cara-kerja-strategi)
- [Persyaratan](#persyaratan)
- [Instalasi di Linux atau VPS](#instalasi-di-linux-atau-vps)
- [Instalasi di Windows](#instalasi-di-windows)
- [Konfigurasi environment](#konfigurasi-environment)
- [Menjalankan bot](#menjalankan-bot)
- [Menjalankan dashboard](#menjalankan-dashboard)
- [Menjalankan backtest](#menjalankan-backtest)
- [State, restart, dan rekonsiliasi](#state-restart-dan-rekonsiliasi)
- [PnL dan portfolio](#pnl-dan-portfolio)
- [Keamanan dan batas API Binance](#keamanan-dan-batas-api-binance)
- [Menjalankan di VPS](#menjalankan-di-vps)
- [Pengujian](#pengujian)
- [Troubleshooting](#troubleshooting)
- [Struktur repository](#struktur-repository)
- [Keterbatasan dan disclaimer](#keterbatasan-dan-disclaimer)

## Fitur

- Strategi RSI multi-timeframe untuk Binance Spot.
- Evaluasi sinyal entry hanya pada candle 15 menit yang sudah tertutup.
- Konteks RSI 1 jam dan 4 jam.
- RSI menggunakan metode Wilder/RMA.
- Position sizing berdasarkan persentase equity.
- Take profit dinamis berdasarkan ATR 1 jam, dengan batas minimum dan maksimum.
- Stop loss, cooldown, re-entry lockout, dan RSI re-arm setelah transaksi.
- Batas jumlah posisi terbuka dan entry per pair dalam jendela waktu.
- DRY RUN/paper trading tanpa mengirim order.
- Binance Spot Testnet dan live trading.
- OCO TP/SL untuk posisi live jika diaktifkan.
- Pemantauan TP/SL oleh bot sebagai fallback ketika OCO tidak digunakan.
- Penyimpanan state atomik untuk pemulihan setelah restart.
- Estimasi realized PnL, unrealized PnL, dan portfolio.
- Dashboard read-only dengan refresh otomatis setiap 5 detik dan diagnostik RSI per pair.
- Log aplikasi dan notifikasi Telegram opsional.
- Cache harga, throttling request, serta penanganan kondisi order yang hasilnya belum pasti.

## Cara kerja strategi

### Sinyal entry

Sinyal entry diproses ketika candle 15 menit baru selesai. Secara umum, kondisi yang diperiksa adalah:

1. RSI 15 menit sebelumnya berada pada atau di bawah `RSI_TRIGGER`.
2. RSI 15 menit terbaru menembus ke atas `RSI_TRIGGER`.
3. Dalam sejumlah candle sebelumnya, RSI pernah berada di bawah `RSI_OVERSOLD`.
4. RSI 4 jam berada di bawah `RSI_4H_MAX`.
5. RSI 1 jam berada di bawah `RSI_1H_MAX`.
6. Pair tidak memiliki posisi aktif.
7. Pair tidak sedang cooldown atau terkena lockout re-entry.
8. Batas entry dalam jendela waktu belum tercapai.
9. Syarat RSI re-arm terpenuhi jika reset post-trade masih aktif.
10. Saldo quote mencukupi dan order memenuhi ketentuan minimum exchange.
11. Jumlah posisi terbuka belum mencapai `MAX_OPEN_POSITIONS`.

### Take profit dan stop loss

Take profit dihitung saat entry:

```text
ATR TP (%) = ATR 1H (%) × TP_ATR_MULT
TP (%)     = clamp(ATR TP, TP_MIN_PCT, TP_MAX_PCT)
TP price   = entry price × (1 + TP (%) / 100)
SL price   = entry price × (1 - SL_PCT / 100)
```

Pada live mode dengan `USE_OCO=true`, bot mencoba memasang OCO SELL di Binance:

- Leg atas: `LIMIT_MAKER` untuk take profit.
- Leg bawah: `STOP_LOSS` untuk stop loss.

Jika hasil pemasangan atau eksekusi order tidak dapat dipastikan, bot menyimpan pending intent dan membekukan pair terkait untuk mencegah pengiriman order berulang yang berisiko. Periksa order dan saldo di Binance sebelum melakukan rekonsiliasi.

## Persyaratan

- Python 3.10 atau lebih baru.
- Git.
- Koneksi internet yang dapat mengakses Binance.
- Akun Binance untuk mode live.
- Akun dan API key Binance Spot Testnet untuk pengujian testnet.
- Linux/VPS direkomendasikan untuk operasi 24 jam.

Dependensi Python dipasang dari `requirements.txt`:

- `ccxt`: koneksi exchange dan eksekusi order.
- `requests`: request HTTP, termasuk kebutuhan notifikasi.
- `python-dotenv`: membaca konfigurasi dari file `.env`.
- `filelock`: penguncian lintas platform agar hanya satu proses bot per file state (menggantikan `fcntl` yang hanya tersedia di Unix).
- `pytest`: menjalankan unit test.

## Instalasi di Linux atau VPS

Contoh berikut menggunakan Ubuntu/Debian.

### 1. Instal paket sistem

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip
```

Periksa versi Python:

```bash
python3 --version
git --version
```

Pastikan Python versi 3.10 atau lebih baru.

### 2. Clone repository

```bash
git clone https://github.com/rmdnl/RSI-Binance.git
cd RSI-Binance
```

Jika repository sudah ada di server, masuk ke direktori repository yang sudah di-clone. Jangan membuat salinan kedua yang menjalankan state/account yang sama.

### 3. Buat virtual environment

```bash
python3 -m venv venv
source venv/bin/activate
```

Setiap kali membuka sesi terminal baru, aktifkan kembali environment:

```bash
cd RSI-Binance
source venv/bin/activate
```

### 4. Instal dependensi

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Pastikan instalasi selesai tanpa error.

### 5. Buat file konfigurasi

```bash
cp env.example .env
nano .env
```

Isi konfigurasi sesuai kebutuhan. Untuk instalasi pertama, **jangan mengubah `DRY_RUN=true`**.

Simpan file di editor nano dengan `Ctrl+O`, Enter, lalu keluar menggunakan `Ctrl+X`.

### 6. Jalankan pengujian awal

```bash
pytest -q
```

Jika test selesai dengan status sukses, lanjutkan ke [Menjalankan bot](#menjalankan-bot).

## Instalasi di Windows

Contoh menggunakan PowerShell.

### 1. Instal kebutuhan

Pastikan Git dan Python 3.10 atau lebih baru sudah terinstal dan dapat dipanggil dari PowerShell.

```powershell
python --version
git --version
```

### 2. Clone repository

```powershell
git clone https://github.com/rmdnl/RSI-Binance.git
cd RSI-Binance
```

### 3. Buat dan aktifkan virtual environment

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

Jika PowerShell memblokir aktivasi script, jalankan perintah berikut hanya untuk sesi terminal saat ini, kemudian aktifkan kembali environment:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\venv\Scripts\Activate.ps1
```

### 4. Instal dependensi

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 5. Buat file konfigurasi

```powershell
Copy-Item env.example .env
notepad .env
```

Isi konfigurasi, simpan, lalu jalankan test:

```powershell
pytest -q
```

## Konfigurasi environment

Semua konfigurasi bot dibaca dari file `.env`. Bot memuat `.env` yang berada di direktori repository (satu direktori dengan `bot.py`) supaya konfigurasi tetap terbaca walau proses dijalankan dari direktori kerja lain, misalnya melalui systemd. Jika file tersebut tidak ada, bot memakai pencarian `.env` bawaan `python-dotenv` dari direktori kerja. Gunakan `env.example` sebagai template resmi dan sesuaikan nilainya sebelum menjalankan.

Nilai di `env.example` sama dengan nilai default di kode. Jika sebuah variabel dihapus dari `.env`, bot memakai default tersebut, bukan nilai kosong.

### 1. Binance API dan mode

```dotenv
BINANCE_API_KEY=
BINANCE_API_SECRET=
BINANCE_TESTNET=false

DRY_RUN=true
DRY_RUN_BALANCE=1000
PAPER_FEE_PCT=0.1
```

Arti konfigurasi:

| Pengaturan | Fungsi |
|---|---|
| `BINANCE_API_KEY` | API key Binance |
| `BINANCE_API_SECRET` | API secret Binance |
| `BINANCE_TESTNET` | Menggunakan endpoint Binance Testnet ketika DRY RUN dimatikan |
| `DRY_RUN` | Simulasi tanpa order sungguhan |
| `DRY_RUN_BALANCE` | Modal awal virtual dalam quote asset |
| `PAPER_FEE_PCT` | Asumsi fee per sisi untuk simulasi |

**Perbedaan mode:**

| DRY_RUN | BINANCE_TESTNET | Perilaku |
|---|---|---|
| `true` | `false` | Simulasi paper, tidak mengirim order |
| `true` | `true` | Tetap simulasi paper |
| `false` | `true` | Mengirim order ke Binance Spot Testnet |
| `false` | `false` | Live trading menggunakan akun Binance sungguhan |

Untuk testnet, gunakan API key testnet, bukan API key akun live. Untuk live, gunakan API key akun live yang memiliki izin trading Spot.

> Jangan pernah mengunggah file `.env` ke GitHub, membagikan API secret, atau mengaktifkan izin withdrawal. Pastikan file `.env` tidak ikut di-commit ke repository.

### 2. Pair dan quote asset

```dotenv
SYMBOLS=BNB/USDT,ETH/USDT,SOL/USDT
QUOTE_ASSET=USDT
```

- `SYMBOLS`: daftar pair Spot, dipisahkan koma.
- `QUOTE_ASSET`: aset quote yang digunakan untuk saldo dan perhitungan portfolio.
- Semua pair sebaiknya menggunakan quote asset yang sama.
- Pastikan pair tersedia dan aktif di Binance Spot.

### 3. Telegram (opsional)

```dotenv
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

Untuk mengaktifkannya:

1. Buat bot Telegram melalui `@BotFather`.
2. Simpan token bot pada `TELEGRAM_BOT_TOKEN`.
3. Dapatkan chat ID tujuan.
4. Kirim pesan awal ke bot jika diperlukan agar bot dapat mengirim pesan ke chat tersebut.
5. Isi kedua variabel di file `.env`.

Notifikasi hanya aktif jika token dan chat ID sama-sama tersedia. Jika salah satu variabel kosong, bot tetap berjalan tanpa notifikasi dan menulis warning `Telegram tidak dikonfigurasi` di log saat startup.

Notifikasi startup menyebut mode yang benar-benar aktif — `Bot dimulai (DRY RUN)`, `Bot dimulai (TESTNET)`, atau `Bot dimulai (LIVE)` — dan notifikasi `BUY` diberi penanda `[DRY RUN]` atau `[TESTNET]`.

Semua kegagalan yang terjadi sebelum loop utama berjalan juga dikirim ke Telegram, bukan hanya tercatat di log: gagal menyiapkan exchange, state yang perlu rekonsiliasi manual, saldo gagal dibaca, rekonsiliasi startup gagal, dan proses lain yang masih memegang lock. Kegagalan pengiriman Telegram sendiri dicatat di log beserta status HTTP dan badan respons, misalnya `Telegram gagal: HTTP 401 {"description":"Unauthorized"}`.

Untuk memastikan jalur notifikasi berfungsi tanpa menjalankan bot:

```bash
cd /path/ke/repository
python3 -c "
import os, bot
bot.load_dotenv(os.path.join(os.getcwd(), '.env'))
cfg = bot.Config.from_env()
print('token terisi:', bool(cfg.tg_token), '| chat:', cfg.tg_chat)
bot.Notifier(cfg.tg_token, cfg.tg_chat).send('uji notifikasi manual')
"
```

Jika token terisi dan pesan uji sampai, tetapi notifikasi startup tetap tidak muncul, kemungkinan besar proses berhenti di salah satu jalur sebelum `run()`; periksa `journalctl -u <service> -n 100 --no-pager` atau `bot.log`.

### 4. Parameter RSI

```dotenv
RSI_PERIOD=14
RSI_4H_MAX=35
RSI_1H_MAX=35
RSI_OVERSOLD=25
RSI_TRIGGER=32
ARM_LOOKBACK=12
CONTEXT_USE_CLOSED=false
```

| Variabel | Fungsi |
|---|---|
| `RSI_PERIOD` | Periode RSI Wilder/RMA |
| `RSI_4H_MAX` | Batas maksimum RSI timeframe 4 jam |
| `RSI_1H_MAX` | Batas maksimum RSI timeframe 1 jam |
| `RSI_OVERSOLD` | Ambang oversold untuk proses arm sinyal |
| `RSI_TRIGGER` | Level RSI 15 menit yang harus ditembus ke atas |
| `ARM_LOOKBACK` | Jumlah candle 15 menit untuk mencari kondisi oversold sebelumnya |
| `CONTEXT_USE_CLOSED` | Memilih candle berjalan atau candle tertutup untuk konteks 1H/4H |

`CONTEXT_USE_CLOSED=false` menggunakan konteks candle 1H/4H yang sedang berjalan. Nilainya lebih responsif tetapi dapat berubah sebelum candle timeframe tersebut ditutup.

`CONTEXT_USE_CLOSED=true` hanya menggunakan candle 1H/4H yang sudah tertutup. Pendekatan ini lebih konservatif dan sinyal dapat lebih jarang.

### 5. Ukuran posisi

```dotenv
POSITION_PCT=30
MAX_OPEN_POSITIONS=3
```

- `POSITION_PCT`: persentase equity yang dialokasikan untuk satu entry.
- `MAX_OPEN_POSITIONS`: batas maksimum posisi bot yang terbuka secara bersamaan.

Perhatikan bahwa alokasi posisi bukan jaminan batas kerugian. Pergerakan harga, slippage, dan kegagalan eksekusi tetap dapat menimbulkan kerugian.

### 6. Take profit dan stop loss

```dotenv
TP_MIN_PCT=1.0
TP_MAX_PCT=3.0
TP_ATR_MULT=1.5
SL_PCT=2.0
USE_OCO=true
```

- TP dihitung dari ATR 1H dikalikan `TP_ATR_MULT`.
- Hasil TP dibatasi oleh `TP_MIN_PCT` dan `TP_MAX_PCT`.
- SL dihitung sebagai persentase dari harga entry.
- `USE_OCO=true`: bot mencoba memasang OCO TP/SL pada Binance untuk posisi live.
- `USE_OCO=false`: bot memantau harga dan mengirim market sell ketika TP/SL tersentuh. Mekanisme ini memerlukan bot tetap berjalan dan koneksi exchange tersedia.
- Pada DRY RUN, TP/SL selalu disimulasikan melalui pemantauan harga.

### 7. Cooldown dan re-entry

```dotenv
COOLDOWN_MINUTES=240
REENTRY_LOCKOUT_MINUTES=120
REARM_LEVEL=50
MAX_ENTRIES_PER_WINDOW=2
ENTRY_WINDOW_HOURS=24
```

- `COOLDOWN_MINUTES`: jeda pair setelah stop loss.
- `REENTRY_LOCKOUT_MINUTES`: jeda minimum setelah transaksi sebelum entry baru.
- `REARM_LEVEL`: RSI 15 menit harus pulih ke level ini sebelum sinyal berikutnya dapat diterima setelah transaksi.
- `MAX_ENTRIES_PER_WINDOW`: maksimum entry per pair dalam jendela waktu.
- `ENTRY_WINDOW_HOURS`: panjang jendela pembatasan entry.

### 8. Loop dan file state/log

```dotenv
LOOP_SECONDS=10
STATE_FILE=state.json
LOG_FILE=bot.log
```

- `LOOP_SECONDS`: interval siklus pemeriksaan bot.
- `STATE_FILE`: lokasi file penyimpanan state.
- `LOG_FILE`: lokasi file log.

Walaupun loop berjalan berkala, sinyal entry 15 menit hanya dievaluasi berdasarkan candle 15 menit yang sudah tertutup. Jangan menurunkan interval tanpa alasan karena request yang terlalu sering dapat meningkatkan penggunaan API.

## Menjalankan bot

Jalankan perintah dari direktori repository dengan virtual environment aktif.

### 1. DRY RUN (wajib untuk pengujian pertama)

Pastikan konfigurasi:

```dotenv
DRY_RUN=true
BINANCE_TESTNET=false
```

Jalankan:

```bash
python bot.py
```

Di Windows, perintahnya sama:

```powershell
python bot.py
```

Pada mode ini bot tidak mengirim order. Saldo simulasi dimulai dari `DRY_RUN_BALANCE`. Bot tetap membutuhkan koneksi internet untuk mengambil data market.

Amati output terminal dan file log. Pastikan tidak ada error konfigurasi, pair, indikator, atau koneksi market.

### 2. Binance Spot Testnet

Setelah DRY RUN berjalan dengan baik:

1. Buat/akses akun Binance Spot Testnet.
2. Buat API key dan secret khusus Testnet.
3. Isi API key Testnet ke `.env`.
4. Ubah konfigurasi:

```dotenv
DRY_RUN=false
BINANCE_TESTNET=true
```

5. Jalankan kembali:

```bash
python bot.py
```

Bot sekarang dapat mengirim order ke lingkungan Testnet. Periksa order, fill, saldo, dan perilaku TP/SL melalui akun Testnet.

### 3. Live trading

Live trading menggunakan dana sungguhan. Lakukan hanya setelah pengujian DRY RUN dan Testnet, serta setelah memahami state, pending order, OCO, dan risiko eksekusi.

Konfigurasi live:

```dotenv
DRY_RUN=false
BINANCE_TESTNET=false
BINANCE_API_KEY=API_KEY_LIVE_ANDA
BINANCE_API_SECRET=API_SECRET_LIVE_ANDA
```

Mulai dengan modal kecil dan pantau secara langsung. Jangan menganggap hasil Testnet atau backtest sebagai jaminan hasil live.

### Penting sebelum mengganti mode

State menyimpan mode bot (`dry`, `testnet`, atau `live`). Bot menolak penggunaan state dari mode berbeda untuk mencegah pencampuran posisi dan saldo. Gunakan state terpisah untuk lingkungan yang berbeda. Jangan menghapus atau mengedit state live hanya untuk melewati pemeriksaan mode.

**Jalankan hanya satu proses bot untuk satu file state dan satu akun.** Dua proses yang memakai state/account sama dapat menyebabkan konflik order dan data.

## Menjalankan dashboard

Dashboard adalah aplikasi terpisah dari proses bot. Dashboard membaca file state dan tidak mengirim order.

### 1. Pastikan bot sudah berjalan

Dashboard mengambil data dari `STATE_FILE`. Jika belum ada state atau snapshot portfolio, dashboard akan menampilkan informasi bahwa data belum tersedia.

### 2. Buka terminal kedua

Jangan menghentikan terminal bot. Buka sesi terminal baru, masuk ke direktori repository, lalu aktifkan virtual environment.

Linux/VPS:

```bash
cd RSI-Binance
source venv/bin/activate
python dashboard.py
```

Windows PowerShell:

```powershell
cd RSI-Binance
.\venv\Scripts\Activate.ps1
python dashboard.py
```

### 3. Buka dashboard

Akses melalui browser pada komputer yang menjalankan dashboard:

```text
http://127.0.0.1:8060
```

Dashboard melakukan refresh otomatis setiap **5 detik**.

Informasi yang ditampilkan:

- Mode bot dan quote asset.
- Diagnostik RSI per pair (RSI 15m/1h/4h, status, alasan gate entry) dan grafik interaktif tren RSI 15m/1h/4h selama 24 jam terakhir untuk pair yang dipilih. Histori memakai candle tertutup dan disimpan dalam state; RSI 1h/4h berasal dari agregasi candle 15m sehingga tidak menambah request API pada setiap evaluasi. Nilai agregasi memakai histori 15m yang tersedia, jadi dapat sedikit berbeda dari RSI 1h/4h exchange yang dipakai sebagai gate entry pada keputusan nyata.
- Equity estimasi beserta label valuasi (portofolio kertas pada dry run, mark-to-market pada live).
- Unrealized PnL dan realized PnL.
- Daftar posisi bot, kuantitas, entry, mark price, nilai, TP, dan SL.
- Saldo aset: total, free, used, dan estimasi nilai.
- Aset yang tidak dapat dinilai dalam quote asset.
- Status API pause dan jumlah event rate-limit.
- 100 event/error terbaru.
- Waktu snapshot dan usia data.

### 4. Konfigurasi port dan state dashboard

Dashboard menggunakan environment variable berikut:

| Variabel | Default | Fungsi |
|---|---|---|
| `DASHBOARD_HOST` | `127.0.0.1` | Alamat bind dashboard |
| `DASHBOARD_PORT` | `8060` | Port dashboard |
| `STATE_FILE` | `state.json` | File state yang dibaca |

Dashboard tidak memuat file `.env` secara otomatis. Jika ingin mengubah konfigurasi dashboard, berikan environment variable pada proses saat dijalankan.

Contoh Linux:

```bash
DASHBOARD_HOST=127.0.0.1 DASHBOARD_PORT=8060 STATE_FILE=state.json python dashboard.py
```

Pastikan dashboard dan bot menggunakan file `STATE_FILE` yang sama dan dijalankan dari direktori kerja yang sesuai.

### 5. Akses dashboard VPS dari komputer lokal

Default dashboard hanya bind ke localhost VPS. Gunakan SSH tunnel agar dashboard dapat dibuka dengan aman dari komputer pribadi:

```bash
ssh -L 8060:127.0.0.1:8060 USER@IP_VPS
```

Ganti `USER` dan `IP_VPS` sesuai server.

Setelah SSH terhubung, buka di browser komputer lokal:

```text
http://127.0.0.1:8060
```

Jangan mengekspos port dashboard langsung ke internet. Dashboard tidak menyediakan autentikasi pengguna atau TLS bawaan.

## Menjalankan backtest

Backtest menggunakan data historis candle 15 menit Binance. Data dapat diunduh dari endpoint publik, sehingga API key tidak diperlukan untuk pengunduhan data historis.

Contoh:

```bash
# Backtest default 90 hari
python backtest.py --symbol NEARUSDT --days 90

# Backtest 180 hari dengan beberapa kombinasi RSI 4H/1H
python backtest.py --symbol NEARUSDT --days 180 --combos 30/30,35/40,40/35,40/40

# Gunakan konteks candle 1H/4H yang sudah tertutup
python backtest.py --symbol NEARUSDT --days 180 --closed

# Simpan data historis yang diunduh ke CSV
python backtest.py --symbol NEARUSDT --days 90 --save-csv nearusdt_15m.csv

# Ulangi backtest menggunakan CSV lokal
python backtest.py --symbol NEARUSDT --days 90 --data-csv nearusdt_15m.csv
```

### Argument backtest

| Argument | Fungsi |
|---|---|
| `--symbol` | Pair dalam format simbol Binance, misalnya `NEARUSDT` |
| `--days` | Jumlah hari periode pengujian |
| `--warmup-days` | Data tambahan untuk pemanasan indikator |
| `--combos` | Kombinasi batas RSI 4H/1H, format `30/30,35/40` |
| `--closed` | Menggunakan konteks candle 1H/4H tertutup |
| `--period` | Periode RSI |
| `--oversold` | Ambang oversold (default `25`, sama dengan `RSI_OVERSOLD` di `env.example`) |
| `--trigger` | Level trigger RSI 15m |
| `--lookback` | Lookback candle untuk kondisi oversold |
| `--rearm` | Level RSI untuk re-arm |
| `--pos` | Persentase equity per posisi |
| `--tp-min`, `--tp-max` | Batas minimum dan maksimum TP |
| `--tp-atr` | Pengali ATR untuk TP |
| `--sl` | Persentase stop loss |
| `--cooldown` | Cooldown setelah SL dalam menit |
| `--lockout` | Lockout re-entry dalam menit |
| `--max-entries` | Batas entry dalam jendela waktu |
| `--window-hours` | Panjang jendela entry dalam jam |
| `--fee` | Fee per sisi dalam persen |
| `--slip` | Asumsi slippage SL dalam persen |
| `--data-csv` | Membaca candle dari CSV lokal |
| `--save-csv` | Menyimpan data hasil unduhan ke CSV |

Pada tabel hasil, kolom `ditahan` menghitung sinyal yang lolos konteks RSI 4H/1H tetapi diblok aturan anti beli beruntun (cooldown setelah SL, lockout re-entry, batas entry per jendela, atau reset RSI belum pulih). Backtest akan mencetak peringatan bila data sebelum jendela uji kurang dari 200 candle, karena RSI awal belum stabil.

Backtest bukan simulasi sempurna kondisi live. Entry diasumsikan pada close candle sinyal. Jika SL dan TP tersentuh dalam candle yang sama, SL dihitung lebih dahulu. ATR dinormalisasi terhadap close candle 1H terakhir, sedangkan bot memakai harga terkini. Max drawdown dihitung pada saat trade ditutup, bukan intratrade, sehingga nilainya lebih kecil dari drawdown real-time. Spread, market impact, latency, partial fill, downtime, dan seluruh kondisi kegagalan exchange tidak dimodelkan secara lengkap.

## State, restart, dan rekonsiliasi

Bot menyimpan state secara atomik menggunakan file sementara dan penggantian file. State mencakup mode bot, saldo paper, status pair, candle terakhir, cooldown, lockout, posisi, pending order, informasi OCO, PnL, snapshot portfolio, dan event log.

### Backup state

Sebelum upgrade, perubahan konfigurasi besar, atau pemulihan manual, hentikan proses bot dengan benar dan backup file state serta log:

```bash
cp state.json state.json.backup
cp bot.log bot.log.backup
```

Sesuaikan nama file jika `STATE_FILE` atau `LOG_FILE` diubah.

Jangan mengedit state ketika bot sedang berjalan. Jangan menghapus state live sebagai cara untuk mengatasi error.

### Pending order dan order ambiguous

Jika respons order BUY, SELL, atau OCO tidak dapat dipastikan, bot menyimpan pending intent dan membekukan pair terkait. Jangan mengirim order pengganti secara manual sebelum memeriksa kondisi exchange.

Periksa hal berikut langsung di Binance:

- Open orders.
- Order history.
- Trade history dan fill.
- Partial fill.
- Saldo base dan quote.
- Order ID dan OCO order-list ID.

Setelah kondisi exchange dan state cocok, lakukan rekonsiliasi secara hati-hati. Jangan menghapus pending intent hanya agar bot kembali trading.

### OCO terisi sebagian

Jika salah satu leg OCO hanya terisi sebagian, bot tidak mengurangi kuantitas posisi dan tidak melepas referensi OCO. Pair ditandai `reconciliation` dan berhenti diproses sampai kondisi exchange dicocokkan manual. Alasan: sisa kuantitas leg OCO dapat masih aktif di exchange, sehingga mengurangi posisi secara lokal berisiko menjual kuantitas yang sama dua kali.

### Saat bot restart atau berhenti

- Posisi yang masih terbuka tidak otomatis dijual ketika bot berhenti.
- OCO yang sudah diterima Binance dapat tetap aktif di exchange meskipun bot/VPS mati.
- Posisi tanpa OCO tidak dipantau selama proses bot mati.
- Selalu periksa posisi dan open order setelah restart, terutama jika proses sebelumnya berhenti ketika order sedang diproses.
- Pada mode DRY RUN, pending order yang tertinggal dari proses yang berhenti mendadak dibersihkan otomatis saat startup, karena mode ini tidak pernah membuat order exchange.

## PnL dan portfolio

### Unrealized PnL

Unrealized PnL merupakan estimasi perubahan nilai posisi berdasarkan mark price dikurangi biaya posisi yang tercatat.

### Realized PnL

Realized PnL dihitung dari hasil penjualan dikurangi bagian biaya posisi yang terjual. Perhitungan ini hanya mencakup transaksi yang dikenali dan dicatat bot sejak tracking dimulai.

Perhitungan tidak selalu mencakup transaksi manual, deposit, withdrawal, trade sebelum tracking dimulai, atau seluruh fee yang dibayar dalam aset lain.

### Equity

Equity merupakan estimasi nilai portfolio dalam quote asset berdasarkan harga market yang tersedia. Equity bukan saldo free yang dapat langsung digunakan untuk order atau withdrawal.

Aset yang tidak memiliki harga konversi yang tersedia akan ditampilkan, tetapi dikecualikan dari estimasi equity. Dashboard bukan laporan resmi Binance atau laporan pajak.

## Keamanan dan batas API Binance

### Pengamanan API key

Untuk API key live:

- Aktifkan hanya izin Spot trading yang dibutuhkan.
- Matikan izin withdrawal.
- Gunakan IP whitelist jika tersedia.
- Pisahkan API key live dan Testnet.
- Jangan menyimpan API key di source code.
- Jangan membagikan file `.env`.
- Jangan mengunggah state atau log yang berisi informasi sensitif ke repository publik.

### Pengendalian request

Bot menggunakan beberapa mekanisme untuk mengurangi tekanan API:

- CCXT `enableRateLimit`.
- Timeout REST.
- Cache mark price.
- Pembatasan frekuensi snapshot portfolio.
- Pembatasan polling OCO.
- Pencatatan event rate-limit.
- Global pause saat mendeteksi kondisi 429/418 atau rate-limit.
- Pemakaian `Retry-After` jika tersedia.
- Penanganan konservatif untuk order yang hasilnya belum pasti.

Mekanisme tersebut tidak menjamin bebas dari rate-limit atau IP ban. Binance dapat mengubah API weight, order limit, endpoint, dan kebijakan sewaktu-waktu.

Rekomendasi:

1. Jalankan satu instance bot untuk satu state/account.
2. Jangan memperkecil `LOOP_SECONDS` tanpa kebutuhan.
3. Hindari beberapa proses yang melakukan private REST request dari IP sama.
4. Pantau log dan status rate-limit.
5. Jangan melakukan retry manual berulang ketika status order belum jelas.

## Menjalankan di VPS

Untuk operasi berkelanjutan, gunakan Linux/VPS dengan direktori repository tetap dan virtual environment khusus.

Contoh sesi menggunakan tmux:

```bash
sudo apt install -y tmux

cd RSI-Binance
source venv/bin/activate

tmux new -s rsi-bot
python bot.py
```

Untuk detach tanpa menghentikan bot: tekan `Ctrl+B`, kemudian `D`.

Melihat kembali sesi:

```bash
tmux attach -t rsi-bot
```

Dashboard dijalankan dalam sesi terpisah:

```bash
cd RSI-Binance
source venv/bin/activate
tmux new -s rsi-dashboard
python dashboard.py
```

Detach dengan `Ctrl+B`, lalu `D`.

Untuk melihat sesi tmux:

```bash
tmux ls
```

tmux menjaga proses tetap berjalan setelah sesi SSH terputus, tetapi tidak otomatis menjamin bot aktif kembali setelah reboot VPS. Untuk auto-start dan restart otomatis, siapkan service manager seperti systemd secara terpisah setelah konfigurasi dan perilaku bot sudah diuji. Pastikan hanya ada satu proses bot yang memakai state/account tersebut.

## Pengujian

Jalankan dari direktori repository dengan virtual environment aktif:

```bash
pytest -q
```

Unit test yang tersedia (`tests/test_safety.py`, `tests/test_backtest.py`, dan `tests/test_notifier.py`; 29 kasus) mencakup:

- RSI pada data yang naik monotonis bernilai 100.
- RSI pada data flat bernilai 100 (regresi sentinel lama `50`).
- RSI saat seluruh pergerakan turun bernilai 0.
- RSI mengembalikan `None` bila jumlah data kurang dari periode.
- ATR% memakai `current_price` sebagai harga referensi.
- ATR% mengembalikan `None` bila data tidak cukup.
- `ProcessLock` mengunci lintas platform dan menolak acquire kedua.
- `poll_oco` bersifat fail-closed (`unknown`) saat exchange error.
- `Config.from_env` menolak nilai integer non-bulat.
- `update_rsi_history` membuang sampel di luar window 24 jam namun tetap menyimpan sampel lama yang masih di dalam window.
- `aggregate_candles` menggabungkan candle 15m menjadi 1h/4h dengan OHLCV yang benar.
- `scan_entry` menulis `diagnostics` dan `rsi_history` (15m/1h/4h) saat pair belum memenuhi syarat entry.
- `update_portfolio` menulis snapshot dashboard lengkap pada mode dry run (equity, unrealized/realized PnL, posisi, saldo).
- Nilai default `Config.from_env` sama dengan nilai di `env.example`.
- `reconcile_startup` pada mode dry run membersihkan pending order yang tertinggal.
- OCO yang terisi sebagian bersifat fail-closed: kuantitas posisi tidak dikurangi dan pair menunggu rekonsiliasi manual.
- CLI `backtest.py` dapat mem-parse argumennya sendiri (regresi format `%` pada teks `--help`).
- Net return backtest pada jalur SL sama dengan rumus `(entry*(1-SL%)*(1-slip)/entry)*(1-fee)^2-1`.
- Net return backtest pada jalur TP sama dengan rumus `(1+TP%)*(1-fee)^2-1`.
- Backtest menghitung sinyal yang diblok lockout/max-entries pada kolom `ditahan`.
- Notifier tidak mengirim apa pun bila token/chat ID kosong, dan mencatat status HTTP serta tipe exception saat pengiriman gagal.
- `run()` memancarkan notifikasi start lalu stop meskipun loop langsung dihentikan.
- Hanya ada satu pembuatan `Notifier` di `main()` dan setiap jalur keluar sebelum `run()` mengirim peringatan.
- Notifikasi startup menyebut mode sebenarnya (`DRY RUN`, `TESTNET`, atau `LIVE`), bukan hanya dry/live biner.
- `new_state` menolak state dengan mode berbeda (`State mode dry tidak cocok dengan testnet`), sehingga state DRY RUN tidak pernah dipakai ulang oleh Testnet atau live.

Unit test tidak mengirim order dan tidak membuktikan profitabilitas atau keamanan seluruh skenario live.

## Troubleshooting

### `python: command not found`

Pada Ubuntu/Debian, gunakan `python3` dan pastikan paket `python3` serta `python3-venv` terinstal.

### `No module named ...`

Pastikan virtual environment aktif, kemudian jalankan:

```bash
pip install -r requirements.txt
```

### Service restart berulang dan keluar dengan status 2

Jika `journalctl -u <unit>` menunjukkan `Main process exited, code=exited, status=2` bersamaan dengan `State perlu rekonsiliasi manual: State mode dry tidak cocok dengan testnet`, artinya `state.json` berasal dari mode lain. Mode aktif ditentukan `DRY_RUN` dan `BINANCE_TESTNET` di `.env`.

Jangan mengedit `bot_mode` di dalam state untuk melewati pemeriksaan — arsipkan state lama agar bot membuat state baru yang bersih untuk mode aktif:

```bash
sudo systemctl stop <unit>
cd /home/ubuntu/RSI-Binance
mv state.json "state.json.$(date +%Y%m%d-%H%M%S).bak"
sudo systemctl start <unit>
```

Lakukan hanya saat tidak ada posisi terbuka, dan hanya bila state lama memang berasal dari mode simulasi. State live yang tidak cocok tidak boleh diarsipkan tanpa memeriksa saldo dan open order lebih dulu.

### Notifikasi Telegram tidak muncul

1. Pastikan `.env` berada di direktori yang sama dengan `bot.py` dan berisi `TELEGRAM_BOT_TOKEN` serta `TELEGRAM_CHAT_ID`.
2. Cari baris `Telegram tidak dikonfigurasi` atau `Telegram gagal: HTTP 4xx` di log dan `journalctl`.
3. Periksa kode keluar proses. Kode `2` berarti bot berhenti sebelum loop utama; pesannya dikirim ke Telegram bila token valid.
4. Kirim pesan uji dengan perintah `python3 -c` pada seksi Telegram di atas untuk memisahkan masalah kredensial dari masalah jalur startup.

### File `.env` tidak terbaca

Pastikan file bernama tepat `.env` dan berada di direktori yang sama dengan `bot.py`; bot memuat `.env` dari direktori `bot.py`, bukan dari direktori kerja, sehingga tetap terbaca saat dijalankan sebagai service (mis. systemd). Periksa juga apakah variabel yang dibutuhkan sudah diisi.

### API key atau autentikasi gagal

Pastikan API key dan secret sesuai dengan lingkungan yang dipilih. Testnet harus memakai key Testnet, sedangkan live harus memakai key live. Periksa izin Spot trading, IP whitelist, dan status akun.

### Pair atau market tidak ditemukan

Pastikan simbol tersedia di Binance Spot dan format pair benar, misalnya `ETH/USDT`. Pastikan quote asset sesuai dengan konfigurasi.

### Dashboard menampilkan state tidak ditemukan

Pastikan bot sudah dijalankan, dashboard menggunakan direktori kerja yang benar, dan `STATE_FILE` pada kedua proses menunjuk ke file yang sama.

### Dashboard tidak menampilkan data terbaru

Pastikan proses bot masih aktif dan snapshot portfolio sudah tersedia. Dashboard refresh setiap 5 detik, tetapi data sumber hanya berubah ketika bot memperbarui state.

### Bot menolak pergantian mode

Periksa mode yang tercatat dalam state. Gunakan state terpisah untuk DRY RUN, Testnet, dan live. Jangan menghapus state live untuk melewati pemeriksaan ini.

### Pending order atau OCO

Jangan langsung mengulang order. Periksa open orders, order history, fill, saldo, dan ID order di Binance sebelum melakukan rekonsiliasi.

## Struktur repository

```text
RSI-Binance/
├── bot.py               # Bot trading, indikator, broker, state, dan API
├── dashboard.py         # Dashboard read-only, default port 8060
├── backtest.py          # Backtest historis strategi RSI
├── env.example          # Template konfigurasi bot
├── requirements.txt     # Dependensi Python
├── tests/
│   ├── test_safety.py   # Unit test keselamatan dan indikator
│   ├── test_backtest.py # Unit test parser dan matematika backtest
│   └── test_notifier.py # Unit test jalur notifikasi Telegram tanpa jaringan
└── README.md            # Dokumentasi proyek
```

## Keterbatasan dan disclaimer

- Bot merupakan sistem Binance Spot long-only berdasarkan pair yang dikonfigurasi.
- Harga, candle, precision, filter pair, dan respons exchange dapat berubah.
- Market order dapat mengalami slippage dan spread.
- OCO dapat ditolak oleh exchange akibat filter, precision, saldo, atau kondisi order.
- Partial fill dan transaksi manual dapat menyebabkan state serta PnL berbeda dari Binance.
- Posisi tanpa OCO tidak terlindungi oleh pemantauan bot ketika proses bot mati.
- Dashboard menyajikan estimasi dan bukan laporan resmi exchange.
- Backtest dan hasil historis tidak menjamin hasil masa depan.
- Optimasi parameter dapat menyebabkan overfitting.
- Live trading membutuhkan pengawasan manusia, pengujian, dan pengelolaan risiko.

Pengguna bertanggung jawab atas API key, modal, konfigurasi, pengujian, keamanan server, kepatuhan terhadap ketentuan yang berlaku, serta seluruh keputusan trading.
