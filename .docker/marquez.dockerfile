# syntax=docker/dockerfile:1.11
# The development graph backend's server (deploy/fixtures/marquez): Marquez's own API, on a JRE that runs on every node.
#
# WHY THE ESTATE REPACKAGES IT. `marquezproject/marquez` publishes linux/amd64 only (every tag through 0.51.1, read from
# Docker Hub 2026-10-16), and on this estate's arm64 node the published image exits `exec format error`. The API is a
# JVM jar, the same bytes on every architecture: the upstream stage only hands its files over (COPY --from runs
# nothing of it), and Temurin's JRE, which publishes arm64 and amd64, runs them. The JRE is the one upstream ships
# (JDK 17.0.14+7, the image's JAVA_VERSION), so the jar runs on the runtime it was released on.
#
#   scripts/dagger-image.sh --name marquez --push 172.17.0.1:5000/marquez:0.51.1
#
# hadolint ignore=DL3029  # Reason: the stage is copied from, never run, so its platform is the one upstream publishes.
FROM --platform=linux/amd64 marquezproject/marquez:0.51.1@sha256:0721c976cff17d8b14f7949d85d6dac9c7ea37cb9fe857caa19833730fcb1a50 AS upstream

FROM eclipse-temurin:17.0.14_7-jre-noble@sha256:db4dd3e55dd374d87cf00eb784bd44d67900abc87da82080cbae055a22c8ac76

LABEL org.opencontainers.image.title="marquez" \
      org.opencontainers.image.description="Marquez's API on a multi-arch JRE, for rask's development graph backend" \
      org.opencontainers.image.source="https://github.com/MarquezProject/marquez"

COPY --from=upstream /usr/src/app /usr/src/app
WORKDIR /usr/src/app
USER 1001
EXPOSE 5000 5001
ENTRYPOINT ["/usr/src/app/entrypoint.sh"]
