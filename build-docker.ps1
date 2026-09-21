# Docker build and push script for jira-to-ado (Windows PowerShell)
# Usage: .\build-docker.ps1 -Action all -Push
# Examples:
#   .\build-docker.ps1 -Action backend
#   .\build-docker.ps1 -Action all -Push
#   .\build-docker.ps1 -Action all -Registry "myregistry.azurecr.io/" -Tag "v1.0" -Push

param(
    [ValidateSet("backend", "frontend", "all")]
    [string]$Action = "all",
    
    [string]$Registry = "",
    [string]$Tag = "latest",
    [switch]$Push = $false
)

$BackendImage = if ($Registry) { "$Registry`jira-ado-api" } else { "jira-ado-api" }
$FrontendImage = if ($Registry) { "$Registry`jira-ado-web" } else { "jira-ado-web" }

Write-Host "🐳 Docker Build Script for jira-to-ado" -ForegroundColor Cyan
Write-Host "Registry: $(if ($Registry) { $Registry } else { 'local' })"
Write-Host "Tag: $Tag"
Write-Host ""

function Build-Backend {
    Write-Host "📦 Building backend..." -ForegroundColor Yellow
    docker build -f Dockerfile.backend `
        -t "$BackendImage`:$Tag" `
        -t "$BackendImage`:latest" `
        .
    
    if ($LASTEXITCODE -eq 0) {
        Write-Host "✅ Backend built: $BackendImage`:$Tag" -ForegroundColor Green
    } else {
        Write-Host "❌ Backend build failed" -ForegroundColor Red
        exit 1
    }
}

function Build-Frontend {
    Write-Host "📦 Building frontend..." -ForegroundColor Yellow
    docker build -f Dockerfile.frontend `
        -t "$FrontendImage`:$Tag" `
        -t "$FrontendImage`:latest" `
        .
    
    if ($LASTEXITCODE -eq 0) {
        Write-Host "✅ Frontend built: $FrontendImage`:$Tag" -ForegroundColor Green
    } else {
        Write-Host "❌ Frontend build failed" -ForegroundColor Red
        exit 1
    }
}

function Push-Images {
    if (-not $Registry) {
        Write-Host "⚠️  No registry specified. Use -Registry parameter to push." -ForegroundColor Yellow
        return
    }

    Write-Host ""
    Write-Host "📤 Pushing images to registry..." -ForegroundColor Yellow
    
    docker push "$BackendImage`:$Tag"
    docker push "$BackendImage`:latest"
    docker push "$FrontendImage`:$Tag"
    docker push "$FrontendImage`:latest"
    
    if ($LASTEXITCODE -eq 0) {
        Write-Host "✅ Images pushed to $Registry" -ForegroundColor Green
    } else {
        Write-Host "❌ Push failed" -ForegroundColor Red
        exit 1
    }
}

# Main execution
switch ($Action) {
    "backend" {
        Build-Backend
        if ($Push) { Push-Images }
    }
    "frontend" {
        Build-Frontend
        if ($Push) { Push-Images }
    }
    "all" {
        Build-Backend
        Build-Frontend
        if ($Push) { Push-Images }
    }
}

Write-Host ""
Write-Host "Done! 🎉" -ForegroundColor Green
