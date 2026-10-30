# Core binary

This directory holds the Blocknet core binary at runtime. The binary is NOT
committed — download the daemon-only release matching your chain data:

```bash
# pick the version that matches data/blocknet's chain db (0.22.0 at writing)
curl -LO https://github.com/blocknetprivacy/core/releases/download/v0.22.0/blocknet-core-amd64-linux-0.22.0.zip
curl -LO https://github.com/blocknetprivacy/core/releases/download/v0.22.0/SHA256SUMS.txt
sha256sum -c SHA256SUMS.txt --ignore-missing   # verify before running
python3 -m zipfile -e blocknet-core-amd64-linux-0.22.0.zip .
chmod +x blocknet-core-amd64-linux
```

Run it headless (see docs/INSTALL.md):

```bash
./blocknet-core-amd64-linux --api 127.0.0.1:18512 --data ../data/blocknet &
```

Keep your wallet files (`shop.wallet.dat/.pw`) OUTSIDE git — they are ignored
by design. Releases: https://github.com/blocknetprivacy/core
