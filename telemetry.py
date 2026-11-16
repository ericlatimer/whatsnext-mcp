"""Send this server's own requests and logs to Application Insights.

As a Functions custom handler, requests are proxied straight to this process,
and the Functions host does not record them reliably: in testing (2026-11-16)
App Insights showed about half the requests, none of the 429s, and none of this
server's log lines. Flex Consumption also has no request/HTTP-status platform
metrics. So the server reports its own telemetry, and alerts read that.

No-op when APPLICATIONINSIGHTS_CONNECTION_STRING is unset (local runs, tests).
"""

import os

from azure.monitor.opentelemetry import configure_azure_monitor
from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware


def instrument(app, service_name: str = 'whatsnext-mcp'):
    connection_string = os.environ.get('APPLICATIONINSIGHTS_CONNECTION_STRING')
    if not connection_string:
        return app
    os.environ.setdefault('OTEL_SERVICE_NAME', service_name)
    configure_azure_monitor(
        connection_string=connection_string,
        # Only this server's loggers (guard rate-limit lines, upstream errors),
        # not the SDK's or uvicorn's.
        logger_name='whatsnext_mcp',
        enable_live_metrics=False,
    )
    # Outermost, so requests the guard refuses (401/429) are recorded too.
    return OpenTelemetryMiddleware(app)
