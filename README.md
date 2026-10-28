# VPS Stock Monitor (BandwagonHost & DMIT)

24/7 lightweight restock monitor for BandwagonHost (`bwh81.net`) and DMIT (`dmit.io`) special plans.

## Monitored Plans (8 total)

### BandwagonHost (`bwh81.net`)
- `pid=157`: MEGABOX-PRO ($49/yr)
- `pid=156`: BIGGERBOX-PRO ($39/yr)
- `pid=94`: CN2 GIA-E 10G ($49.99/yr)
- `pid=147`: THE PLAN 2024 ($99/yr)

### DMIT (`dmit.io`)
- `pid=183`: LAX.Pro.WEE ($39.90/yr)
- `pid=186`: LAX.Pro.MALIBU ($49.90/yr)
- `pid=218`: LAX.EB.CORONA ($49.90/yr)
- `pid=188`: LAX.EB.WEE ($39.90/yr)

## Features
- **3-level stock classification**: `out_of_stock`, `invite_required`, and genuinely `in_stock` (purchasable without invite code).
- **Transient SSL/network retry**: Automatic exponential backoff retries on `requests.RequestException` (`SSLEOFError`, timeouts, etc.).
- **0-token background polling + Webhook trigger**: Only fires `POST $WEBHOOK_URL` with `Authorization: Bearer $WEBHOOK_KEY` when `purchasable_without_invite == True`.
