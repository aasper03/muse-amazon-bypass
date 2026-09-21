import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "amazon_cart.app:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8792")),
    )


if __name__ == "__main__":
    main()
