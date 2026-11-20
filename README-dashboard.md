# RSI Binance Dashboard (read-only)

Dashboard terpisah pada port 8060. File bot.py, strategi, .env, requirements.txt, state.json, order, OCO, dan Telegram tidak diubah.

## Fitur
- Equity Binance Spot dalam USDT, saldo free/used, dan aset akun yang bisa dinilai.
- Posisi dari state.json, mark price, nilai posisi, unrealized PnL, TP/SL, dan status OCO.
- Realized PnL FIFO dari fill akun pada pair SYMBOLS selama 90 hari.
- Win rate, riwayat exit, status service bot, status pair, dan log terakhir.
- Auto-refresh 10 detik; cache API 10 detik.
- Tidak ada endpoint untuk membuat atau membatalkan order.

## Deploy
Salin dashboard.py dan rsi-dashboard.service.example ke /opt/rsi-binance. Lalu:

    sudo cp /opt/rsi-binance/rsi-dashboard.service.example /etc/systemd/system/rsi-dashboard.service
    sudo systemctl daemon-reload
    sudo systemctl enable --now rsi-dashboard
    sudo systemctl status rsi-dashboard --no-pager

Service contoh bind ke 0.0.0.0:8060. Sebelum membuka port publik, batasi UFW ke IP tepercaya atau pasang reverse proxy HTTPS dengan autentikasi. Dashboard menampilkan saldo dan aktivitas akun. Untuk akses privat, ubah DASHBOARD_HOST ke 127.0.0.1 dan gunakan SSH tunnel.

Akses: http://IP-VPS:8060

## Batasan perhitungan
- Equity menjumlahkan saldo total aset yang dapat dinilai ke QUOTE_ASSET. Stablecoin umum diasumsikan setara 1 unit quote.
- Unrealized PnL hanya untuk posisi bot dalam state.json dan belum mengurangi estimasi fee keluar.
- Realized PnL memakai FIFO fill akun 90 hari, sehingga transaksi manual pada pair yang sama ikut terhitung. Buy sebelum jendela 90 hari tidak dapat dipasangkan, sehingga hasil historis bisa tidak lengkap.
- DRY_RUN tidak menyimpan ledger realized PnL.
- API key perlu izin baca akun/trade. Dashboard tidak membutuhkan izin trading dan tidak mengirim order.
- Hanya memakai dependency yang sudah tersedia: ccxt, python-dotenv, dan Python standard library.
