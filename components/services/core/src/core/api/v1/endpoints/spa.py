"""SvelteKit static-build SPA fallback.

`adapter-static`'s entry is `index.html`. Unknown paths (client-side routes
like `/viewer/...`) get the same file so the SPA router can take over.

Only mounted if the build directory exists — packaged deploys ship without
the SPA when the API is the only thing being served.
"""

from fastapi import APIRouter
from fastapi.responses import FileResponse

from core.api.dependencies import SettingsDep


router = APIRouter(include_in_schema=False)


@router.get("/{full_path:path}")
def spa(full_path: str, settings: SettingsDep) -> FileResponse:
    build_dir = settings.resolved_spa_build
    target = build_dir / full_path if full_path else build_dir / "index.html"
    if target.is_file():
        return FileResponse(target)
    return FileResponse(build_dir / "index.html")
