#!/usr/bin/env bash
set -euo pipefail
docker buildx build -f .docker/ray.dockerfile -t ray:dev --load .
echo ">> torch.cuda check inside container"
docker run --rm --gpus all ray:dev \
  python -c "import torch; print('cuda', torch.cuda.is_available()); assert torch.cuda.is_available()"
echo ">> runner entrypoint present"
docker run --rm ray:dev runner --help >/dev/null
echo "OK ray:dev GPU + runner"
