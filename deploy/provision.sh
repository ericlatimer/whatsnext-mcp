#!/usr/bin/env bash
# One-time Azure setup for the What's Next public MCP server, then a first publish.
# Idempotent enough to re-run: existing resources are left as they are.
#
#   deploy/provision.sh            # create everything + publish
#   deploy/provision.sh publish    # re-publish code only
#
# Cost controls: Flex Consumption (pay per execution, scales to zero), max 2
# instances, 512 MB instances, Standard_LRS storage. The $10/mo budget on the
# billing account emails on overrun.
set -euo pipefail

AZ="${AZ:-$HOME/.azure-cli-venv/bin/az}"
RG="whatsnext-mcp-rg"
LOCATION="westus2"
APP="whatsnext-mcp"                 # → https://whatsnext-mcp.azurewebsites.net (must be globally unique)
STORAGE="whatsnextmcp$(echo -n "$($AZ account show --query id -o tsv)" | shasum | cut -c1-8)"
MAX_INSTANCES=2

cd "$(dirname "$0")/.."

publish() {
  # Flex Consumption remote-builds requirements.txt into .python_packages.
  PATH=".venv/bin:$(dirname "$AZ"):$PATH" func azure functionapp publish "$APP" --python
}

if [[ "${1:-}" == "publish" ]]; then publish; exit 0; fi

$AZ group create -n "$RG" -l "$LOCATION" -o none
echo "✓ resource group $RG"

if ! $AZ storage account show -n "$STORAGE" -g "$RG" -o none 2>/dev/null; then
  $AZ storage account create -n "$STORAGE" -g "$RG" -l "$LOCATION" --sku Standard_LRS \
    --allow-blob-public-access false --min-tls-version TLS1_2 -o none
fi
echo "✓ storage $STORAGE"

if ! $AZ functionapp show -n "$APP" -g "$RG" -o none 2>/dev/null; then
  $AZ functionapp create -n "$APP" -g "$RG" --storage-account "$STORAGE" \
    --flexconsumption-location "$LOCATION" --runtime python --runtime-version 3.14 \
    --instance-memory 512 --maximum-instance-count "$MAX_INSTANCES" -o none
fi
$AZ functionapp scale config set -n "$APP" -g "$RG" --maximum-instance-count "$MAX_INSTANCES" -o none
# Set HTTP concurrency explicitly. With it unset (the default) and a 1-2 instance
# cap, the Flex front end queued requests for 15-45s before they reached the host.
$AZ functionapp scale config set -n "$APP" -g "$RG" --trigger-type http \
  --trigger-settings perInstanceConcurrency=16 -o none
echo "✓ function app $APP (max $MAX_INSTANCES instances, 16 concurrent requests each)"

# Unlisted launch: require a key until we're ready to list it publicly.
# Delete the MCP_API_KEY setting to open it up.
KEY="$($AZ functionapp config appsettings list -n "$APP" -g "$RG" \
        --query "[?name=='MCP_API_KEY'].value | [0]" -o tsv)"
if [[ -z "$KEY" ]]; then KEY="$(openssl rand -hex 24)"; fi
$AZ functionapp config appsettings set -n "$APP" -g "$RG" -o none --settings \
  AzureWebJobsFeatureFlags=EnableMcpCustomHandlerPreview \
  PYTHONPATH=/home/site/wwwroot/.python_packages/lib/site-packages \
  MCP_API_KEY="$KEY"
echo "✓ app settings"

publish

echo
echo "MCP endpoint: https://$APP.azurewebsites.net/mcp"
echo "API key:      read it with: $AZ functionapp config appsettings list -n $APP -g $RG --query \"[?name=='MCP_API_KEY'].value | [0]\" -o tsv"
