# RSI Binance Dashboard (read-only)

Dashboard terpisah pada port 8060. File bot.py, strategi, .env, requirements.txt, state.json, order, OCO, dan Telegram tidak diubah.

## Fitur
- Equity Binance Spot dalam USDT, saldo free/used, dan aset akun yang bisa dinilai.
- Posisi dari state.json, mark price, nilai posisi, unrealized PnL, TP/SL, dan status OCO.
- Realized PnL FIFO dari fill akun pada pair SYMBOLS selama 90 hari.
- Win rate, riwayat exit, status service bot, status pair, dan log terakhir.
- Auto-refresh 10 detik; cache API 10 detik.
- Tidak ada endpoint untuk membuat atau membatalkan order.

## Deploy di VPS

Pastikan perubahan dashboard diambil ke branch terpisah. Perintah ini tidak mengubah branch main:

    cd /opt/rsi-binance
    git fetch origin
    git checkout feature/dashboard-8060
    git pull origin feature/dashboard-8060

Pasang service dashboard terpisah:

    sudo cp /opt/rsi-binance/rsi-dashboard.service.example /etc/systemd/system/rsi-dashboard.service
    sudo systemctl daemon-reload
    sudo systemctl enable --now rsi-dashboard
    sudo systemctl status rsi-dashboard --no-pager

Service contoh sengaja bind ke 127.0.0.1:8060 agar saldo akun tidak terekspos langsung ke internet. Untuk membukanya dari komputer lokal, jalankan SSH tunnel dari komputer lokal:

    ssh -L 8060:127.0.0.1:8060 ubuntu@IP-VPS

Lalu buka http://127.0.0.1:8060 di browser lokal. Jangan bind ke 0.0.0.0 atau membuka port publik tanpa firewall allowlist dan autentikasi HTTPS.

## Batasan perhitungan
- Equity menjumlahkan saldo total aset yang dapat dinilai ke QUOTE_ASSET. Stablecoin umum diasumsikan setara 1 unit quote.
- Unrealized PnL hanya untuk posisi bot dalam state.json dan belum mengurangi estimasi fee keluar.
- Realized PnL memakai FIFO fill akun 90 hari, sehingga transaksi manual pada pair yang sama ikut terhitung. Buy sebelum jendela 90 hari tidak dapat dipasangkan, sehingga hasil historis bisa tidak lengkap.
- DRY_RUN tidak menyimpan ledger realized PnL.
- API key perlu izin baca akun/trade. Dashboard tidak membutuhkan izin trading dan tidak mengirim order.
- Hanya memakai dependency yang sudah tersedia: ccxt, python-dotenv, dan Python standard library.
