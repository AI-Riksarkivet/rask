# syntax=docker/dockerfile:1.11
# The development stand-in for the platform's NATS auth-callout signer (deploy/fixtures/nats/callout). A platform runs
# its own signer; this image exists only so the development fixture and the nats-auth lane have one.
#
#   scripts/dagger-image.sh --name bus-signer --push 172.17.0.1:5000/bus-signer:<tag>
FROM golang:1.27-alpine AS build
WORKDIR /src
COPY deploy/fixtures/nats/callout/go.mod deploy/fixtures/nats/callout/go.sum ./
RUN go mod download
COPY deploy/fixtures/nats/callout/*.go ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/bus-signer .

FROM gcr.io/distroless/static-debian12:nonroot

LABEL org.opencontainers.image.title="bus-signer" \
      org.opencontainers.image.description="Development stand-in for a NATS auth-callout signer (TokenReview to a scoped, short-lived bus user)"

COPY --from=build /out/bus-signer /bus-signer
USER nonroot:nonroot
ENTRYPOINT ["/bus-signer"]
