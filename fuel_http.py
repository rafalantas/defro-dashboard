"""ThingSpeak/Open-Meteo JSON transport for the fuel domain."""
from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from fuel_errors import FuelError
from thermoflow_local_feeds import try_serve


def request_json(url: str, params: dict | None = None, form: dict | None = None,
                 payload: dict | None = None, timeout: int = 10):
    """Perform a bounded JSON GET/POST and translate transport errors to FuelError."""
    if params and form is None and payload is None and url.endswith("/feeds.json"):
        # Plain ThingSpeak range reads come from the local SQLite mirror when it is
        # provably equivalent; None means "use ThingSpeak as before".
        local = try_serve(url, params)
        if local is not None:
            return local
    if params:
        url = f"{url}?{urlencode(params)}"
    headers = {"Accept": "application/json", "User-Agent": "defro-dashboard-fuel"}
    data = None
    if form is not None:
        data = urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    base = url.split("?")[0]
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise FuelError(502, f"{base}: HTTP {error.code}") from None
    except (URLError, TimeoutError, OSError) as error:
        raise FuelError(502, f"{base}: {error}") from None
