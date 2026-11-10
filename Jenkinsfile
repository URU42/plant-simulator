// plant-simulator CI/CD: 테스트 → 이미지 빌드 → Harbor push → GitOps 저장소(being-gitops) 의 이미지 태그 갱신.
// 팀의 frontend 파이프라인과 같은 모양이다: rootless-docker 에이전트, 태그 파일, Harbor 로봇 자격, GitOps PAT.
// 배포 자체는 GitOps 저장소 k8s/test 를 바라보는 쪽(ArgoCD 등)이 한다. deploy/k8s 의 매니페스트는 그 저장소에 옮겨 둔다.

pipeline {
    agent { label 'rootless-docker' }

    options {
        timestamps()
        disableConcurrentBuilds()
        skipDefaultCheckout(true)
    }

    environment {
        IMAGE = 'harbor.uru42.space/uru42/plant-simulator'
    }

    stages {
        stage('Checkout') {
            steps {
                checkout scm
            }
        }

        stage('Validate') {
            steps {
                sh '''
                    test -f Dockerfile
                    test -f pyproject.toml
                    test -f config/plants.k8s.yaml
                '''
            }
        }

        stage('Test') {
            steps {
                sh '''#!/bin/sh
set -eu

export DOCKER_HOST="unix:///run/user/$(id -u)/docker.sock"

# 에이전트에 Python 을 두지 않고 uv 이미지 안에서 테스트한다 (의존성은 uv.lock 그대로)
docker run --rm \
  -v "$PWD":/src -w /src \
  -e UV_CACHE_DIR=/tmp/uv-cache -e UV_PROJECT_ENVIRONMENT=/tmp/venv \
  ghcr.io/astral-sh/uv:python3.11-bookworm-slim \
  sh -c "uv sync --frozen && uv run pytest -q -p no:cacheprovider"
'''
            }
        }

        stage('Build') {
            steps {
                sh '''#!/bin/sh
set -eu

export DOCKER_HOST="unix:///run/user/$(id -u)/docker.sock"

TAG="test-${BUILD_NUMBER}-$(git rev-parse --short=8 HEAD)"
printf '%s' "$TAG" > .plant-sim-image-tag

docker build \
  -t "${IMAGE}:${TAG}" \
  -f Dockerfile \
  .

echo "Built image: ${IMAGE}:${TAG}"
'''
            }
        }

        stage('Push') {
            steps {
                withCredentials([
                    usernamePassword(
                        credentialsId: 'harbor-jenkins-robot',
                        usernameVariable: 'HARBOR_USER',
                        passwordVariable: 'HARBOR_PASS'
                    )
                ]) {
                    sh '''#!/bin/sh
set -eu
set +x

export DOCKER_HOST="unix:///run/user/$(id -u)/docker.sock"
export DOCKER_CONFIG="$(mktemp -d)"
trap 'rm -rf "$DOCKER_CONFIG"' EXIT

TAG="$(cat .plant-sim-image-tag)"

printf '%s' "$HARBOR_PASS" |
  docker login harbor.uru42.space \
    -u "$HARBOR_USER" \
    --password-stdin

docker push "${IMAGE}:${TAG}"

echo "Pushed image: ${IMAGE}:${TAG}"
'''
                }
            }
        }

        stage('Update GitOps Tag') {
            steps {
                withCredentials([
                    gitUsernamePassword(
                        credentialsId: 'gitops-github-pat',
                        gitToolName: 'Default'
                    )
                ]) {
                    sh '''#!/bin/sh
set -eu

GITOPS_REPO="https://github.com/URU42/being-gitops.git"
IMAGE="harbor.uru42.space/uru42/plant-simulator"
TAG="$(cat "$WORKSPACE/.plant-sim-image-tag")"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

echo "[1/6] Clone GitOps repository"
git clone --branch main --depth 1 \
  "$GITOPS_REPO" "$WORKDIR/gitops"

cd "$WORKDIR/gitops/k8s/test"

echo "[2/6] Update plant-simulator image tag"
echo "Target: ${IMAGE}:${TAG}"

kustomize edit set image "${IMAGE}=${IMAGE}:${TAG}"

echo "[3/6] Render Kubernetes manifests"
kustomize build . > "$WORKDIR/rendered.yaml"

echo "Images in rendered manifests:"
grep -n 'image:' "$WORKDIR/rendered.yaml" || true

echo "[4/6] Verify plant-simulator image"

if ! grep -Fq "image: ${IMAGE}:${TAG}" \
    "$WORKDIR/rendered.yaml"; then
    echo "ERROR: plant-simulator image tag not found in rendered manifests" >&2
    echo "       k8s/test 에 deploy/k8s 의 plant-sim 매니페스트가 들어 있어야 한다" >&2
    exit 1
fi

echo "[5/6] Commit GitOps changes"
cd "$WORKDIR/gitops"

git config user.name "URU42 Jenkins"
git config user.email "jenkins@localhost"

git add k8s/test/kustomization.yaml

if git diff --cached --quiet; then
    echo "No GitOps changes"
    exit 0
fi

git diff --cached --stat
git commit -m "deploy(plant-sim): ${TAG}"

echo "[6/6] Push GitOps changes"
git push origin HEAD:main

echo "SUCCESS: plant-simulator GitOps updated to ${TAG}"
'''
                }
            }
        }
    }
}
