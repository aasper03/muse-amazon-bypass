# Changelog

## [Unreleased] - Full Cart & List Operations

### Added
- **Remove from cart** (`POST /cart/remove`) — Remove item by ASIN using delete button + CSRF token
- **Update cart quantity** (`POST /cart/quantity`) — Change item quantity, 0 removes item
- **Save for later** (`POST /cart/save-for-later`) — Move item from cart to Save for Later
- **Move to cart** (`POST /cart/move-to-cart`) — Move item from Save for Later back to cart
- **Get saved for later** (`GET /cart/saved`) — List items in Save for Later
- **Registry add-to-cart** (`POST /registry/add`) — Add item from baby registry, marked as registry gift in cart
- **Generic list add-to-cart** (`POST /list/add`) — Add from any list type: baby_registry, wedding_registry, wishlist, gift_list
- **Add to wishlist** (`POST /wishlist/add`) — Add item to user's wishlist
- **Product reviews** (`GET /product/{asin}/reviews`) — Get reviews with rating, title, body, author, date. Sort by helpful or recent.
- **Order history** (`GET /orders`) — List recent order IDs

### Fixed
- **Cookie detection for all regions** — Changed `_session_signed_in()` to use regex matching any `at-*`, `sess-at-*`, or `x-*` cookie. Previously only detected US cookies (`at-main`, `sess-at-main`, `x-main`), missing Canadian cookies (`at-acbca`, etc.), UK, DE, and other regions. Now works for ALL regions (US, CA, UK, DE, FR, ES, IT, NL, JP, AU, MX, IN, AE, SA, IE, BE).

### Technical Details
- All operations use `curl_cffi` with Chrome TLS impersonation. No browser needed.
- Registry/list add-to-cart fetches product page with `?colid={listId}&coliid={listItemId}` params, which makes Amazon include `listPurchaseAttribution` fields in the add-to-cart form. These fields mark the item as from the list in the cart.
- Cart operations (remove, quantity, save-for-later, move-to-cart) use the cart form's submit buttons with UUIDs and CSRF tokens.
- Shipping address for registry/list items is selected at checkout — Amazon shows the list owner's address as an option.

### Limitations
- **Address management** — Amazon requires password re-authentication for address book, order history details, and checkout. These operations redirect to sign-in even with valid session cookies. This is Amazon's intentional security policy, not a limitation of this tool.
- **Checkout** — Not implemented by design. User completes checkout manually in browser.
- **Payment methods** — Not implemented (sensitive, user handles).
