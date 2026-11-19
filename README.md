# Bot Spot Multicoin Binance — RSI Oversold Reversal

Bot trading spot Binance untuk banyak pair sekaligus. Daftar pair diatur manual lewat `SYMBOLS` di `.env`.

## Logika

**Entry** (semua harus terpenuhi, dicek setiap candle 15m yang baru close):

1. RSI 4H < 30 dan RSI 1H < 30 (konteks oversold besar)
2. RSI 15m pernah < 30 dalam 12 candle terakhir, lalu candle close menembus naik ke atas 32

**Posisi**: 30% equity per trade (equity = saldo USDT bebas + nilai posisi bot yang masih terbuka).

**Exit**:
- Take profit dinamis 1–3%: `1.5 × ATR% 1H` saat entry, dibatasi 1%–3% (pasar tenang → ~1%, pasar bergejolak → mendekati 3%)
- Stop loss 2% → pair masuk status `cooldown` (default 240 menit)

**Anti beli beruntun saat sideways** (semuanya aktif bersamaan):
- Setelah trade (entry/exit), RSI 15m harus pulih ≥ `REARM_LEVEL` (50) dulu sebelum sinyal baru dianggap valid
- Lockout `REENTRY_LOCKOUT_MINUTES` (120) sejak entry/exit terakhir pada pair yang sama
- Maksimal `MAX_ENTRIES_PER_WINDOW` (2) entry per pair per `ENTRY_WINDOW_HOURS` (24)
- Satu posisi per pair, total posisi terbuka maksimal `MAX_OPEN_POSITIONS` (3)

**Telegram**: notifikasi start, BUY, TP, SL, cooldown selesai, dan error (dibatasi 1x/30 menit per jenis).

## Instalasi

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # lalu isi
python bot.py
```

## Urutan yang disarankan

1. `DRY_RUN=true` beberapa hari (saldo virtual, tidak ada order asli, tidak butuh API key)
2. `BINANCE_TESTNET=true` + `DRY_RUN=false` bila ingin menguji alur order sungguhan
3. Live dengan modal kecil

## API key Binance

- Aktifkan hanya **Spot & Margin Trading**, matikan Withdrawals
- Pakai IP whitelist (IP VPS)

## Menjalankan di VPS

```bash
tmux new -s rsi-bot
source venv/bin/activate && python bot.py
# keluar: Ctrl+B lalu D
```

Atau pakai systemd agar otomatis menyala setelah reboot. State tersimpan di `state.json`, jadi posisi tetap terpantau setelah bot restart.

## Catatan penting

- SL/TP dipantau oleh bot (cek harga tiap `LOOP_SECONDS`), **bukan** order OCO di sisi Binance. Bila bot/VPS mati saat posisi terbuka, tidak ada proteksi otomatis.
- Syarat entry sengaja ketat, jadi sinyal bisa jarang muncul (RSI 1H masih < 30 saat RSI 15m sudah menembus 32 itu kondisi yang jarang). Longgarkan lewat `RSI_1H_MAX` / `RSI_4H_MAX` bila perlu.
- Rasio risiko/imbalan: TP 1% vs SL 2% berarti win rate harus tinggi agar impas, apalagi dengan fee. Pantau hasil di mode DRY RUN dulu.
- Ini bukan nasihat keuangan; trading crypto berisiko kehilangan modal.
