# amazon-cart

HTTP API to search Amazon and add a product to a signed-in cart. One process can use two accounts, `personal` and `business`. Each account is a [Netscape cookie jar](https://curl.se/docs/http-cookies.html) you export from a normal browser. This package does not ship anyone's cookies, hostnames, or API keys.

Search does not require a login. Add to cart does. Adding an item does not check out or pay.

## Install

Requires Python 3.11+ and `curl` on `PATH`.

```bash
python -m venv .venv
source .venv/bin/activate
pip install .
cp .env.example .env
# put a long random value in AMAZON_API_KEY
```

From Git:

```bash
pip install git+https://github.com/aasper03/amazon-cart.git
```

## Run

```bash
set -a
source .env
set +a
amazon-cart
```

The server listens on `127.0.0.1:8792` unless `HOST` and `PORT` are set. Put your own HTTPS reverse proxy in front of it if you want it on the public internet. Keep the API key secret.

```bash
curl -sS http://127.0.0.1:8792/health
curl -sS -H "Authorization: Bearer $AMAZON_API_KEY" \
  'http://127.0.0.1:8792/search?query=usb-c+cable&max_results=5'
```

## Cookie jars

Cart calls read:

| Account | Default file |
|---------|----------------|
| `personal` | `~/.amazon-cart/cookies-personal.txt` |
| `business` | `~/.amazon-cart/cookies-business.txt` |

Override the directory with `AMAZON_COOKIE_DIR`, or either file with `AMAZON_PERSONAL_COOKIES` and `AMAZON_BUSINESS_COOKIES`.

Sign in with a normal browser. Amazon blocks automated login windows. Export that browser's Amazon cookies to a Netscape cookie file and save it at the path above (`chmod 600`). A business account session usually includes a `b2b` cookie. A personal add is refused if that cookie is present, and a business add is refused if it is missing, so one account cannot write into the other cart.

```bash
curl -sS -H "Authorization: Bearer $AMAZON_API_KEY" \
  'http://127.0.0.1:8792/cart?account=personal'

curl -sS -H "Authorization: Bearer $AMAZON_API_KEY" \
  'http://127.0.0.1:8792/cart/add?asin=B000000000&quantity=1&account=business'
```

`POST /cart` accepts JSON: `{"asin","quantity","region","account"}`.

`account` is `personal` or `business`. The default is `personal`. A missing login returns HTTP 409.

## Endpoints

| Method | Path | Auth |
|--------|------|------|
| GET | `/health` | no |
| GET | `/regions` | yes |
| GET | `/search?query=…` | yes |
| GET | `/products?query=…` | yes |
| GET | `/product/{asin}` | yes |
| GET | `/product/{asin}/variations` | yes |
| GET | `/cart?account=personal\|business` | yes |
| GET | `/cart/add?asin=…&quantity=1&account=…` | yes |
| POST | `/cart` | yes |

Search also accepts `max_results` (default 16) and `region` (default `us`). Region codes: `us`, `uk`, `ca`, `de`, `fr`, `es`, `it`, `nl`, `jp`, `au`, `mx`, `in`, `ae`, `sa`, `ie`, `be`.

Send the key as `Authorization: Bearer <AMAZON_API_KEY>` or `X-Api-Key`.

If Amazon serves a block page, the API returns HTTP 503.
