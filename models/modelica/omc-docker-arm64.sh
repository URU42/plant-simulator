#!/bin/sh
# Apple Silicon Docker Desktop용으로 확인한 공식 ARM64 이미지.
set -eu
exec docker run --rm --platform linux/arm64 --network none \
  --mount "type=bind,source=$PWD,target=/work" --workdir /work \
  --user "$(id -u):$(id -g)" \
  openmodelica/openmodelica@sha256:a7f9f5f747a6a2ee98bbace787a6946e03d61282fee8ac1058ee71adce05b89d \
  omc "$@"
