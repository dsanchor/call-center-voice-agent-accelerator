# Container Image Build and Deployment

This directory contains GitHub Actions workflows for building and pushing container images to GitHub Container Registry (GHCR).

## Workflow: Build and Push Container Image

The `build-and-push.yml` workflow automatically builds and pushes Docker images when:

- Code is pushed to `main` or `develop` branches (only if files in `server/` directory change)
- Pull requests are opened against `main` branch (only if files in `server/` directory change)
- Manually triggered via GitHub Actions UI

### Image Location

Images are pushed to: `ghcr.io/dsanchor/call-center-voice-agent-accelerator/voice-live-agent`

### Available Tags

- `latest` - Latest build from main branch
- `main` - Latest build from main branch
- `develop` - Latest build from develop branch
- `main-<sha>` - Specific commit from main branch
- `develop-<sha>` - Specific commit from develop branch
- `pr-<number>` - Pull request builds

### Using the Images

To pull and run the latest image:

```bash
# Pull the latest image
docker pull ghcr.io/dsanchor/call-center-voice-agent-accelerator/voice-live-agent:latest

# Run the container
docker run -p 8000:8000 \
  -e AZURE_VOICE_LIVE_API_KEY="your-api-key" \
  -e AZURE_VOICE_LIVE_ENDPOINT="your-endpoint" \
  -e VOICE_LIVE_MODEL="gpt-4o-mini" \
  -e AZURE_AGENT_PROJECT_NAME="your-project-name" \
  -e AZURE_AGENT_ID="your-agent-id" \
  -e ACS_CONNECTION_STRING="your-acs-connection" \
  ghcr.io/dsanchor/call-center-voice-agent-accelerator/voice-live-agent:latest
```

### Azure Container Apps Deployment

To use with Azure Container Apps, update your deployment to use the GitHub Container Registry image:

```bash
# Update the image reference in your deployment
IMAGE_NAME="ghcr.io/dsanchor/call-center-voice-agent-accelerator/voice-live-agent:latest"

az containerapp update \
  --name $CONTAINER_APP_NAME \
  --resource-group $RESOURCE_GROUP \
  --image $IMAGE_NAME
```

### Permissions

The workflow uses the `GITHUB_TOKEN` which has the necessary permissions to push to GHCR. No additional secrets are required.

### Multi-platform Support

Images are built for both `linux/amd64` and `linux/arm64` architectures, ensuring compatibility with various deployment targets including Apple Silicon and ARM-based cloud instances.