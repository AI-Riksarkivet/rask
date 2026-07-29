"""The annotations group router — composes the feature modules under ``/api``.

Registration order is preserved from the pre-split ``annotate.py`` (GET unit ·
GET versions · POST unit · POST tags) so ``/openapi.json``'s paths enumeration
stays byte-stable for snapshot tests / SDK generators. Matching is unaffected
either way (distinct path arities).
"""

from fastapi import APIRouter

from annotator.annotations import save, tags, versions, wire


router = APIRouter(prefix="/api")
router.include_router(wire.router)
router.include_router(versions.router)
router.include_router(save.router)
router.include_router(tags.router)
