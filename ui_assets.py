"""Static assets shared by the admin and OAuth login."""

import re
from functools import cache
from hashlib import sha256
from pathlib import Path

from starlette.exceptions import HTTPException
from starlette.responses import FileResponse

UI = Path(__file__).with_name("import_ui")


@cache
def asset_url(name):
    version = sha256((UI / name).read_bytes()).hexdigest()[:12]
    return f"/assets/{name}?v={version}"


def asset(request):
    name = request.path_params["name"]
    if name in ("archive.css", "platform-colors.css"):
        return FileResponse(UI / name, media_type="text/css")
    if name == "archive.js":
        return FileResponse(UI / name, media_type="text/javascript")
    if name == "flame-circle.svg":
        return FileResponse(UI / name, media_type="image/svg+xml")
    if re.fullmatch(r"golos-text-(latin|cyrillic)-(400|600)-normal\.woff2", name):
        return FileResponse(UI / "fonts" / name, media_type="font/woff2")
    raise HTTPException(404)
