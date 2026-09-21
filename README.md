# Muse Amazon Shopping Bypass

This is a direct response to Amazon's block of AI agent shopping traffic on 9/20/2026.

Amazon product search and add-to-cart API for [Muse](https://muse.ai). Run it on the machine Muse can already SSH into, then point Muse at `http://127.0.0.1:8792`.

One connection covers two carts, `personal` and `business`. Each cart is a [Netscape cookie jar](https://curl.se/docs/http-cookies.html) exported from a normal browser. This package does not include anyone's cookies, hostname, or API key.

Search does not require a login. Add to cart does. Adding an item does not check out or pay.

## Set up

You need Python 3.11+ and `curl`.

```bash
python -m venv .venv
source .venv/bin/activate
pip install git+https://github.com/aasper03/muse-amazon-bypass.git
mkdir -p ~/.amazon-cart
chmod 700 ~/.amazon-cart
openssl rand -hex 32 > ~/.amazon-cart/api-key
chmod 600 ~/.amazon-cart/api-key
```

Sign in to Amazon in a normal browser. Amazon blocks automated login windows. Export cookies for each account (`chmod 600`):

| Account | File |
|---------|------|
| `personal` | `~/.amazon-cart/cookies-personal.txt` |
| `business` | `~/.amazon-cart/cookies-business.txt` |

A business session includes a `b2b` cookie. A personal add is refused when that cookie is present, and a business add is refused when it is missing.

```bash
export AMAZON_API_KEY="$(cat ~/.amazon-cart/api-key)"
export HOST=127.0.0.1
export PORT=8792
amazon-cart
```

`MUSE_API_KEY` is accepted as an alias of `AMAZON_API_KEY`.

Confirm the server is up:

```bash
curl -sS http://127.0.0.1:8792/health
```

Expect `{"ok":true,"service":"amazon-cart"}`.

## Use from Muse over SSH

When Muse has a shell on the machine running `amazon-cart`, it calls the API on localhost. Leave the process bound to `127.0.0.1`. A public hostname and TLS are not part of this setup.

Tell Muse the SSH host, the base URL `http://127.0.0.1:8792`, and to send `Authorization: Bearer` with `AMAZON_API_KEY`. Keep the key out of chat. A prompt you can paste is in [MUSE.md](MUSE.md).

After SSH, Muse can run:

```bash
curl -sS -H "Authorization: Bearer $AMAZON_API_KEY" \
  'http://127.0.0.1:8792/search?query=usb-c+cable&max_results=5'

curl -sS -H "Authorization: Bearer $AMAZON_API_KEY" \
  'http://127.0.0.1:8792/cart/add?asin=B000000000&quantity=1&account=personal'
```

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Liveness, no auth |
| GET | `/search?query=…` | Search products |
| GET | `/product/{asin}` | Product details |
| GET | `/product/{asin}/variations` | Variants |
| GET | `/cart?account=personal\|business` | Read that cart |
| GET | `/cart/add?asin=…&quantity=1&account=…` | Add to that cart |

`POST /cart` accepts `{"asin","quantity","region","account"}`. A missing login returns HTTP 409. An Amazon block page returns HTTP 503.

Use `account=personal` or `account=business` on cart routes. One key covers both carts.

## Optional: public HTTPS provider

Muse can also register this API with `credentials.request_api_access`. That connector stores `api_hosts` as a bare public hostname and calls it over HTTPS. Put TLS in front of port 8792 first. See [Caddyfile.example](Caddyfile.example) and the registration steps in [MUSE.md](MUSE.md).

For that connector, the hostname has no `https://` and no path. `placement` is `bearer_header` and cannot be changed later.

## Use without Muse

The same server is a normal HTTP API. Skip Muse and call it yourself with the Bearer key:

```bash
curl -sS -H "Authorization: Bearer $AMAZON_API_KEY" \
  'http://127.0.0.1:8792/search?query=usb-c+cable&max_results=5'
```

`X-Api-Key` is also accepted. Search accepts `max_results` (default 16) and `region` (default `us`): `us`, `uk`, `ca`, `de`, `fr`, `es`, `it`, `nl`, `jp`, `au`, `mx`, `in`, `ae`, `sa`, `ie`, `be`. `/regions` and `/products` are also available. `/products` is an alias for search.
