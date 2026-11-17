#!/usr/bin/env bash
# One-time Azure setup for the What's Next public MCP server, then a first publish.
# Idempotent enough to re-run: existing resources are left as they are.
# For routine code deploys use ./deploy.sh instead.
#
#   ALERT_EMAIL=you@example.com deploy/provision.sh   # create everything + publish
#   REQUIRE_KEY=1 ... deploy/provision.sh             # also require an API key (unlisted launch)
#   deploy/provision.sh publish                       # re-publish code only (what deploy.sh calls)
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

$AZ functionapp config appsettings set -n "$APP" -g "$RG" -o none --settings \
  AzureWebJobsFeatureFlags=EnableMcpCustomHandlerPreview \
  PYTHONPATH=/home/site/wwwroot/.python_packages/lib/site-packages
# The server is public: no key unless asked for. Re-running this script must not
# quietly lock it again (it used to create a key whenever none was set).
if [[ "${REQUIRE_KEY:-0}" == "1" ]]; then
  KEY="$($AZ functionapp config appsettings list -n "$APP" -g "$RG" \
          --query "[?name=='MCP_API_KEY'].value | [0]" -o tsv)"
  [[ -n "$KEY" ]] || KEY="$(openssl rand -hex 24)"
  $AZ functionapp config appsettings set -n "$APP" -g "$RG" -o none --settings MCP_API_KEY="$KEY"
  echo "✓ API key required (MCP_API_KEY)"
fi
echo "✓ app settings"

# Alerts read the server's own telemetry (telemetry.py): the Functions host doesn't
# record proxied requests reliably. Each log line also lands twice (OpenTelemetry +
# host console capture), so trace queries keep only the OpenTelemetry copy.
if [[ -n "${ALERT_EMAIL:-}" ]]; then
  $AZ monitor action-group create -g "$RG" -n whatsnext-mcp-alerts --short-name wnmcp \
    --action email owner "$ALERT_EMAIL" -o none
  AG="$($AZ monitor action-group show -g "$RG" -n whatsnext-mcp-alerts --query id -o tsv)"
  AI="$($AZ monitor app-insights component show -a "$APP" -g "$RG" --query id -o tsv)"
  alert() {  # name severity threshold query description
    local verb=create
    $AZ monitor scheduled-query show -g "$RG" -n "$1" -o none 2>/dev/null && verb=update
    $AZ monitor scheduled-query "$verb" -g "$RG" -n "$1" --scopes "$AI" --severity "$2" \
      --window-size 15m --evaluation-frequency 15m --action-groups "$AG" --description "$5" \
      --condition "total n from 'Q1' > $3" --condition-query Q1="$4" -o none
  }
  alert "MCP errors" 2 5 \
    "union (requests | where cloud_RoleName == '$APP' and toint(resultCode) >= 500 | project n = itemCount), (traces | where cloud_RoleName == '$APP' and isnotempty(customDimensions.logger_name) and message has 'upstream error' | project n = itemCount)" \
    "5xx responses or What's Next API failures from the public MCP server"
  alert "MCP rate limiting" 3 100 \
    "requests | where cloud_RoleName == '$APP' and resultCode == '429' | project n = itemCount" \
    "Sustained 429s from the public MCP server (possible abuse, or limits too tight)"
  echo "✓ alerts → $ALERT_EMAIL"
else
  echo "• ALERT_EMAIL not set: alerts left as they are"
fi

publish

echo
echo "MCP endpoint: https://$APP.azurewebsites.net/mcp"
