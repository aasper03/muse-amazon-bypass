#!/usr/bin/env python3
"""Amazon product search and cart API.

Search and product pages are fetched with curl. Cart actions use a Netscape
cookie jar exported from a normal browser session for each Amazon account.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import re
import secrets
from html import unescape
from typing import Any
from urllib.parse import quote_plus, unquote, urlencode, urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

API_KEY = (os.environ.get("AMAZON_API_KEY") or os.environ.get("API_KEY") or "").strip()
DEFAULT_REGION = os.environ.get("AMAZON_DEFAULT_REGION", "us").strip().lower() or "us"

REGIONS: dict[str, str] = {
    "us": "amazon.com",
    "uk": "amazon.co.uk",
    "ca": "amazon.ca",
    "de": "amazon.de",
    "fr": "amazon.fr",
    "es": "amazon.es",
    "it": "amazon.it",
    "nl": "amazon.nl",
    "jp": "amazon.co.jp",
    "au": "amazon.com.au",
    "mx": "amazon.com.mx",
    "in": "amazon.in",
    "ae": "amazon.ae",
    "sa": "amazon.sa",
    "ie": "amazon.ie",
    "be": "amazon.com.be",
}

ASIN_RE = re.compile(r"\b([A-Z0-9]{10})\b")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

app = FastAPI(
    title="Amazon Cart",
    version="0.1.0",
    description="Search Amazon products and add them to a signed-in cart.",
)
_bearer = HTTPBearer(auto_error=False)
_fetch_lock = asyncio.Lock()


def _cookie_dir() -> str:
    return os.environ.get(
        "AMAZON_COOKIE_DIR",
        os.path.join(os.path.expanduser("~"), ".amazon-cart"),
    )


_COOKIE = os.path.join(_cookie_dir(), "cookies-personal.txt")
_jar_var: contextvars.ContextVar[str] = contextvars.ContextVar("amazon_cookie_jar", default="")


def _active_jar() -> str:
    return _jar_var.get() or _COOKIE


def _jar_for_account(account: str) -> tuple[str, str]:
    key = (account or "personal").strip().lower().replace("_", "-")
    folder = _cookie_dir()
    if key in ("personal", "default", ""):
        return "personal", os.environ.get("AMAZON_PERSONAL_COOKIES", os.path.join(folder, "cookies-personal.txt"))
    if key in ("business", "prime-business", "primebusiness"):
        return "business", os.environ.get("AMAZON_BUSINESS_COOKIES", os.path.join(folder, "cookies-business.txt"))
    raise HTTPException(status_code=400, detail="account must be personal or business")


def _jar_is_business() -> bool:
    try:
        text = open(_active_jar(), encoding="utf-8", errors="replace").read()
    except OSError:
        return False
    return "\tb2b\t" in text


def _require_key(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    if not API_KEY:
        raise HTTPException(status_code=503, detail="API key not configured")
    provided = ""
    if creds and creds.credentials:
        provided = creds.credentials.strip()
    if not provided:
        raw = (request.headers.get("authorization") or "").strip()
        if raw.lower().startswith("bearer "):
            provided = raw[7:].strip()
        else:
            provided = raw
    if not provided:
        provided = (request.headers.get("x-api-key") or "").strip()
    if not provided or not secrets.compare_digest(provided, API_KEY):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _domain(region: str) -> str:
    return REGIONS.get(region.lower(), REGIONS[DEFAULT_REGION])


def _normalize_asin(value: str) -> str:
    raw = (value or "").strip()
    if re.fullmatch(r"[A-Za-z0-9]{10}", raw):
        return raw.upper()
    m = re.search(r"/(?:dp|gp/product|product)/([A-Za-z0-9]{10})", raw)
    if m:
        return m.group(1).upper()
    m = ASIN_RE.search(raw.upper())
    if m:
        return m.group(1)
    raise HTTPException(status_code=400, detail=f"Invalid ASIN or product URL: {value}")


def _textify(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _page_title(html: str) -> str:
    m = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
    return _textify(m.group(1)) if m else ""


def _is_blocked(status: int, url: str, html: str) -> bool:
    title = _page_title(html).lower()
    url_l = url.lower()
    sample = html[:8000].lower()
    has_products = (
        'data-component-type="s-search-result"' in html or 'id="productTitle"' in html
    )
    if status in (429, 503) and not has_products:
        return True
    if "sorry! something went wrong" in title:
        return True
    if any(token in url_l for token in ("/captcha", "validatecaptcha")):
        return True
    if "cs_503_logo" in html or "dogs of amazon" in sample:
        return True
    if "captchacharacters" in sample or 'id="captchacharacters"' in sample:
        return True
    return False


def _is_missing_product(html: str) -> bool:
    title = _page_title(html).lower()
    sample = html[:12000].lower()
    return (
        "page not found" in title
        or "couldn't find that page" in sample
        or "we couldn't find that page" in sample
        or "looking for something" in title
    )


def _interstitial_payload(html: str) -> dict[str, Any] | None:
    """Akamai interstitial: pow is `i + Number("aaaa" + "bbbb")`, posted with bm-verify."""
    if "triggerInterstitialChallenge" not in html and "/_sec/verify" not in html:
        return None
    stamp = re.search(r"var i = (\d+);", html)
    parts = re.search(r'Number\("(\d+)"\s*\+\s*"(\d+)"\)', html)
    token = re.search(r'"bm-verify"\s*:\s*"([^"]+)"', html)
    if not (stamp and parts and token):
        return None
    return {
        "bm-verify": token.group(1),
        "pow": int(stamp.group(1)) + int(parts.group(1) + parts.group(2)),
    }


async def _curl(
    url: str,
    body: str | None = None,
    content_type: str = "application/json",
    headers: list[str] | None = None,
) -> tuple[int, str]:
    jar = _active_jar()
    os.makedirs(os.path.dirname(jar) or ".", exist_ok=True)
    if not os.path.exists(jar):
        open(jar, "a").close()
        os.chmod(jar, 0o600)
    cmd = [
        "curl",
        "-sS",
        "-L",
        "--compressed",
        "--max-time",
        "25",
        "-A",
        HEADERS["User-Agent"],
        "-b",
        jar,
        "-c",
        jar,
        "-w",
        "\n__HTTP__%{http_code}",
    ]
    for header in headers or []:
        cmd += ["-H", header]
    if body is not None:
        cmd += ["-X", "POST", "-H", f"Content-Type: {content_type}", "--data", body]
    cmd.append(url)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode("utf-8", "replace").strip()
        raise HTTPException(
            status_code=503,
            detail=f"Amazon fetch failed: {err or proc.returncode}",
        )
    raw = stdout.decode("utf-8", "replace")
    if "\n__HTTP__" not in raw:
        raise HTTPException(status_code=503, detail="Amazon fetch returned no status")
    html, status_s = raw.rsplit("\n__HTTP__", 1)
    return int(status_s.strip() or "0"), html


async def _fetch_html_unlocked(url: str) -> str:
    """curl, not Python HTTP: Amazon 503s httpx/Playwright TLS fingerprints from this VPS."""
    status, html = await _curl(url)
    challenge = _interstitial_payload(html)
    if challenge:
        origin = "{0.scheme}://{0.netloc}".format(urlsplit(url))
        await _curl(
            f"{origin}/_sec/verify?provider=interstitial",
            body=json.dumps(challenge),
        )
        status, html = await _curl(url)
        challenge = _interstitial_payload(html)
    if challenge or _is_blocked(status, url, html):
        raise HTTPException(
            status_code=503,
            detail=f"Amazon blocked the request (HTTP {status}) at {url}. Retry later.",
        )
    return html


async def _fetch_html(url: str) -> str:
    async with _fetch_lock:
        return await _fetch_html_unlocked(url)


def _session_signed_in() -> bool:
    try:
        text = open(_active_jar(), encoding="utf-8", errors="replace").read()
    except OSError:
        return False
    return any(name in text for name in ("\tat-main\t", "\tsess-at-main\t", "\tx-main\t"))


def _atc_request(html: str, quantity: int) -> tuple[str, str, str]:
    form_m = re.search(r'<form[^>]*id="addToCart"[^>]*>(.*?)</form>', html, re.S)
    if not form_m:
        raise HTTPException(
            status_code=409,
            detail="This product has no Add to Cart button. Pick a variation or check availability.",
        )
    form = form_m.group(1)
    action_m = re.search(r'formaction="(/cart/add-to-cart/[^"]+)"', form)
    if not action_m:
        action_m = re.search(r'<form[^>]*id="addToCart"[^>]*action="([^"]+)"', html)
    action = unescape(action_m.group(1)) if action_m else "/cart/add-to-cart/ref=dp_start-bbf_1_glance"
    fields: list[tuple[str, str]] = []
    csrf = ""
    for inp in re.findall(r"<input\b[^>]*>", form, re.S):
        name_m = re.search(r'name="([^"]+)"', inp)
        if not name_m:
            continue
        name = name_m.group(1)
        typ_m = re.search(r'type="([^"]+)"', inp)
        typ = (typ_m.group(1) if typ_m else "hidden").lower()
        if typ in ("submit", "button", "checkbox", "radio", "image"):
            continue
        if name in ("submit.buy-now", "isBuyNow"):
            continue
        val_m = re.search(r'value="([^"]*)"', inp)
        value = unescape(val_m.group(1)) if val_m else ""
        if "%" in value:
            value = unquote(value)
        if name == "anti-csrftoken-a2z" and not csrf:
            csrf = value
        if name == "items[0.base][quantity]":
            value = str(quantity)
        fields.append((name, value))
    if not any(name == "items[0.base][quantity]" for name, _ in fields):
        fields.append(("items[0.base][quantity]", str(quantity)))
    fields.append(("submit.add-to-cart", "Add to cart"))
    if not csrf:
        raise HTTPException(status_code=503, detail="Amazon page had no add-to-cart token.")
    return action, csrf, urlencode(fields)


def _parse_cart(html: str, domain: str) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    chunks = re.split(r'(?=<div[^>]*class="[^"]*sc-list-item)', html)
    for chunk in chunks:
        if "sc-list-item" not in chunk[:300]:
            continue
        card = chunk[:25000]
        asin_m = re.search(r'data-asin="([A-Z0-9]{10})"', card) or re.search(
            r"/(?:dp|gp/product)/([A-Z0-9]{10})", card
        )
        title_m = re.search(r'class="a-truncate-full[^"]*"[^>]*>(.*?)</span>', card, re.S)
        if not title_m:
            continue
        title = _textify(title_m.group(1))
        if not title or title.lower().startswith("in "):
            continue
        asin = asin_m.group(1) if asin_m else ""
        key = asin or title
        if key in seen:
            continue
        seen.add(key)
        qty_m = re.search(
            r'<input[^>]*name="quantityBox"[^>]*value="(\d+)"', card
        ) or re.search(
            r'<input[^>]*value="(\d+)"[^>]*name="quantityBox"', card
        )
        price_m = re.search(r'class="a-offscreen">([^<]+)', card)
        items.append(
            {
                "asin": asin or None,
                "title": title,
                "quantity": int(qty_m.group(1)) if qty_m else 1,
                "price": unescape(price_m.group(1)).strip() if price_m else None,
                "url": f"https://www.{domain}/dp/{asin}" if asin else None,
            }
        )
    subtotal = None
    item_count = None
    label_m = re.search(
        r'id="sc-subtotal-label-buybox"[^>]*>\s*([^<]+)', html
    )
    amount_m = re.search(
        r'id="sc-subtotal-amount-buybox".*?sc-white-space-nowrap">\s*([^<]+)',
        html,
        re.S,
    )
    if label_m:
        count_m = re.search(r"(\d+)", label_m.group(1))
        item_count = int(count_m.group(1)) if count_m else None
    if amount_m:
        subtotal = unescape(amount_m.group(1)).strip()
    return {
        "signed_in": _session_signed_in(),
        "item_count": item_count if item_count is not None else len(items),
        "subtotal": subtotal,
        "items": items,
        "cart_url": f"https://www.{domain}/gp/cart/view.html",
    }


def _parse_search(html: str, domain: str) -> list[dict[str, Any]]:
    chunks = re.split(
        r'(?=<div[^>]*data-component-type="s-search-result")',
        html,
    )
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for chunk in chunks:
        if 'data-component-type="s-search-result"' not in chunk[:500]:
            continue
        card = chunk[:40000]
        asin_m = re.search(r"amzn1\.asin(?:\.\d+)?\.([A-Z0-9]{10})", card)
        if not asin_m:
            asin_m = re.search(r'data-asin="([A-Z0-9]{10})"', card)
        if not asin_m:
            continue
        asin = asin_m.group(1)
        if asin in seen:
            continue
        skip_titles = {"results", "more results", "skip to", "featured from amazon brands"}
        title_parts: list[str] = []
        for h2 in re.findall(r"<h2\b[^>]*>(.*?)</h2>", card, re.S):
            text = _textify(h2)
            if not text or text.lower() in skip_titles:
                continue
            title_parts.append(text)
        title = " ".join(title_parts)
        if len(title) < 3:
            alt = re.search(r'alt="([^"]{8,300})"', card)
            title = _textify(alt.group(1)) if alt else ""
            title = re.sub(r"^Sponsored Ad\s*-\s*", "", title).strip()
        if len(title) < 3:
            continue
        seen.add(asin)
        price_m = re.search(r'class="a-offscreen">([^<]+)', card)
        price = unescape(price_m.group(1)).strip() if price_m else None
        rating = None
        rating_m = re.search(r"([0-9.]+) out of 5 stars", card)
        if rating_m:
            rating = float(rating_m.group(1))
        review_count = None
        review_m = re.search(
            r"out of 5 stars by ([\d,]+) review", card, re.I
        ) or re.search(r'aria-label="([\d,]+)\s+ratings?"', card, re.I)
        if review_m:
            review_count = int(review_m.group(1).replace(",", ""))
        image_m = re.search(r'<img[^>]*class="s-image"[^>]*src="([^"]+)"', card)
        if not image_m:
            image_m = re.search(r'src="(https://m\.media-amazon\.com/images/[^"]+)"', card)
        image_url = unescape(image_m.group(1)) if image_m else None
        sponsored = (
            "puis-sponsored" in card
            or "Sponsored Ad" in card
            or "s-sponsored-label-text" in card
            or ">Sponsored<" in card
        )
        is_prime = "a-icon-prime" in card or "Amazon Prime" in card
        results.append(
            {
                "asin": asin,
                "title": title,
                "url": f"https://www.{domain}/dp/{asin}",
                "price": price,
                "rating": rating,
                "review_count": review_count,
                "is_prime": is_prime,
                "image_url": image_url,
                "sponsored": sponsored,
            }
        )
    return results


def _parse_product(html: str, asin: str, domain: str) -> dict[str, Any]:
    if 'id="productTitle"' not in html and _is_missing_product(html):
        raise HTTPException(status_code=404, detail=f"Amazon has no product page for {asin}")
    title_m = re.search(
        r'id="productTitle"[^>]*>(.*?)</span>', html, re.S
    )
    title = _textify(title_m.group(1)) if title_m else f"ASIN {asin}"
    # Price nearest the buy box, not an earlier widget.
    price = None
    list_price = None
    price_region = html
    marker = html.find('id="corePrice')
    if marker < 0:
        marker = html.find("apex-pricetopay-value")
    if marker >= 0:
        price_region = html[marker : marker + 4000]
    price_m = re.search(r'class="a-offscreen">([^<]+)', price_region)
    if price_m:
        price = unescape(price_m.group(1)).strip()
    list_m = re.search(
        r'class="a-text-price"[^>]*>.*?class="a-offscreen">([^<]+)',
        price_region,
        re.S,
    )
    if list_m:
        list_price = unescape(list_m.group(1)).strip()
    rating = None
    rating_m = re.search(
        r'id="acrPopover"[^>]*title="([^"]+)"', html
    ) or re.search(r"([0-9.]+) out of 5 stars", html[marker if marker > 0 else 0 : (marker if marker > 0 else 0) + 20000])
    if rating_m:
        rm = re.search(r"([0-9.]+)", rating_m.group(1))
        if rm:
            rating = float(rm.group(1))
    review_count = None
    review_m = re.search(
        r'id="acrCustomerReviewText"[^>]*aria-label="([^"]+)"', html
    ) or re.search(r'id="acrCustomerReviewText"[^>]*>(.*?)</span>', html, re.S)
    if review_m:
        digits = re.search(r"([\d,]+)", review_m.group(1))
        if digits:
            review_count = int(digits.group(1).replace(",", ""))
    availability = None
    avail_m = re.search(
        r'primary-availability-message[^>]*>(.*?)</span>', html, re.S
    )
    if avail_m:
        availability = _textify(avail_m.group(1)) or None
    brand = None
    brand_m = re.search(r'<a[^>]*id="bylineInfo"[^>]*>(.*?)</a>', html, re.S)
    if brand_m:
        brand = _textify(brand_m.group(1)) or None
        brand = re.sub(r"^Visit the\s+", "", brand)
        brand = re.sub(r"\s+Store$", "", brand) or brand
    features: list[str] = []
    bullets_m = re.search(r'id="feature-bullets"(.*?)</ul>', html, re.S)
    if bullets_m:
        for item in re.findall(r"<span[^>]*>(.*?)</span>", bullets_m.group(1), re.S):
            text = _textify(item)
            if text and not re.fullmatch(r"about this item", text, re.I):
                features.append(text)
        # de-dupe while preserving order
        deduped: list[str] = []
        for text in features:
            if text not in deduped:
                deduped.append(text)
        features = deduped[:12]
    image_urls: list[str] = []
    landing = re.search(r'id="landingImage"[^>]*data-a-dynamic-image="([^"]+)"', html)
    if landing:
        try:
            raw = unescape(landing.group(1))
            image_urls = list(json.loads(raw).keys())[:8]
        except (json.JSONDecodeError, TypeError):
            image_urls = []
    if not image_urls:
        src = re.search(r'id="landingImage"[^>]*src="([^"]+)"', html)
        if src:
            image_urls = [unescape(src.group(1))]
    categories: list[str] = []
    crumbs = re.search(
        r'id="wayfinding-breadcrumbs_feature_div"(.*?)</ul>', html, re.S
    )
    if crumbs:
        categories = [
            _textify(a)
            for a in re.findall(r"<a[^>]*>(.*?)</a>", crumbs.group(1), re.S)
        ]
        categories = [c for c in categories if c]
    is_prime = "a-icon-prime" in html or 'id="primeBadge_feature_div"' in html
    if title == f"ASIN {asin}" and not price and not features:
        raise HTTPException(
            status_code=503,
            detail=f"Amazon returned an unreadable product page for {asin}.",
        )
    return {
        "asin": asin,
        "title": title,
        "url": f"https://www.{domain}/dp/{asin}",
        "price": price,
        "list_price": list_price,
        "rating": rating,
        "review_count": review_count,
        "availability": availability,
        "brand": brand,
        "features": features,
        "image_urls": image_urls,
        "categories": categories,
        "is_prime": is_prime,
    }


def _parse_variations(html: str, asin: str, domain: str) -> list[dict[str, Any]]:
    variations: list[dict[str, Any]] = []
    blob = re.search(
        r'"dimensionValuesDisplayData"\s*:\s*(\{.*?\})\s*,\s*"',
        html,
        re.S,
    )
    labels: list[str] = []
    label_m = re.search(r'"dimensionsDisplay"\s*:\s*(\[[^\]]*\])', html)
    if label_m:
        try:
            labels = json.loads(label_m.group(1))
        except json.JSONDecodeError:
            labels = []
    if blob:
        try:
            data = json.loads(blob.group(1))
        except json.JSONDecodeError:
            data = {}
        for var_asin, dims in data.items():
            if not re.fullmatch(r"[A-Z0-9]{10}", var_asin):
                continue
            dim_map = {}
            if isinstance(dims, list):
                for i, value in enumerate(dims):
                    key = labels[i] if i < len(labels) else f"option_{i+1}"
                    dim_map[str(key)] = value
            title = " / ".join(str(v) for v in dims) if isinstance(dims, list) else None
            variations.append(
                {
                    "asin": var_asin,
                    "title": title,
                    "dimensions": dim_map,
                    "price": None,
                    "available": True,
                    "url": f"https://www.{domain}/dp/{var_asin}",
                }
            )
    if not variations:
        title_m = re.search(r'id="productTitle"[^>]*>(.*?)</span>', html, re.S)
        variations.append(
            {
                "asin": asin,
                "title": _textify(title_m.group(1)) if title_m else None,
                "dimensions": {},
                "price": None,
                "available": True,
                "url": f"https://www.{domain}/dp/{asin}",
            }
        )
    return variations


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "service": "amazon-cart"}


@app.get("/regions", dependencies=[Depends(_require_key)])
async def list_regions() -> dict[str, Any]:
    return {"regions": sorted(REGIONS.keys()), "default": DEFAULT_REGION}


class SearchResponse(BaseModel):
    query: str
    region: str
    page: int
    result_count: int
    results: list[dict[str, Any]]


@app.get("/search", response_model=SearchResponse, dependencies=[Depends(_require_key)])
async def search(
    query: str = Query(..., min_length=1),
    region: str = Query(DEFAULT_REGION),
    page: int = Query(1, ge=1, le=20),
    max_results: int = Query(16, ge=1, le=48),
    include_sponsored: bool = Query(False),
) -> SearchResponse:
    domain = _domain(region)
    url = f"https://www.{domain}/s?k={quote_plus(query.strip())}"
    if page > 1:
        url += f"&page={page}"
    html = await _fetch_html(url)
    parsed = _parse_search(html, domain)
    results = parsed if include_sponsored else [r for r in parsed if not r.get("sponsored")]
    if not results:
        lowered = html.lower()
        if "no results for" in lowered or "did not match any products" in lowered:
            results = []
        elif not parsed:
            raise HTTPException(
                status_code=503,
                detail="Amazon returned no parseable products. Likely a block page.",
            )
        else:
            # Every card was sponsored; return them rather than a fake empty search.
            results = parsed
    results = results[:max_results]
    return SearchResponse(
        query=query.strip(),
        region=region.lower(),
        page=page,
        result_count=len(results),
        results=results,
    )


@app.get("/product/{asin}", dependencies=[Depends(_require_key)])
async def get_product(
    asin: str,
    region: str = Query(DEFAULT_REGION),
) -> dict[str, Any]:
    asin_n = _normalize_asin(asin)
    domain = _domain(region)
    html = await _fetch_html(f"https://www.{domain}/dp/{asin_n}")
    return _parse_product(html, asin_n, domain)


@app.get("/product/{asin}/variations", dependencies=[Depends(_require_key)])
async def get_variations(
    asin: str,
    region: str = Query(DEFAULT_REGION),
) -> dict[str, Any]:
    asin_n = _normalize_asin(asin)
    domain = _domain(region)
    html = await _fetch_html(f"https://www.{domain}/dp/{asin_n}")
    variations = _parse_variations(html, asin_n, domain)
    return {
        "asin": asin_n,
        "region": region.lower(),
        "variation_count": len(variations),
        "variations": variations,
    }


class AddToCartBody(BaseModel):
    asin: str
    quantity: int = 1
    region: str = DEFAULT_REGION
    account: str = "personal"


def _select_account(account: str) -> str:
    label, jar = _jar_for_account(account)
    _jar_var.set(jar)
    if not _session_signed_in():
        raise HTTPException(
            status_code=409,
            detail=(
                f"The {label} Amazon account is not signed in. "
                "Sign in to that account in the login window on this machine, then retry."
            ),
        )
    business = _jar_is_business()
    if label == "personal" and business:
        raise HTTPException(
            status_code=409,
            detail="Refusing to add to personal: this session is the Amazon Business account.",
        )
    if label == "business" and not business:
        raise HTTPException(
            status_code=409,
            detail="Refusing to add to business: this session is not the Amazon Business account.",
        )
    return label


async def _add_to_cart(asin: str, quantity: int, region: str, account: str) -> dict[str, Any]:
    if quantity < 1 or quantity > 30:
        raise HTTPException(status_code=400, detail="quantity must be between 1 and 30")
    label = _select_account(account)
    asin_n = _normalize_asin(asin)
    domain = _domain(region)
    product_url = f"https://www.{domain}/dp/{asin_n}"
    async with _fetch_lock:
        html = await _fetch_html_unlocked(product_url)
        action, csrf, body = _atc_request(html, quantity)
        origin = f"https://www.{domain}"
        post_url = action if action.startswith("http") else origin + action
        status, result = await _curl(
            post_url,
            body=body,
            content_type="application/x-www-form-urlencoded",
            headers=[
                f"Origin: {origin}",
                f"Referer: {product_url}",
                f"anti-csrftoken-a2z: {csrf}",
            ],
        )
    if _is_blocked(status, post_url, result):
        raise HTTPException(
            status_code=503,
            detail="Amazon blocked add-to-cart. Retry later.",
        )
    added = bool(re.search(r">\s*Added to cart\s*<", result, re.I))
    title_m = re.search(r'id="productTitle"[^>]*>(.*?)</span>', html, re.S)
    title = _textify(title_m.group(1)) if title_m else None
    if not added:
        alert = re.search(r'class="a-alert-content"[^>]*>(.*?)</(?:div|span)>', result, re.S)
        detail = _textify(alert.group(1)) if alert else "Amazon did not confirm the item was added."
        raise HTTPException(status_code=409, detail=detail[:300])
    return {
        "added": True,
        "asin": asin_n,
        "quantity": quantity,
        "title": title,
        "region": region.lower(),
        "account": label,
        "signed_in": _session_signed_in(),
        "cart_url": f"https://www.{domain}/gp/cart/view.html",
    }


@app.post("/cart", dependencies=[Depends(_require_key)])
async def add_to_cart(body: AddToCartBody) -> dict[str, Any]:
    return await _add_to_cart(body.asin, body.quantity, body.region, body.account)


@app.get("/cart/add", dependencies=[Depends(_require_key)])
async def add_to_cart_get(
    asin: str = Query(..., min_length=1),
    quantity: int = Query(1, ge=1, le=30),
    region: str = Query(DEFAULT_REGION),
    account: str = Query("personal"),
) -> dict[str, Any]:
    return await _add_to_cart(asin, quantity, region, account)


@app.get("/cart", dependencies=[Depends(_require_key)])
async def view_cart(
    region: str = Query(DEFAULT_REGION),
    account: str = Query("personal"),
) -> dict[str, Any]:
    label = _select_account(account)
    domain = _domain(region)
    html = await _fetch_html(f"https://www.{domain}/gp/cart/view.html")
    cart = _parse_cart(html, domain)
    cart["region"] = region.lower()
    cart["account"] = label
    return cart


@app.get("/products", dependencies=[Depends(_require_key)])
async def products_alias(
    query: str = Query(..., min_length=1),
    region: str = Query(DEFAULT_REGION),
    page: int = Query(1, ge=1, le=20),
    max_results: int = Query(16, ge=1, le=48),
) -> SearchResponse:
    return await search(
        query=query,
        region=region,
        page=page,
        max_results=max_results,
        include_sponsored=False,
    )
