{{/* Names and labels. */}}
{{- define "mcp-gateway.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "mcp-gateway.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "mcp-gateway.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "mcp-gateway.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "mcp-gateway.selectorLabels" -}}
app.kubernetes.io/name: {{ include "mcp-gateway.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "mcp-gateway.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "mcp-gateway.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* The Secret holding MCP_GATEWAY_CONFIG: rendered from .Values.config, or the existing one. */}}
{{- define "mcp-gateway.configSecretName" -}}
{{- if .Values.existingSecret.name -}}
{{- .Values.existingSecret.name -}}
{{- else -}}
{{- include "mcp-gateway.fullname" . -}}
{{- end -}}
{{- end -}}

{{/*
Seconds the gateway needs to stop: a request that runs on after its client left is bounded by the
upstream's read_timeout_seconds, then the application gives the reads 10 s to finish and 10 s to
close once cancelled (DETACHED_READS_SHUTDOWN_SECONDS in src/mcp_gateway/app.py, twice), and the
preStop sleep keeps the pod serving while the endpoint is removed from the Services.
*/}}
{{- define "mcp-gateway.shutdownBudgetSeconds" -}}
{{- add 20 (int .Values.preStopSleepSeconds) -}}
{{- end -}}

{{/* The largest upstreams.*.read_timeout_seconds of the inline config, 0 when none is readable. */}}
{{- define "mcp-gateway.maxReadTimeoutSeconds" -}}
{{- $max := 0.0 -}}
{{- $upstreams := get (default dict .Values.config) "upstreams" -}}
{{- if kindIs "map" $upstreams -}}
{{- range $name, $upstream := $upstreams -}}
{{- if kindIs "map" $upstream -}}
{{- $timeout := get $upstream "read_timeout_seconds" -}}
{{- if or (kindIs "float64" $timeout) (kindIs "int64" $timeout) (kindIs "int" $timeout) -}}
{{- $max = maxf $max (float64 $timeout) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- $max -}}
{{- end -}}

{{/* terminationGracePeriodSeconds: the configured one, else the largest read timeout plus the shutdown budget. */}}
{{- define "mcp-gateway.terminationGracePeriodSeconds" -}}
{{- if not (kindIs "invalid" .Values.terminationGracePeriodSeconds) -}}
{{- .Values.terminationGracePeriodSeconds -}}
{{- else -}}
{{- add (int64 (ceil (float64 (include "mcp-gateway.maxReadTimeoutSeconds" .)))) (int (include "mcp-gateway.shutdownBudgetSeconds" .)) 5 -}}
{{- end -}}
{{- end -}}

{{/* True when more than one replica can run (a fixed count above 1, or an autoscaler allowed above 1). */}}
{{- define "mcp-gateway.multiReplica" -}}
{{- if or (gt (int .Values.replicaCount) 1) (and .Values.autoscaling.enabled (gt (int .Values.autoscaling.maxReplicas) 1)) -}}true{{- end -}}
{{- end -}}

{{/*
Render-time checks. Every read of the config is guarded: `helm lint` keeps rendering after a
failure, so a missing or mistyped key must produce a message here and never a nil dereference.
*/}}
{{- define "mcp-gateway.validate" -}}
{{- $config := default dict .Values.config -}}
{{- $inline := not (empty $config) -}}
{{- $existing := not (empty .Values.existingSecret.name) -}}
{{- if and $inline $existing -}}
{{- fail "set either config (the chart renders the Secret) or existingSecret.name, not both" -}}
{{- end -}}
{{- if not (or $inline $existing) -}}
{{- fail "no gateway configuration: set config (see docs/configuration.md) or existingSecret.name" -}}
{{- end -}}
{{- if not .Values.image.repository -}}
{{- fail "image.repository is required" -}}
{{- end -}}
{{- if and .Values.autoscaling.enabled (lt (int .Values.autoscaling.maxReplicas) (int .Values.autoscaling.minReplicas)) -}}
{{- fail "autoscaling.maxReplicas must not be below autoscaling.minReplicas" -}}
{{- end -}}
{{- $multi := include "mcp-gateway.multiReplica" . -}}
{{- if $inline -}}
{{- $state := get $config "state" -}}
{{- $stateType := "" -}}
{{- if kindIs "map" $state -}}{{- $stateType = get $state "type" -}}{{- end -}}
{{- if and $multi (ne (toString $stateType) "redis") -}}
{{- fail (printf "more than one replica can run (replicaCount %v, autoscaling.enabled %v, autoscaling.maxReplicas %v) and that needs the shared state: set config.state.type to \"redis\" (a per-process state gives every replica its own sessions, resolver cache and rate limit buckets)" .Values.replicaCount .Values.autoscaling.enabled .Values.autoscaling.maxReplicas) -}}
{{- end -}}
{{- $listen := get $config "listen" -}}
{{- if kindIs "map" $listen -}}
{{- $port := get $listen "port" -}}
{{- if and (not (kindIs "invalid" $port)) (ne (int $port) (int .Values.containerPort)) -}}
{{- fail (printf "config.listen.port (%v) must equal containerPort (%v): the probes and the Service use it" $port .Values.containerPort) -}}
{{- end -}}
{{- $host := get $listen "host" -}}
{{- if and (not (kindIs "invalid" $host)) (not (has (toString $host) (list "0.0.0.0" "::"))) -}}
{{- fail (printf "config.listen.host is %q: the gateway must listen on 0.0.0.0 (or ::) for the probes and the Service to reach it" (toString $host)) -}}
{{- end -}}
{{- end -}}
{{- $publicUrl := get $config "public_url" -}}
{{- if kindIs "string" $publicUrl -}}
{{- $path := (urlParse $publicUrl).path -}}
{{- if ne $path .Values.publicPath -}}
{{- fail (printf "publicPath (%q) must equal the path of config.public_url (%q): the Ingress and HTTPRoute route it and its /.well-known document" .Values.publicPath $path) -}}
{{- end -}}
{{- end -}}
{{- $maxRead := float64 (include "mcp-gateway.maxReadTimeoutSeconds" .) -}}
{{- $grace := float64 (include "mcp-gateway.terminationGracePeriodSeconds" .) -}}
{{- if le $grace $maxRead -}}
{{- fail (printf "terminationGracePeriodSeconds (%v) must be greater than the largest upstream read_timeout_seconds (%v): requests that run on after their client left are bounded by it, and the kubelet would kill them. Leave it unset for a computed default (%v)" $grace $maxRead (add (int64 (ceil $maxRead)) (int (include "mcp-gateway.shutdownBudgetSeconds" .)) 5)) -}}
{{- end -}}
{{- else -}}
{{- if and $multi (not .Values.existingSecret.sharedState) -}}
{{- fail "more than one replica needs the shared state: the configuration in existingSecret must have state.type \"redis\"; confirm it with existingSecret.sharedState=true" -}}
{{- end -}}
{{- if kindIs "invalid" .Values.terminationGracePeriodSeconds -}}
{{- fail "with existingSecret the chart cannot read the upstream read timeouts: set terminationGracePeriodSeconds above the largest read_timeout_seconds plus the shutdown budget (see README)" -}}
{{- end -}}
{{- end -}}
{{- if and .Values.ingress.enabled .Values.httpRoute.enabled -}}
{{- fail "enable either ingress or httpRoute, not both" -}}
{{- end -}}
{{- end -}}
