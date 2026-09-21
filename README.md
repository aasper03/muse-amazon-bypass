# amazon-cart

A small HTTPS API so [Muse](https://muse.ai) can search Amazon and add items to a cart. Muse has no field for a custom server URL. You run this service on a public hostname, then register it as a custom API provider with one Bearer key.

One Muse connection covers two carts, `personal` and `business`. Each cart is a [Netscape cookie jar](https://curl.se/docs/http-cookies.html) exported from a normal browser. This package does not include anyone's cookies, hostname, or API key.

Search does not require a login. Add to cart does. Adding an item does not check out or pay.

Full registration steps and a prompt you can paste to Muse are in [MUSE.md](MUSE.md).

## Set up for Muse

You need Python 3.11+, `curl`, and a public HTTPS name. Muse will not call a Tailscale address, a raw IP, or plain `http://`.

```bash
python -m venv .venv
source .venv/bin/activate
pip install git+https://github.com/aasper03/amazon-cart.git
mkdir -p ~/.amazon-cart
chmod 700 ~/.amazon-cart
openssl rand -hex 32 > ~/.amazon-cart/api-key
chmod 600 ~/.amazon-cart/api-key
```

```bash
export AMAZON_API_KEY="$(cat ~/.amazon-cart/api-key)"
export HOST=127.0.0.1
export PORT=8792
amazon-cart
```

`MUSE_API_KEY` is accepted as an alias of `AMAZON_API_KEY`.

Proxy port 8792 with your own TLS hostname. See [Caddyfile.example](Caddyfile.example). Then confirm from outside the machine:

```bash
curl -sS https://amazon.example.com/health
```

Sign in to Amazon in a normal browser. Amazon blocks automated login windows. Export cookies for each account (`chmod 600`):

| Account | File |
|---------|------|
| `personal` | `~/.amazon-cart/cookies-personal.txt` |
| `business` | `~/.amazon-cart/cookies-business.txt` |

A business session includes a `b2b` cookie. A personal add is refused when that cookie is present, and a business add is refused when it is missing.

Tell Muse:

- **provider**: `amazon-products`
- **hostname**: your public hostname, with no `https://` and no path
- **placement**: `bearer_header`

Muse should call `credentials.request_api_access` with `auth_scheme: api_key` and those values. Paste `AMAZON_API_KEY` into the hosted Secure Vault link. Do not put the key in chat. `placement` cannot be changed later.

After that, Muse calls `https://your-hostname` with `Authorization: Bearer`. Use `account=personal` or `account=business` on cart routes. One key is enough for both.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Liveness, no auth |
| GET | `/search?query=…` | Search products |
| GET | `/product/{asin}` | Product details |
| GET | `/product/{asin}/variations` | Variants |
| GET | `/cart?account=personal\|business` | Read that cart |
| GET | `/cart/add?asin=…&quantity=1&account=…` | Add to that cart |

`POST /cart` accepts `{"asin","quantity","region","account"}`. A missing login returns HTTP 409. An Amazon block page returns HTTP 503.

## Use without Muse

The same server is a normal HTTP API. Skip provider registration and call it yourself with the Bearer key:

```bash
curl -sS -H "Authorization: Bearer $AMAZON_API_KEY" \
  'http://127.0.0.1:8792/search?query=usb-c+cable&max_results=5'

curl -sS -H "Authorization: Bearer $AMAZON_API_KEY" \
  'http://127.0.0.1:8792/cart/add?asin=B000000000&quantity=1&account=personal'
```

`X-Api-Key` is also accepted. Bind to `127.0.0.1` if you only want local use. Search accepts `max_results` (default 16) and `region` (default `us`): `us`, `uk`, `ca`, `de`, `fr`, `es`, `it`, `nl`, `jp`, `au`, `mx`, `in`, `ae`, `sa`, `ie`, `be`. `/regions` and `/products` are also available. `/products` is an alias for search.
