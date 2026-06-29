"""controlplane — read-only listing of operator Project CRs for the home picker.
Stateless: no DB/Lance/Ray/S3; reads the k8s API per request (see routes.py)."""

from controlplane import health, routes
from service_kit import make_service_app


app = make_service_app(title="controlplane", routers=[health.router, routes.router])
