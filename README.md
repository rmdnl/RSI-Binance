# RSI-Binance: Spot Trading Bot

Bot trading spot Binance multi-pair berbasis konfirmasi RSI 15 menit dengan konteks RSI 1 jam dan 4 jam. Repository ini menyediakan bot paper/live/testnet, backtest, dashboard read-only lokal, notifikasi Telegram, dan penyimpanan state untuk pemantauan ulang setelah restart.

> **Peringatan:** Perangkat lunak ini bukan nasihat keuangan. Trading kripto berisiko tinggi dan dapat kehilangan seluruh modal. Uji terlebih dahulu dalam DRY RUN dan Binance testnet. Tidak ada jaminan profit.

## Daftar Isi

- [Ringkasan fitur](#ringkasan-fitur)
- [Cara kerja strategi](#cara-kerja-strategi)
- [Persyaratan dan instalasi](#persyaratan-dan-instalasi)
- [Konfigurasi `.env`](#konfigurasi-env)
- [Urutan menjalankan](#urutan-menjalankan)
- [Dashboard port 8060](#dashboard-port-8060)
- [State dan rekonsiliasi](#state-dan-rekonsiliasi)
- [PnL dan portfolio](#pnl-dan-portfolio)
- [Pengamanan API Binance](#pengamanan-api-binance)
- [Backtest](#backtest)
- [Test](#test)
- [Menjalankan di VPS](#menjalankan-di-vps)
- [Keterbatasan penting](#keterbatasan-penting)

## Ringkasan fitur

- Entri RSI multi-timeframe untuk Spot Binance.
- Evaluasi sinyal hanya pada candle 15 menit yang sudah tertutup.
- Konteks RSI 1 jam dan 4 jam, menggunakan candle tertutup atau candle berjalan.
- Ukuran posisi sebagai persentase equity.
- Take profit dinamis berbasis ATR 1 jam, dibatasi nilai minimum dan maksimum.
- Stop loss persentase, cooldown, lockout re-entry, dan reset RSI setelah transaksi.
- Maksimal satu posisi per pair serta batas total posisi terbuka.
- Paper trading tanpa order Binance.
- Binance testnet dan live mode.
- OCO TP/SL untuk posisi live.
- Pemantauan lokal fallback tanpa OCO atau saat OCO gagal dipastikan.
- Penyimpanan state atomik untuk restart dan perubahan proses.
- Realized PnL bot dan unrealized PnL mark-to-market.
- Dashboard read-only: equity estimasi, posisi, saldo aset, PnL, TP/SL, status API, dan log event.
- 100 event terbaru tersedia pada dashboard; maksimum 300 event disimpan di state.
- Throttling dan cache harga untuk mengurangi tekanan REST API.
- Penanganan fail-close untuk sell/order/OCO yang hasilnya belum pasti.
- Rotating log file dan notifikasi Telegram opsional.

## Cara kerja strategi

### Sinyal entri

Sinyal valid jika seluruh kondisi berikut terpenuhi:

1. RSI 15 menit sebelumnya `<= RSI_TRIGGER`.
2. RSI 15 menit candle close terbaru `> RSI_TRIGGER`.
3. Minimal satu RSI dalam `ARM_LOOKBACK` candle 15 menit sebelumnya berada di bawah `RSI_OVERSOLD`.
4. RSI 4 jam lebih rendah dari `RSI_4H_MAX`.
5. RSI 1 jam lebih rendah dari `RSI_1H_MAX`.
6. Tidak ada posisi aktif pada pair.
7. Pair tidak sedang cooldown.
8. Lockout re-entry sudah selesai.
9. Jumlah entry dalam jendela waktu belum mencapai batas.
10. Jumlah posisi terbuka belum mencapai `MAX_OPEN_POSITIONS`.
11. Bila reset post-trade aktif, RSI 15 menit sudah pulih ke `REARM_LEVEL`.
12. Saldo quote cukup dan nilai order memenuhi minimum exchange.

RSI dihitung dengan metode Wilder/RMA, bukan SMA RSI.

### TP dan SL

TP dinamis dihitung saat entry:

```text
atr_tp_pct = ATR% 1H × TP_ATR_MULT
TP_PCT = clamp(atr_tp_pct, TP_MIN_PCT, TP_MAX_PCT)
TP = entry × (1 + TP_PCT / 100)
SL = entry × (1 - SL_PCT / 100)
```

Pada live mode dengan `USE_OCO=true`, bot mencoba memasang SELL OCO:

- Leg atas: `LIMIT_MAKER` sebagai take profit.
- Leg bawah: `STOP_LOSS` sebagai stop loss.

Bila OCO ditolak atau respons tidak dapat dipastikan, bot tidak memasang ulang secara agresif. Posisi dibekukan/pending untuk rekonsiliasi manual. Pada DRY RUN, TP/SL hanya dipantau secara simulasi.

## Persyaratan dan instalasi

### Persyaratan

- Python 3.10 atau lebih baru.
- Akses internet ke Binance.
- Akun Binance/testnet untuk mode live atau testnet.
- API key dengan izin terbatas untuk mode live/testnet.
- Linux/VPS direkomendasikan untuk operasi berkelanjutan.

### Instalasi

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp env.example .env
```

Edit `.env` sebelum menjalankan bot.

### Dependensi

```text
ccxt>=4.3.0
requests>=2.31.0
python-dotenv>=1.0.0
pytest>=8.0.0
```

## Konfigurasi `.env`

### Binance API

```dotenv
BINANCE_API_KEY=
BINANCE_API_SECRET=
BINANCE_TESTNET=false
```

Aktifkan hanya izin spot trading yang diperlukan. Matikan Withdrawal. IP whitelist sangat disarankan. Gunakan akun dan kunci testnet terpisah untuk pengujian.

### Mode eksekusi

```dotenv
DRY_RUN=true
DRY_RUN_BALANCE=1000
PAPER_FEE_PCT=0.1
```

- `DRY_RUN=true`: tidak mengirim order; saldo mulai dari `DRY_RUN_BALANCE`.
- `PAPER_FEE_PCT`: fee per sisi dalam simulasi paper.
- `BINANCE_TESTNET=true`: aktif pada mode `DRY_RUN=false` untuk Binance testnet.
- `DRY_RUN=false`: bot dapat mengirim order live/testnet sungguhan.

### Pair

```dotenv
SYMBOLS=BNB/USDT,ETH/USDT,SOL/USDT
QUOTE_ASSET=USDT
```

Gunakan pair Spot yang tersedia dan memiliki quote sama dengan `QUOTE_ASSET`. Format `BNBUSDT` dapat dicoba bila resolver menemukan market yang sesuai.

### Telegram

```dotenv
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

Telegram aktif hanya jika keduanya terisi.

### RSI dan timeframe

```dotenv
RSI_PERIOD=14
RSI_4H_MAX=35
RSI_1H_MAX=35
RSI_OVERSOLD=25
RSI_TRIGGER=32
ARM_LOOKBACK=12
CONTEXT_USE_CLOSED=false
```

- `CONTEXT_USE_CLOSED=false`: RSI 1H/4H dapat menggunakan candle berjalan.
- `CONTEXT_USE_CLOSED=true`: hanya candle 1H/4H yang sudah tertutup.

### Ukuran posisi

```dotenv
POSITION_PCT=30
MAX_OPEN_POSITIONS=3
```

`POSITION_PCT` adalah persentase equity yang dialokasikan ke satu entry.

### TP dan SL

```dotenv
TP_MIN_PCT=1.0
TP_MAX_PCT=3.0
TP_ATR_MULT=1.5
SL_PCT=2.0
USE_OCO=true
```

### Cooldown dan entry

```dotenv
COOLDOWN_MINUTES=240
REENTRY_LOCKOUT_MINUTES=120
REARM_LEVEL=50
MAX_ENTRIES_PER_WINDOW=2
ENTRY_WINDOW_HOURS=24
```

- Setelah stop loss, pair masuk cooldown.
- Setelah transaksi, `need_reset` menahan entry sampai RSI pemulihan tercapai.
- Lockout dan batas jendela diterapkan per pair.

### Loop dan file

```dotenv
LOOP_SECONDS=10
STATE_FILE=state.json
LOG_FILE=bot.log
```

Interval terlalu rendah dapat meningkatkan tekanan REST API. Default `10` detik adalah simulasi/cycle; pasar 15 menit dievaluasi hanya saat candle baru close.

## Urutan menjalankan

Urutan aman yang direkomendasikan:

```bash
python bot.py
```

Mulai dengan:

```dotenv
DRY_RUN=true
```

Setelah stabil, gunakan testnet:

```dotenv
DRY_RUN=false
BINANCE_TESTNET=true
```

Baru gunakan live dengan modal kecil:

```dotenv
DRY_RUN=false
BINANCE_TESTNET=false
```

Bot menolak perpindahan mode state yang tidak konsisten. Jangan memindahkan state live ke testnet atau sebaliknya tanpa rekonsiliasi manual.

## Dashboard port 8060

Jalankan bot, lalu dashboard terpisah:

```bash
python dashboard.py
```

Buka:

```text
http://127.0.0.1:8060
```

Dashboard dapat menampilkan:

- Mode bot.
- Equity estimasi dalam quote asset.
- Unrealized PnL.
- Realized PnL yang tercatat bot.
- Jumlah dan daftar posisi terbuka.
- Pair, status, kuantitas, entry, mark price, nilai, TP, dan SL.
- Saldo total, free, dan used per aset.
- Nilai aset dalam quote asset.
- Aset yang tidak dapat dinilai dan dikecualikan dari equity.
- Status pause API dan jumlah event rate-limit.
- 100 event/error terbaru.
- Waktu dan usia snapshot terakhir.

Dashboard hanya membaca `STATE_FILE`, tidak mengirim order, tidak mengubah state, dan tidak memiliki endpoint trading.

Default bind adalah localhost. Untuk akses remote dari VPS:

```bash
ssh -L 8060:127.0.0.1:8060 user@vps-host
```

Kemudian buka `http://127.0.0.1:8060` pada komputer lokal. Jangan membuka port 8060 ke internet tanpa authentication dan TLS.

## State dan rekonsiliasi

State ditulis atomik melalui file temporary lalu `os.replace` ke file yang ditentukan `STATE_FILE`.

State memuat:

- Mode bot.
- Saldo paper.
- Status dan candle terakhir setiap pair.
- Cooldown dan lockout.
- Entry window dan hysteresis RSI.
- Posisi terbuka.
- Pending order.
- OCO intent dan ID leg.
- Realized PnL dan riwayat transaksi bot.
- Snapshot equity/portfolio.
- Event log aplikasi.
- Status pause dan API rate-limit.

### Pending order

Jika hasil buy/sell/OCO tidak dapat dipastikan, bot menyimpan pending intent dan membekukan pair yang bersangkutan. Bot tidak melakukan retry otomatis agresif untuk order ambiguous.

Operator wajib memeriksa:

- Open orders.
- Order history.
- Fill dan partial fill.
- Saldo base/quote.
- ID order dan ID list OCO.

Baru kemudian lakukan rekonsiliasi state secara manual. Jangan menghapus pending intent hanya untuk melanjutkan trading jika state belum cocok dengan Binance.

### Pemisahan mode

State mencatat `bot_mode`:

- `dry`
- `testnet`
- `live`

Pergantian mode ditolak apabila mode state lama tidak sama. Ini mencegah posisi paper dicampur dengan live account atau sebaliknya.

## PnL dan portfolio

### Unrealized PnL

Untuk posisi bot:

```text
unrealized PnL = kuantitas × mark price - biaya posisi
```

Mark price mengikuti ticker market yang tersedia. Pada paper mode, mark price dihitung dari market/pool data bot.

### Realized PnL

Realized PnL dihitung dari exit yang dapat dicatat bot:

```text
PnL = hasil penjualan - bagian biaya posisi yang terjual
```

Hanya mencakup transaksi yang dikenali bot sejak tracking dimulai. Tidak mencakup sempurna:

- Transaksi manual.
- Deposit dan withdrawal.
- Trade sebelum `realized_tracking_since`.
- Fee yang dibayar dalam aset selain quote/base yang tidak dapat direkonsiliasi.

Dashboard menampilkan estimasi akuntansi bot, bukan laporan resmi/tax ledger Binance.

### Equity

Equity adalah estimasi mark-to-market dalam quote asset. Equity bukan saldo free yang tersedia untuk withdrawal atau posisi baru.

Aset yang tidak memiliki pasar langsung ke quote ditampilkan tetapi dikecualikan dari equity.

## Pengamanan API Binance

Bot menerapkan:

- CCXT `enableRateLimit: true`.
- Timeout REST 20 detik.
- Cache mark price sekitar 8–15 detik bergantung pemakaian.
- Snapshot portfolio maksimum sekali per 30 detik.
- Poll OCO minimum 30 detik per pair.
- State/API event tersimpan untuk dashboard.
- Hentian global saat exception terindikasi 429/418/rate-limit.
- `Retry-After` dipakai bila tersedia.
- Fallback pause konservatif digunakan bila header tidak tersedia.
- Tidak ada retry agresif untuk sell/order/OCO ambiguous.

Pengamanan ini tidak menjamin bebas ban/limit. Binance dapat mengubah endpoint weight, order/account limit, atau kebijakan IP kapan saja.

Best practice:

- Jalankan satu instance bot untuk satu state/account.
- Jangan menurunkan `LOOP_SECONDS` secara tidak perlu.
- Gunakan API IP whitelist.
- Hindari banyak proses private REST dari IP yang sama.
- Gunakan WebSocket untuk data market real-time ber-volume tinggi bila dikembangkan lebih lanjut.
- Jangan mengulang request tanpa backoff dan status rate-limit.
- Periksa response header/weight yang tersedia pada dashboard dan log.

## API key Binance

Gunakan kunci khusus dengan:

- Izin Spot & Margin Trading yang dibutuhkan/izin spot yang dibatasi.
- Withdrawal dimatikan.
- IP whitelist bila tersedia.
- Kunci testnet terpisah.

API key tidak boleh dibagikan atau disimpan di repository/public state.

## Backtest

```bash
python backtest.py --symbol NEARUSDT --days 90
python backtest.py --symbol NEARUSDT --days 180 --combos 30/30,35/40,40/35,40/40
python backtest.py --symbol NEARUSDT --days 180 --closed
```

Argument penting:

- `--symbol`: pair historis.
- `--days`: periode uji.
- `--warmup-days`: periode pemanasan indikator.
- `--combos`: kombinasi batas RSI 4H/1H.
- `--closed`: meniru konteks candle tertutup.
- `--data-csv`: memakai data CSV lokal.
- `--save-csv`: menyimpan data unduhan untuk repeat test.

Catatan:

- Entry diasumsikan pada close candle sinyal.
- SL dan TP yang bersentuhan dalam satu candle diperlakukan sebagai SL.
- Fee dan slippage dapat dikonfigurasi pada CLI.
- Backtest tidak memodelkan seluruh spread, partial fill, market impact, latency, downtime, dan kegagalan API.
- Hasil historis bukan jaminan performa masa depan.

## Test

```bash
pytest -q
```

Test mencakup:

- RSI naik monotonis.
- RSI flat.
- ATR true range menggunakan previous close.
- Penolakan integer config pecahan.
- Penolakan nilai numerik non-finite.

Test tidak mengirim order atau terhubung ke Binance. Passing test bukan bukti profit atau kompatibilitas live.

## Menjalankan di VPS

Gunakan virtual environment dan working directory repository yang sama. Tambahkan systemd/tmux hanya setelah mode DRY RUN dan testnet stabil.

Saat bot berhenti:

- Posisi open tidak otomatis dijual.
- Posisi dengan OCO tetap dapat dilindungi Binance.
- Posisi tanpa OCO tidak dipantau selama proses bot mati.

Oleh karena itu, OCO atau perlindungan exchange tetap penting untuk live trading.

Contoh tmux:

```bash
tmux new -s rsi-bot
source venv/bin/activate
python bot.py
```

Keluar: `Ctrl+B`, lalu `D`.

## Notifikasi Telegram

Bot dapat mengirim:

- Startup dan shutdown.
- Buy/sell.
- Take profit dan stop loss.
- Cooldown selesai.
- Kegagalan OCO.
- Pending/ambiguous order.
- Error dan rate-limit.

Telegram bersifat opsional dan tidak boleh menjadi satu-satunya sistem monitoring.

## Keterbatasan penting

- Konfigurasi/indikator bergantung pada candle, precision, dan respons exchange.
- Fill market, slippage, spread, dan perubahan harga saat eksekusi tidak dapat dihilangkan.
- OCO dapat ditolak bila pair/filter/quantity berubah.
- Partial fill dan transaksi manual dapat membuat state/PnL berbeda dari Binance.
- Dashboard adalah estimasi state dan balance, bukan laporan resmi exchange.
- Tidak ada jaminan profit atau audit eksternal.
- Bot spot adalah sistem long-only pada pair quote yang dikonfigurasi.
- Optimasi parameter dapat mengarah ke overfitting.
- Live trading memerlukan testnet, observasi, modal kecil, dan pemantauan manusia.

## Struktur repository

```text
bot.py          # Bot trading, indikator, broker, state, throttling API
backtest.py     # Backtest historis 15m dan fitur 1H/4H
dashboard.py    # Dashboard read-only lokal port 8060
env.example     # Template konfigurasi
tests/          # Unit test keselamatan dan indikator
requirements.txt
README.md
```

## Disclaimer

Pengguna bertanggung jawab atas API key, modal, konfigurasi, pengujian, keamanan server, kepatuhan regulasi, dan keputusan trading. Gunakan hanya pada akun dan pair yang telah diuji.
