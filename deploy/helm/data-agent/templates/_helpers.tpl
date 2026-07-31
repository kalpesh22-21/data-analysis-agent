{{/*
Expand the name of the chart.
*/}}
{{- define "data-agent.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this
(by the DNS naming spec). If release name contains chart name it will be used
as a full name.
*/}}
{{- define "data-agent.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "data-agent.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels applied to every object in the chart.
*/}}
{{- define "data-agent.labels" -}}
helm.sh/chart: {{ include "data-agent.chart" . }}
{{ include "data-agent.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels (chart-wide). Stable across upgrades — MUST NOT include the
chart version or any per-release-mutating value.
*/}}
{{- define "data-agent.selectorLabels" -}}
app.kubernetes.io/name: {{ include "data-agent.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Per-component fully qualified name.
Usage: {{ include "data-agent.componentFullname" (dict "root" $ "component" "runtime") }}
*/}}
{{- define "data-agent.componentFullname" -}}
{{- printf "%s-%s" (include "data-agent.fullname" .root) .component | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Per-component labels — common labels plus app.kubernetes.io/component.
Usage: {{ include "data-agent.componentLabels" (dict "root" $ "component" "runtime") }}
*/}}
{{- define "data-agent.componentLabels" -}}
{{ include "data-agent.labels" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/*
Per-component selector labels — stable selector labels plus component.
Usage: {{ include "data-agent.componentSelectorLabels" (dict "root" $ "component" "runtime") }}
*/}}
{{- define "data-agent.componentSelectorLabels" -}}
{{ include "data-agent.selectorLabels" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/*
Create the name of the service account to use.
*/}}
{{- define "data-agent.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "data-agent.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Name of the shared ConfigMap holding non-secret env.
*/}}
{{- define "data-agent.configMapName" -}}
{{- printf "%s-config" (include "data-agent.fullname" .) }}
{{- end }}

{{/*
Name of the Secret that pods should reference in envFrom.
When `secrets.existingSecret` is set we defer to that Secret (chart-managed
Secret is NOT rendered); otherwise the chart-managed Secret name is used.
*/}}
{{- define "data-agent.secretName" -}}
{{- if .Values.secrets.existingSecret }}
{{- .Values.secrets.existingSecret }}
{{- else }}
{{- printf "%s-secret" (include "data-agent.fullname" .) }}
{{- end }}
{{- end }}

{{/*
Resolve the effective image reference for a component.
Per-component image fields fall back to the global `image` block, and the tag
falls back to the chart appVersion.
Usage: {{ include "data-agent.image" (dict "root" $ "component" .Values.components.runtime) }}
*/}}
{{- define "data-agent.image" -}}
{{- $global := .root.Values.image -}}
{{- $comp := default (dict) .component.image -}}
{{- $repository := default $global.repository $comp.repository -}}
{{- $tag := default (default .root.Chart.AppVersion $global.tag) $comp.tag -}}
{{- printf "%s:%s" $repository $tag -}}
{{- end }}

{{/*
Resolve the effective imagePullPolicy for a component.
*/}}
{{- define "data-agent.imagePullPolicy" -}}
{{- $global := .root.Values.image -}}
{{- $comp := default (dict) .component.image -}}
{{- default $global.pullPolicy $comp.pullPolicy -}}
{{- end }}

{{/*
Default RUNTIME_URL derived from the release name, overridable via
`config.RUNTIME_URL`.
*/}}
{{- define "data-agent.runtimeUrl" -}}
{{- $default := printf "http://%s:%d" (include "data-agent.componentFullname" (dict "root" . "component" "runtime")) (int .Values.components.runtime.service.port) -}}
{{- default $default .Values.config.RUNTIME_URL -}}
{{- end }}

{{/*
Default INBOX_SERVICE_URL derived from the release name (points the UI BFF at
the in-cluster inbox Service), overridable via `config.INBOX_SERVICE_URL`.
*/}}
{{- define "data-agent.inboxServiceUrl" -}}
{{- $default := printf "http://%s:%d" (include "data-agent.componentFullname" (dict "root" . "component" "inbox")) (int .Values.components.inbox.service.port) -}}
{{- default $default .Values.config.INBOX_SERVICE_URL -}}
{{- end }}

{{/*
Shared envFrom wiring: the non-secret ConfigMap + the (chart-managed or
existing) Secret. Render with the root context.
Usage: {{- include "data-agent.envFrom" . | nindent 12 }}
*/}}
{{- define "data-agent.envFrom" -}}
- configMapRef:
    name: {{ include "data-agent.configMapName" . }}
- secretRef:
    name: {{ include "data-agent.secretName" . }}
{{- end }}

{{/*
Pod-template checksum annotations so a change to the shared ConfigMap or Secret
triggers a rolling restart. Render with the root context.
Usage: {{- include "data-agent.checksumAnnotations" . | nindent 8 }}
*/}}
{{- define "data-agent.checksumAnnotations" -}}
checksum/config: {{ include (print .Template.BasePath "/configmap.yaml") . | sha256sum }}
checksum/secret: {{ include (print .Template.BasePath "/secret.yaml") . | sha256sum }}
{{- end }}
