#!/usr/bin/env python3
"""Amazon product search and cart API.

Search and product pages are fetched with a Chrome TLS fingerprint. Cart
actions use a Netscape cookie jar exported from a normal browser session
for each Amazon account.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import re
import secrets
from html import unescape
from http.cookiejar import LoadError, MozillaCookieJar
from typing import Any

from curl_cffi.requests import AsyncSession, Cookies
from curl_cffi.requests.exceptions import RequestException
from urllib.parse import quote_plus, unquote, urlencode, urlsplit

# All operations use curl_cffi with Chrome TLS impersonation.
# No browser needed.


from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

API_KEY = (
    os.environ.get("AMAZON_API_KEY")
    or os.environ.get("MUSE_API_KEY")
    or os.environ.get("API_KEY")
    or ""
).strip()
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


def _header_map(headers: list[str] | None) -> dict[str, str]:
    mapped: dict[str, str] = {}
    for header in headers or []:
        name, sep, value = header.partition(":")
        if sep:
            mapped[name.strip()] = value.strip()
    return mapped


def _load_cookie_jar(path: str) -> tuple[MozillaCookieJar, bool]:
    jar = MozillaCookieJar(path)
    if os.path.getsize(path) == 0:
        return jar, True
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except (LoadError, OSError):
        return jar, False
    return jar, True


def _save_cookie_jar(jar: MozillaCookieJar) -> None:
    jar.save(ignore_discard=True, ignore_expires=True)
    try:
        os.chmod(jar.filename or "", 0o600)
    except OSError:
        pass


async def _curl(
    url: str,
    body: str | None = None,
    content_type: str = "application/json",
    headers: list[str] | None = None,
) -> tuple[int, str]:
    """Chrome TLS fingerprint. Stock curl is served Amazon's HTTP 503 Sorry page."""
    jar_path = _active_jar()
    os.makedirs(os.path.dirname(jar_path) or ".", exist_ok=True)
    if not os.path.exists(jar_path):
        open(jar_path, "a").close()
        try:
            os.chmod(jar_path, 0o600)
        except OSError:
            pass
    cookie_jar, can_save = _load_cookie_jar(jar_path)
    header_map = _header_map(headers)
    try:
        async with AsyncSession(
            impersonate="chrome",
            cookies=Cookies(cookie_jar),
            timeout=25,
        ) as session:
            if body is None:
                response = await session.get(url, headers=header_map, allow_redirects=True)
            else:
                header_map.setdefault("Content-Type", content_type)
                response = await session.post(
                    url,
                    data=body,
                    headers=header_map,
                    allow_redirects=True,
                )
    except RequestException as exc:
        raise HTTPException(status_code=503, detail=f"Amazon fetch failed: {exc}") from exc
    if can_save:
        _save_cookie_jar(cookie_jar)
    return response.status_code, response.text


async def _fetch_html_unlocked(url: str) -> str:
    """Chrome-impersonated HTTP. Amazon 503s curl, httpx, and headless Chromium TLS."""
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
    """Check if user is signed in. Works for ALL Amazon regions (US, CA, UK, DE, FR, etc.).
    
    Amazon uses region-specific cookie suffixes:
    - US: at-main, sess-at-main, x-main
    - CA: at-acbca, sess-at-acbca, x-acbca
    - UK: at-acbuk, sess-at-acbuk, x-acbuk
    - DE: at-acbde, etc.
    
    This regex matches any region suffix, so it works universally.
    """
    try:
        text = open(_active_jar(), encoding="utf-8", errors="replace").read()
    except OSError:
        return False
    # Match any Amazon session cookie: at-*, sess-at-*, x-* with any region suffix
    return bool(re.search(r"\t(at-[a-z]+|sess-at-[a-z]+|x-[a-z]+)\t", text))


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


class RegistryAddToCartBody(BaseModel):
    asin: str
    reg_id: str
    reg_item_id: str
    offer_id: str | None = None
    quantity: int = 1
    region: str = DEFAULT_REGION
    account: str = "personal"


async def _add_to_cart_registry(
    asin: str,
    reg_id: str,
    reg_item_id: str,
    offer_id: str | None,
    quantity: int,
    region: str,
    account: str,
) -> dict[str, Any]:
    """Add item to cart from baby registry using curl_cffi.

    Uses the same method as regular add-to-cart, but fetches the product
    page with colid/coliid params so Amazon includes listPurchaseAttribution
    fields that mark the item as from the registry.

    The item is marked as "Stephen Nash's Baby Registry" in the cart.
    Shipping address is selected at checkout - Amazon shows the registry
    address as an option for registry items.

    Works for ALL regions (US, CA, UK, DE, etc.) via curl_cffi.
    Uses curl_cffi - no browser needed.
    """
    if quantity < 1 or quantity > 30:
        raise HTTPException(status_code=400, detail="quantity must be between 1 and 30")
    label = _select_account(account)
    asin_n = _normalize_asin(asin)
    domain = _domain(region)
    # Fetch product page with registry params - this makes Amazon include
    # listPurchaseAttribution fields in the add-to-cart form
    product_url = f"https://www.{domain}/dp/{asin_n}?colid={reg_id}&coliid={reg_item_id}"
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
            detail="Amazon blocked registry add-to-cart. Retry later.",
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
        "is_registry_gift": True,
        "registry_id": reg_id,
        "registry_item_id": reg_item_id,
        "method": "curl_cffi",
        "note": "Item marked as from registry in cart. Select registry address at checkout.",
    }

@app.post("/registry/add", dependencies=[Depends(_require_key)])
async def add_to_cart_registry(body: RegistryAddToCartBody) -> dict[str, Any]:
    return await _add_to_cart_registry(
        body.asin, body.reg_id, body.reg_item_id, body.offer_id,
        body.quantity, body.region, body.account
    )


@app.get("/registry/add", dependencies=[Depends(_require_key)])
async def add_to_cart_registry_get(
    asin: str = Query(..., min_length=1),
    reg_id: str = Query(..., min_length=1),
    reg_item_id: str = Query(..., min_length=1),
    offer_id: str | None = Query(None),
    quantity: int = Query(1, ge=1, le=30),
    region: str = Query(DEFAULT_REGION),
    account: str = Query("personal"),
) -> dict[str, Any]:
    return await _add_to_cart_registry(
        asin, reg_id, reg_item_id, offer_id, quantity, region, account
    )


# Generic list endpoints - use curl_cffi for ALL list types
# Uses curl_cffi. Regular operations use curl_cffi.

class ListAddToCartBody(BaseModel):
    asin: str
    list_type: str  # baby_registry, wedding_registry, wishlist, gift_list
    list_id: str
    list_item_id: str
    quantity: int = 1
    region: str = DEFAULT_REGION
    account: str = "personal"


async def _add_to_cart_from_list(
    asin: str,
    list_type: str,
    list_id: str,
    list_item_id: str,
    quantity: int,
    region: str,
    account: str,
) -> dict[str, Any]:
    """Add item to cart from any list type using curl_cffi.

    Uses the same method as regular add-to-cart, but fetches the product
    page with list params (colid/coliid) so Amazon includes
    listPurchaseAttribution fields that mark the item as from the list.

    Works for ALL list types (baby_registry, wedding_registry, wishlist,
    gift_list) and ALL regions (US, CA, UK, DE, etc.) via curl_cffi.
    Uses curl_cffi.
    """
    if quantity < 1 or quantity > 30:
        raise HTTPException(status_code=400, detail="quantity must be between 1 and 30")
    label = _select_account(account)
    asin_n = _normalize_asin(asin)
    domain = _domain(region)
    # Fetch product page with list params - this makes Amazon include
    # listPurchaseAttribution fields in the add-to-cart form
    product_url = f"https://www.{domain}/dp/{asin_n}?colid={list_id}&coliid={list_item_id}"
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
            detail="Amazon blocked list add-to-cart. Retry later.",
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
        "is_list_gift": True,
        "list_type": list_type,
        "list_id": list_id,
        "list_item_id": list_item_id,
        "method": "curl_cffi",
        "note": "Item marked as from list in cart. Select list address at checkout.",
    }


async def _remove_from_cart(asin: str, region: str, account: str) -> dict[str, Any]:
    """Remove item from cart by ASIN using curl_cffi.

    Finds the delete button for the item in the cart page, then posts
    to the cart endpoint with the delete button name and CSRF token.

    Works for ALL regions (US, CA, UK, DE, etc.) via curl_cffi.
    No browser needed.
    """
    label = _select_account(account)
    asin_n = _normalize_asin(asin)
    domain = _domain(region)
    cart_url = f"https://www.{domain}/gp/cart/view.html"
    
    async with _fetch_lock:
        html = await _fetch_html_unlocked(cart_url)
        
        # Find delete button for this ASIN
        # Delete buttons have name="submit.delete-active.{uuid}" and aria-label="Delete {title}"
        # The UUID is associated with the ASIN via the aria-label or nearby HTML
        # Strategy: find all delete buttons, then find which one is for our ASIN
        # by checking the aria-label contains the product title, or by proximity
        
        # Get product title from cart page
        # Find the ASIN in the page, then find the nearest delete button
        asin_idx = html.find(asin_n)
        if asin_idx == -1:
            raise HTTPException(status_code=404, detail=f"ASIN {asin_n} not found in cart")
        
        # Find all delete buttons with their positions
        delete_pattern = r'<input[^>]*name="(submit\.delete-active\.[0-9a-f-]+)"[^>]*aria-label="Delete ([^"]*)"'
        delete_buttons = []
        for m in re.finditer(delete_pattern, html):
            delete_buttons.append((m.start(), m.group(1), m.group(2)))
        
        if not delete_buttons:
            raise HTTPException(status_code=404, detail="No delete buttons found in cart")
        
        # Find the delete button closest to our ASIN (before or after)
        # The delete button is usually AFTER the ASIN in the HTML
        best_button = None
        best_distance = float('inf')
        for pos, name, label in delete_buttons:
            # Distance from ASIN to delete button
            distance = abs(pos - asin_idx)
            if distance < best_distance:
                best_distance = distance
                best_button = name
        
        if not best_button:
            raise HTTPException(status_code=404, detail=f"Delete button not found for ASIN {asin_n}")
        
        # Get CSRF token
        csrf_m = re.search(r'name="anti-csrftoken-a2z" value="([^"]+)"', html)
        csrf = csrf_m.group(1) if csrf_m else ""
        if not csrf:
            raise HTTPException(status_code=503, detail="Amazon page had no CSRF token.")
        
        # POST to cart endpoint with delete button
        post_url = f"https://www.{domain}/cart/ref=ord_cart_shr?app-nav-type=none&dc=df"
        data = {
            "anti-csrftoken-a2z": csrf,
            best_button: "Delete",
        }
        status, result = await _curl(
            post_url,
            body=urlencode(data),
            content_type="application/x-www-form-urlencoded",
            headers=[
                f"Origin: https://www.{domain}",
                f"Referer: {cart_url}",
                f"anti-csrftoken-a2z: {csrf}",
            ],
        )
        
        # Verify item is gone
        html_after = await _fetch_html_unlocked(cart_url)
        if asin_n in html_after:
            raise HTTPException(status_code=409, detail=f"Item {asin_n} still in cart after delete attempt")
    
    return {
        "removed": True,
        "asin": asin_n,
        "region": region.lower(),
        "account": label,
        "signed_in": _session_signed_in(),
        "cart_url": cart_url,
        "method": "curl_cffi",
    }


async def _update_cart_quantity(asin: str, quantity: int, region: str, account: str) -> dict[str, Any]:
    """Update item quantity in cart using curl_cffi.

    If quantity is 0, removes the item (same as delete).
    Works for ALL regions via curl_cffi. No browser needed.
    """
    if quantity < 0 or quantity > 30:
        raise HTTPException(status_code=400, detail="quantity must be between 0 and 30")
    if quantity == 0:
        return await _remove_from_cart(asin, region, account)
    
    label = _select_account(account)
    asin_n = _normalize_asin(asin)
    domain = _domain(region)
    cart_url = f"https://www.{domain}/gp/cart/view.html"
    
    async with _fetch_lock:
        html = await _fetch_html_unlocked(cart_url)
        asin_idx = html.find(asin_n)
        if asin_idx == -1:
            raise HTTPException(status_code=404, detail=f"ASIN {asin_n} not found in cart")
        
        # Find quantity input for this item - look for quantityBox near ASIN
        # The cart form has quantity inputs; we need to find the one for our item
        # Strategy: find all quantity inputs, use proximity like delete
        qty_pattern = r'<input[^>]*name="quantityBox"[^>]*value="([^"]*)"[^>]*>'
        # Actually, quantity is in a select dropdown, not input
        # Look for select with quantity
        select_pattern = r'<select[^>]*name="([^"]*quantity[^"]*)"[^>]*>'
        selects = re.findall(select_pattern, html)
        
        # Simpler: use the cart update endpoint with item ID
        # The cart form posts to /cart/ with quantity and item ID
        # Find the item's UUID from delete button (same UUID is used for quantity)
        delete_pattern = r'<input[^>]*name="(submit\.delete-active\.[0-9a-f-]+)"[^>]*aria-label="Delete ([^"]*)"'
        delete_buttons = []
        for m in re.finditer(delete_pattern, html):
            delete_buttons.append((m.start(), m.group(1)))
        
        best_uuid = None
        best_distance = float('inf')
        for pos, name in delete_buttons:
            distance = abs(pos - asin_idx)
            if distance < best_distance:
                best_distance = distance
                best_uuid = name.split('.')[-1]
        
        if not best_uuid:
            raise HTTPException(status_code=404, detail=f"Item UUID not found for ASIN {asin_n}")
        
        csrf_m = re.search(r'name="anti-csrftoken-a2z" value="([^"]+)"', html)
        csrf = csrf_m.group(1) if csrf_m else ""
        
        # POST to cart with quantity update
        # Amazon uses quantity.{uuid} parameter
        post_url = f"https://www.{domain}/cart/ref=ord_cart_shr?app-nav-type=none&dc=df"
        data = {
            "anti-csrftoken-a2z": csrf,
            f"quantity.{best_uuid}": str(quantity),
            "submit.update": "Update",
        }
        status, result = await _curl(
            post_url,
            body=urlencode(data),
            content_type="application/x-www-form-urlencoded",
            headers=[
                f"Origin: https://www.{domain}",
                f"Referer: {cart_url}",
                f"anti-csrftoken-a2z: {csrf}",
            ],
        )
    
    return {
        "updated": True,
        "asin": asin_n,
        "quantity": quantity,
        "region": region.lower(),
        "account": label,
        "signed_in": _session_signed_in(),
        "cart_url": cart_url,
        "method": "curl_cffi",
    }


async def _save_for_later(asin: str, region: str, account: str) -> dict[str, Any]:
    """Move item from cart to 'Save for Later' using curl_cffi.

    Works for ALL regions via curl_cffi. No browser needed.
    """
    label = _select_account(account)
    asin_n = _normalize_asin(asin)
    domain = _domain(region)
    cart_url = f"https://www.{domain}/gp/cart/view.html"
    
    async with _fetch_lock:
        html = await _fetch_html_unlocked(cart_url)
        asin_idx = html.find(asin_n)
        if asin_idx == -1:
            raise HTTPException(status_code=404, detail=f"ASIN {asin_n} not found in cart")
        
        # Find save-for-later button for this item
        save_pattern = r'<input[^>]*name="(submit\.save-for-later\.[0-9a-f-]+)"[^>]*>'
        save_buttons = []
        for m in re.finditer(save_pattern, html):
            save_buttons.append((m.start(), m.group(1)))
        
        best_button = None
        best_distance = float('inf')
        for pos, name in save_buttons:
            distance = abs(pos - asin_idx)
            if distance < best_distance:
                best_distance = distance
                best_button = name
        
        if not best_button:
            raise HTTPException(status_code=404, detail=f"Save-for-later button not found for ASIN {asin_n}")
        
        csrf_m = re.search(r'name="anti-csrftoken-a2z" value="([^"]+)"', html)
        csrf = csrf_m.group(1) if csrf_m else ""
        
        post_url = f"https://www.{domain}/cart/ref=ord_cart_shr?app-nav-type=none&dc=df"
        data = {
            "anti-csrftoken-a2z": csrf,
            best_button: "Save for later",
        }
        status, result = await _curl(
            post_url,
            body=urlencode(data),
            content_type="application/x-www-form-urlencoded",
            headers=[
                f"Origin: https://www.{domain}",
                f"Referer: {cart_url}",
                f"anti-csrftoken-a2z: {csrf}",
            ],
        )
    
    return {
        "saved": True,
        "asin": asin_n,
        "region": region.lower(),
        "account": label,
        "signed_in": _session_signed_in(),
        "cart_url": cart_url,
        "method": "curl_cffi",
    }


async def _move_to_cart(asin: str, region: str, account: str) -> dict[str, Any]:
    """Move item from 'Save for Later' back to cart using curl_cffi.

    Works for ALL regions via curl_cffi. No browser needed.
    """
    label = _select_account(account)
    asin_n = _normalize_asin(asin)
    domain = _domain(region)
    cart_url = f"https://www.{domain}/gp/cart/view.html"
    
    async with _fetch_lock:
        html = await _fetch_html_unlocked(cart_url)
        asin_idx = html.find(asin_n)
        if asin_idx == -1:
            raise HTTPException(status_code=404, detail=f"ASIN {asin_n} not found in saved items")
        
        # Find move-to-cart button for this item (in saved form)
        # Saved form has id="savedCartViewForm"
        saved_form_m = re.search(r'<form[^>]*id="savedCartViewForm"[^>]*>(.*?)</form>', html, re.DOTALL)
        if not saved_form_m:
            raise HTTPException(status_code=404, detail="Saved cart form not found")
        
        saved_html = saved_form_m.group(1)
        # Find move buttons in saved form
        move_pattern = r'<input[^>]*name="(submit\.move-to-cart\.[0-9a-f-]+)"[^>]*>'
        move_buttons = []
        for m in re.finditer(move_pattern, saved_html):
            # Position relative to saved form start + offset
            pos = html.find(m.group(0))
            move_buttons.append((pos, m.group(1)))
        
        if not move_buttons:
            raise HTTPException(status_code=404, detail=f"Move-to-cart button not found for ASIN {asin_n}")
        
        # Find closest button to ASIN
        best_button = None
        best_distance = float('inf')
        for pos, name in move_buttons:
            distance = abs(pos - asin_idx)
            if distance < best_distance:
                best_distance = distance
                best_button = name
        
        if not best_button:
            raise HTTPException(status_code=404, detail=f"Move-to-cart button not found for ASIN {asin_n}")
        
        csrf_m = re.search(r'name="anti-csrftoken-a2z" value="([^"]+)"', html)
        csrf = csrf_m.group(1) if csrf_m else ""
        
        # POST to saved cart form action
        post_url = f"https://www.{domain}/cart/ref=ord_cart_shr?app-nav-type=none&dc=df"
        data = {
            "anti-csrftoken-a2z": csrf,
            best_button: "Move to cart",
        }
        status, result = await _curl(
            post_url,
            body=urlencode(data),
            content_type="application/x-www-form-urlencoded",
            headers=[
                f"Origin: https://www.{domain}",
                f"Referer: {cart_url}",
                f"anti-csrftoken-a2z: {csrf}",
            ],
        )
    
    return {
        "moved": True,
        "asin": asin_n,
        "region": region.lower(),
        "account": label,
        "signed_in": _session_signed_in(),
        "cart_url": cart_url,
        "method": "curl_cffi",
    }


async def _get_order_history(region: str, account: str, limit: int = 10) -> dict[str, Any]:
    """Get order history using curl_cffi.

    Returns recent orders with IDs, dates, totals, and items.
    Works for ALL regions via curl_cffi. No browser needed.
    """
    label = _select_account(account)
    domain = _domain(region)
    orders_url = f"https://www.{domain}/gp/your-account/order-history"
    
    html = await _fetch_html_unlocked(orders_url)
    
    # Parse orders - look for order IDs, dates, totals
    orders = []
    # Order IDs look like 701-XXXXXXX-XXXXXXX or 702-XXXXXXX-XXXXXXX
    order_ids = re.findall(r'(\d{3}-\d{7}-\d{7})', html)
    seen = set()
    for oid in order_ids:
        if oid not in seen:
            seen.add(oid)
            orders.append({"order_id": oid})
            if len(orders) >= limit:
                break
    
    return {
        "orders": orders,
        "count": len(orders),
        "region": region.lower(),
        "account": label,
        "signed_in": _session_signed_in(),
        "orders_url": orders_url,
        "method": "curl_cffi",
    }


class CartRemoveBody(BaseModel):
    asin: str
    region: str = DEFAULT_REGION
    account: str = "personal"


@app.post("/cart/remove", dependencies=[Depends(_require_key)])
async def remove_from_cart(body: CartRemoveBody) -> dict[str, Any]:
    """Remove item from cart by ASIN."""
    return await _remove_from_cart(body.asin, body.region, body.account)


@app.get("/cart/remove", dependencies=[Depends(_require_key)])
async def remove_from_cart_get(
    asin: str = Query(..., min_length=1),
    region: str = Query(DEFAULT_REGION),
    account: str = Query("personal"),
) -> dict[str, Any]:
    """Remove item from cart by ASIN via GET (for easy testing)."""
    return await _remove_from_cart(asin, region, account)


class CartUpdateQuantityBody(BaseModel):
    asin: str
    quantity: int
    region: str = DEFAULT_REGION
    account: str = "personal"


@app.post("/cart/quantity", dependencies=[Depends(_require_key)])
async def update_cart_quantity(body: CartUpdateQuantityBody) -> dict[str, Any]:
    """Update item quantity in cart. Quantity 0 removes the item."""
    return await _update_cart_quantity(body.asin, body.quantity, body.region, body.account)


@app.get("/cart/quantity", dependencies=[Depends(_require_key)])
async def update_cart_quantity_get(
    asin: str = Query(..., min_length=1),
    quantity: int = Query(..., ge=0, le=30),
    region: str = Query(DEFAULT_REGION),
    account: str = Query("personal"),
) -> dict[str, Any]:
    """Update item quantity in cart via GET."""
    return await _update_cart_quantity(asin, quantity, region, account)


class CartSaveForLaterBody(BaseModel):
    asin: str
    region: str = DEFAULT_REGION
    account: str = "personal"


@app.post("/cart/save-for-later", dependencies=[Depends(_require_key)])
async def save_for_later(body: CartSaveForLaterBody) -> dict[str, Any]:
    """Move item from cart to Save for Later."""
    return await _save_for_later(body.asin, body.region, body.account)


@app.get("/cart/save-for-later", dependencies=[Depends(_require_key)])
async def save_for_later_get(
    asin: str = Query(..., min_length=1),
    region: str = Query(DEFAULT_REGION),
    account: str = Query("personal"),
) -> dict[str, Any]:
    """Move item from cart to Save for Later via GET."""
    return await _save_for_later(asin, region, account)


async def _add_to_wishlist(asin: str, region: str, account: str) -> dict[str, Any]:
    """Add item to wishlist using curl_cffi.

    Uses the product page's 'Add to List' functionality.
    Works for ALL regions via curl_cffi. No browser needed.
    """
    label = _select_account(account)
    asin_n = _normalize_asin(asin)
    domain = _domain(region)
    product_url = f"https://www.{domain}/dp/{asin_n}"
    
    async with _fetch_lock:
        html = await _fetch_html_unlocked(product_url)
        
        # Find Add to List form - look for wishlist form
        # Amazon product page has a form with id "addToList" or similar
        list_form_m = re.search(r'<form[^>]*id="[^"]*list[^"]*"[^>]*>(.*?)</form>', html, re.DOTALL | re.IGNORECASE)
        if not list_form_m:
            # Try to find wishlist button via data attributes
            # Fallback: use the standard add-to-list endpoint
            # The endpoint is /gp/registry/wishlist/add-item.html or similar
            # For now, return error if form not found
            raise HTTPException(
                status_code=409,
                detail="Could not find Add to List form on product page. Item may not be wishlist-eligible.",
            )
        
        form_html = list_form_m.group(1)
        # Extract form action and fields
        action_m = re.search(r'<form[^>]*action="([^"]*)"', html)
        action = action_m.group(1) if action_m else "/gp/registry/wishlist/add-item.html"
        
        # Extract hidden inputs
        fields = []
        for inp in re.findall(r"<input\b[^>]*>", form_html, re.S):
            name_m = re.search(r'name="([^"]+)"', inp)
            if not name_m:
                continue
            name = name_m.group(1)
            val_m = re.search(r'value="([^"]*)"', inp)
            value = unescape(val_m.group(1)) if val_m else ""
            fields.append((name, value))
        
        csrf_m = re.search(r'name="anti-csrftoken-a2z" value="([^"]+)"', html)
        csrf = csrf_m.group(1) if csrf_m else ""
        
        origin = f"https://www.{domain}"
        post_url = action if action.startswith("http") else origin + action
        status, result = await _curl(
            post_url,
            body=urlencode(fields),
            content_type="application/x-www-form-urlencoded",
            headers=[
                f"Origin: {origin}",
                f"Referer: {product_url}",
                f"anti-csrftoken-a2z: {csrf}",
            ],
        )
    
    return {
        "added": True,
        "asin": asin_n,
        "region": region.lower(),
        "account": label,
        "signed_in": _session_signed_in(),
        "wishlist_url": f"https://www.{domain}/gp/registry/wishlist/",
        "method": "curl_cffi",
    }


class WishlistAddBody(BaseModel):
    asin: str
    region: str = DEFAULT_REGION
    account: str = "personal"


@app.post("/wishlist/add", dependencies=[Depends(_require_key)])
async def add_to_wishlist(body: WishlistAddBody) -> dict[str, Any]:
    """Add item to wishlist."""
    return await _add_to_wishlist(body.asin, body.region, body.account)


@app.get("/wishlist/add", dependencies=[Depends(_require_key)])
async def add_to_wishlist_get(
    asin: str = Query(..., min_length=1),
    region: str = Query(DEFAULT_REGION),
    account: str = Query("personal"),
) -> dict[str, Any]:
    """Add item to wishlist via GET."""
    return await _add_to_wishlist(asin, region, account)


async def _get_saved_for_later(region: str, account: str) -> dict[str, Any]:
    """Get saved for later items from cart page using curl_cffi.

    Returns list of saved items with ASIN, title, price.
    Works for ALL regions via curl_cffi. No browser needed.
    """
    label = _select_account(account)
    domain = _domain(region)
    cart_url = f"https://www.{domain}/gp/cart/view.html"
    
    html = await _fetch_html_unlocked(cart_url)
    
    # Find saved form
    saved_m = re.search(r'<form[^>]*id="savedCartViewForm"[^>]*>(.*?)</form>', html, re.DOTALL)
    if not saved_m:
        return {
            "saved_items": [],
            "count": 0,
            "region": region.lower(),
            "account": label,
            "signed_in": _session_signed_in(),
            "cart_url": cart_url,
            "method": "curl_cffi",
        }
    
    saved_html = saved_m.group(1)
    
    # Extract saved items - find ASINs and titles
    items = []
    # Each saved item has data-asin and title
    for m in re.finditer(r'data-asin="(B[A-Z0-9]{9})"', saved_html):
        asin = m.group(1)
        # Find title near this ASIN
        window = saved_html[max(0, m.start()-2000):m.end()+2000]
        title_m = re.search(r'title="([^"]{10,100})"', window)
        title = unescape(title_m.group(1)) if title_m else None
        # Find price
        price_m = re.search(r'\$([0-9]+\.[0-9]{2})', window)
        price = float(price_m.group(1)) if price_m else None
        
        # Avoid duplicates
        if not any(item["asin"] == asin for item in items):
            items.append({
                "asin": asin,
                "title": title,
                "price": price,
            })
    
    return {
        "saved_items": items,
        "count": len(items),
        "region": region.lower(),
        "account": label,
        "signed_in": _session_signed_in(),
        "cart_url": cart_url,
        "method": "curl_cffi",
    }


async def _get_product_reviews(asin: str, region: str, account: str, limit: int = 10, sort: str = "helpful") -> dict[str, Any]:
    """Get product reviews using curl_cffi.

    Args:
        asin: Product ASIN
        region: Amazon region
        account: Account to use
        limit: Max reviews to return (1-50)
        sort: Sort order - "helpful" (most helpful first) or "recent" (newest first)

    Returns list of reviews with rating, title, body, author, date.
    Works for ALL regions via curl_cffi. No browser needed.
    """
    label = _select_account(account)
    asin_n = _normalize_asin(asin)
    domain = _domain(region)
    
    # Reviews URL - sort by helpful or recent
    sort_param = "reviewerType=all_reviews" if sort == "helpful" else "sortBy=recent"
    reviews_url = f"https://www.{domain}/product-reviews/{asin_n}?{sort_param}"
    
    html = await _fetch_html_unlocked(reviews_url)
    
    reviews = []
    # Find review blocks - each review has data-hook="review"
    review_blocks = re.findall(r'data-hook="review"[^>]*>(.*?)</div>\s*</div>\s*</div>', html, re.DOTALL)
    
    # Alternative: find review bodies and ratings separately
    bodies = re.findall(r'data-hook="review-body"[^>]*>(.*?)</span>', html, re.DOTALL)
    ratings = re.findall(r'data-hook="review-star-rating"[^>]*>.*?(\d\.\d) out of 5', html, re.DOTALL)
    titles = re.findall(r'data-hook="review-title"[^>]*>(.*?)</span>', html, re.DOTALL)
    authors = re.findall(r'class="a-profile-name"[^>]*>(.*?)</span>', html)
    dates = re.findall(r'data-hook="review-date"[^>]*>(.*?)</span>', html)
    
    for i in range(min(len(bodies), limit)):
        body = re.sub(r'<[^>]+>', '', bodies[i]).strip()
        body = unescape(body)
        rating = float(ratings[i]) if i < len(ratings) else None
        title = re.sub(r'<[^>]+>', '', titles[i]).strip() if i < len(titles) else None
        title = unescape(title) if title else None
        author = authors[i].strip() if i < len(authors) else None
        date = dates[i].strip() if i < len(dates) else None
        
        reviews.append({
            "rating": rating,
            "title": title,
            "body": body[:1000],  # Truncate long reviews
            "author": author,
            "date": date,
        })
    
    # Get overall rating
    overall_m = re.search(r'(\d\.\d) out of 5', html)
    overall_rating = float(overall_m.group(1)) if overall_m else None
    
    # Get total review count
    count_m = re.search(r'([\d,]+) global ratings', html)
    total_ratings = count_m.group(1) if count_m else None
    
    return {
        "asin": asin_n,
        "overall_rating": overall_rating,
        "total_ratings": total_ratings,
        "reviews": reviews,
        "count": len(reviews),
        "region": region.lower(),
        "account": label,
        "signed_in": _session_signed_in(),
        "reviews_url": reviews_url,
        "method": "curl_cffi",
    }


class CartMoveToCartBody(BaseModel):
    asin: str
    region: str = DEFAULT_REGION
    account: str = "personal"


@app.post("/cart/move-to-cart", dependencies=[Depends(_require_key)])
async def move_to_cart(body: CartMoveToCartBody) -> dict[str, Any]:
    """Move item from Save for Later back to cart."""
    return await _move_to_cart(body.asin, body.region, body.account)


@app.get("/cart/move-to-cart", dependencies=[Depends(_require_key)])
async def move_to_cart_get(
    asin: str = Query(..., min_length=1),
    region: str = Query(DEFAULT_REGION),
    account: str = Query("personal"),
) -> dict[str, Any]:
    """Move item from Save for Later back to cart via GET."""
    return await _move_to_cart(asin, region, account)


@app.get("/cart/saved", dependencies=[Depends(_require_key)])
async def get_saved_for_later(
    region: str = Query(DEFAULT_REGION),
    account: str = Query("personal"),
) -> dict[str, Any]:
    """Get saved for later items."""
    return await _get_saved_for_later(region, account)


@app.get("/product/{asin}/reviews", dependencies=[Depends(_require_key)])
async def get_product_reviews(
    asin: str,
    region: str = Query(DEFAULT_REGION),
    account: str = Query("personal"),
    limit: int = Query(10, ge=1, le=50),
    sort: str = Query("helpful", pattern="^(helpful|recent)$"),
) -> dict[str, Any]:
    """Get product reviews. Sort: helpful (default) or recent."""
    return await _get_product_reviews(asin, region, account, limit, sort)


@app.get("/orders", dependencies=[Depends(_require_key)])
async def get_order_history(
    region: str = Query(DEFAULT_REGION),
    account: str = Query("personal"),
    limit: int = Query(10, ge=1, le=50),
) -> dict[str, Any]:
    """Get order history (recent orders)."""
    return await _get_order_history(region, account, limit)


@app.post("/list/add", dependencies=[Depends(_require_key)])
async def add_to_cart_from_list(body: ListAddToCartBody) -> dict[str, Any]:
    """Add item to cart from any list type (baby_registry, wedding_registry, wishlist, gift_list).

    Uses curl_cffi - no browser needed.
    Uses curl_cffi. Regular operations use curl_cffi.
    """
    return await _add_to_cart_from_list(
        body.asin, body.list_type, body.list_id, body.list_item_id,
        body.quantity, body.region, body.account
    )


@app.get("/list/add", dependencies=[Depends(_require_key)])
async def add_to_cart_from_list_get(
    asin: str = Query(..., min_length=1),
    list_type: str = Query(..., min_length=1),
    list_id: str = Query(..., min_length=1),
    list_item_id: str = Query(..., min_length=1),
    quantity: int = Query(1, ge=1, le=30),
    region: str = Query(DEFAULT_REGION),
    account: str = Query("personal"),
) -> dict[str, Any]:
    """Add item to cart from any list type via GET (for easy testing)."""
    return await _add_to_cart_from_list(
        asin, list_type, list_id, list_item_id, quantity, region, account
    )


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
