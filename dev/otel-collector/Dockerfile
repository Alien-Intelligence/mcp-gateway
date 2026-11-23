# OpenTelemetry collector of the dev and stub stacks. The configuration is baked into the image
# (a bind mount lands empty on a remote daemon), and the output directory is created here so a
# fresh named volume mounted on it inherits the owner the compose service runs as.
FROM busybox:1.37 AS prepare
RUN mkdir /otel-out && chown 1000:1000 /otel-out

FROM otel/opentelemetry-collector-contrib:0.141.0
COPY config.yaml /etc/otelcol/config.yaml
COPY --from=prepare --chown=1000:1000 /otel-out /otel-out
USER 1000:1000
CMD ["--config", "/etc/otelcol/config.yaml"]
