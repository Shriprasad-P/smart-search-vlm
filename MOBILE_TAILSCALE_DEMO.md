# Smart Stack on Android via Tailscale

## Demo URL

`https://pranjals-macbook-air.tail32e467.ts.net/`

This address is private to devices/users allowed by the Tailscale tailnet. The
Smart Stack server itself listens only on `127.0.0.1`; it is not exposed to the
Wi-Fi LAN or public internet.

## Before the presentation

1. On the Mac, connect Tailscale and run:

   ```bash
   cd /Users/pranjal/garage/smart_stack
   ./run_mobile_tailscale.sh
   ```

2. The launcher automatically prevents Mac sleep until the stop script runs.
   Keep the Mac powered and connected to the internet.

3. On Android, open Tailscale, sign in to the same tailnet, and turn the VPN
   connection on.

4. In Chrome on Android, open the demo URL above. The header should show the
   indexed-photo count.

5. Warm up retrieval with a search such as `mountain road` before presenting.

## Mobile features

- **Search**: existing hybrid keyword + semantic retrieval and reranking.
- **Ask**: existing grounded multimodal chat, with short conversation history.
- **Photos**: browse indexed photos and focus chat on one selected photo.

The mobile gateway is deliberately read-only. It does not ingest, delete, move,
or re-index files, so the existing CLI and SmartStackUI workflows remain
unchanged.

## Quick checks

On the Mac:

```bash
curl http://127.0.0.1:8787/api/health
tailscale serve status
```

If Android cannot connect:

1. Confirm Tailscale says **Connected** on both the Mac and Android.
2. Confirm the Android device uses the same tailnet/account.
3. Re-run `./run_mobile_tailscale.sh` on the Mac.
4. Keep the Mac awake and connected to the internet.

## Stop after the demo

```bash
cd /Users/pranjal/garage/smart_stack
./stop_mobile_tailscale.sh
```

This stops only the Smart Stack mobile service and its HTTPS endpoint on port
443. It leaves the desktop app, database, indexes, and other Tailscale Serve
ports alone.
