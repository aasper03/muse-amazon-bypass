# Connect amazon-cart to Muse

Run the API on a machine Muse can SSH into, bound to localhost. Muse calls `http://127.0.0.1:8792` from that shell. A public hostname is only needed if you register the hosted custom-API connector later in this file.

## 1. Run the API on localhost

You need Python 3.11+ and `curl`.

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

Check on the same machine:

```bash
curl -sS http://127.0.0.1:8792/health
```

Expect `{"ok":true,"service":"amazon-cart"}`.

Cart calls also need Netscape cookie jars. Sign in with a normal browser. Amazon blocks automated login windows. Save the export as:

| Account | File |
|---------|------|
| personal | `~/.amazon-cart/cookies-personal.txt` |
| business | `~/.amazon-cart/cookies-business.txt` |

`chmod 600` both files. A business session includes a `b2b` cookie. A personal add is refused when that cookie is present, and a business add is refused when it is missing.

## 2. Call it over SSH

Tell Muse which host to SSH to, and that the API is already listening on `http://127.0.0.1:8792` there. Muse sends `Authorization: Bearer` using `AMAZON_API_KEY`. Keep the key out of chat. Put it in the environment on that host, or in whatever secret store you already use with Muse.

One key covers both carts. Pass `account=personal` or `account=business` on each cart call. `personal` and `business` are two saved browser sessions.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Liveness, no auth |
| GET | `/regions` | Marketplace codes |
| GET | `/search?query=…&max_results=10` | Search products |
| GET | `/products?query=…` | Alias for search |
| GET | `/product/{asin}` | Product details |
| GET | `/product/{asin}/variations` | Color, size, and style variants |
| GET | `/cart?account=personal` | Personal cart |
| GET | `/cart?account=business` | Business cart |
| GET | `/cart/add?asin=…&quantity=1&account=personal` | Add to the personal cart |
| GET | `/cart/add?asin=…&quantity=1&account=business` | Add to the business cart |
| POST | `/cart` | JSON body `{"asin","quantity","region","account"}` |

Search also accepts `region` (default `us`). Adding an item does not check out or pay. A missing login, or a jar for the wrong account, returns **HTTP 409**. An Amazon block page returns **HTTP 503**.

### Prompt you can paste to Muse

```text
Amazon search and cart is already running on the machine you SSH into.

Base URL: http://127.0.0.1:8792
Auth: Authorization: Bearer, using AMAZON_API_KEY from that host. Do not ask me to paste the key in chat.

Use GET /search?query=… to search, GET /product/{asin} for details, and GET /cart/add?asin=…&quantity=1&account=personal or account=business to add to that cart. GET /cart?account=personal and GET /cart?account=business read the carts. Adding does not purchase. HTTP 409 means that account is not signed in. HTTP 503 means Amazon blocked the fetch.
```

## 3. Optional: public HTTPS provider

`credentials.request_api_access` is a separate path. It mints a hosted link, you paste the API key once, and Muse stores it in the Secure Vault as `custom.<provider>`. That connector calls a bare public hostname over HTTPS. A Tailscale address, a raw IP, or `http://` is not a valid `api_hosts` value.

Put a reverse proxy in front of port 8792. Example Caddy site, replace `amazon.example.com`:

```caddy
amazon.example.com {
	encode gzip
	reverse_proxy 127.0.0.1:8792
}
```

Check from outside your machine:

```bash
curl -sS https://amazon.example.com/health
```

Tell Muse:

- **provider**: `amazon-products`
- **hostname**: `amazon.example.com` (your hostname, no `https://` and no path)
- **placement**: `bearer_header`

Muse should call `credentials.request_api_access` with:

- `provider`: `amazon-products`
- `api_hosts`: `["amazon.example.com"]`
- `auth_scheme`: `api_key`
- `placement`: `bearer_header`

Open the hosted link and paste the value of `AMAZON_API_KEY`. The key is not checked at setup. The first real API call is what confirms it.

`placement` cannot be edited later. A wrong value means a new `request_api_access` link and pasting the key again.

Use any lowercase provider slug you like. `amazon-products` matches the examples below. This key is separate from any other custom Muse provider.

Base URL for this path: `https://amazon.example.com`

### Prompt for the public connector

Replace the hostname, then send:

```text
Register a custom API provider for my Amazon search and cart service.

- provider: amazon-products
- api_hosts: ["amazon.example.com"]
- auth_scheme: api_key
- placement: bearer_header

Call credentials.request_api_access with those values. I will paste the Bearer key into the Secure Vault link. Do not ask me to put the key in chat.

After that, call https://amazon.example.com with Authorization: Bearer. Use GET /search?query=… to search, GET /product/{asin} for details, and GET /cart/add?asin=…&quantity=1&account=personal or account=business to add to that cart. GET /cart?account=personal and GET /cart?account=business read the carts. Adding does not purchase. HTTP 409 means that account is not signed in. HTTP 503 means Amazon blocked the fetch.
```
