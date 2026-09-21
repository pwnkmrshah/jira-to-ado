#!/bin/bash
# Docker build and push script for jira-to-ado
# Usage: ./build-docker.sh [backend|frontend|all] [push]

set -e

REGISTRY="${REGISTRY:-}"
TAG="${TAG:-latest}"
BACKEND_IMAGE="${REGISTRY}jira-ado-api"
FRONTEND_IMAGE="${REGISTRY}jira-ado-web"

echo "🐳 Docker Build Script for jira-to-ado"
echo "Registry: ${REGISTRY:-local}"
echo "Tag: $TAG"
echo ""

# Build backend
build_backend() {
  echo "📦 Building backend..."
  docker build -f Dockerfile.backend \
    -t "$BACKEND_IMAGE:$TAG" \
    -t "$BACKEND_IMAGE:latest" \
    .
  echo "✅ Backend built: $BACKEND_IMAGE:$TAG"
}

# Build frontend
build_frontend() {
  echo "📦 Building frontend..."
  docker build -f Dockerfile.frontend \
    -t "$FRONTEND_IMAGE:$TAG" \
    -t "$FRONTEND_IMAGE:latest" \
    .
  echo "✅ Frontend built: $FRONTEND_IMAGE:$TAG"
}

# Push images to registry
push_images() {
  if [ -z "$REGISTRY" ]; then
    echo "⚠️  No registry specified. Skip pushing with: REGISTRY=yourregistry.azurecr.io ./build-docker.sh all push"
    return
  fi

  echo ""
  echo "📤 Pushing images to registry..."
  docker push "$BACKEND_IMAGE:$TAG"
  docker push "$BACKEND_IMAGE:latest"
  docker push "$FRONTEND_IMAGE:$TAG"
  docker push "$FRONTEND_IMAGE:latest"
  echo "✅ Images pushed to $REGISTRY"
}

# Main logic
case "${1:-all}" in
  backend)
    build_backend
    [ "$2" = "push" ] && push_images
    ;;
  frontend)
    build_frontend
    [ "$2" = "push" ] && push_images
    ;;
  all)
    build_backend
    build_frontend
    [ "$2" = "push" ] && push_images
    ;;
  *)
    echo "Usage: $0 [backend|frontend|all] [push]"
    echo ""
    echo "Examples:"
    echo "  $0 all                    # Build both locally"
    echo "  $0 backend                # Build backend only"
    echo "  $0 frontend               # Build frontend only"
    echo ""
    echo "  REGISTRY=acr.azurecr.io/ TAG=v1.0 $0 all push"
    echo "  # Build with registry prefix and tag, then push to Azure Container Registry"
    exit 1
    ;;
esac

echo ""
echo "Done! 🎉"
