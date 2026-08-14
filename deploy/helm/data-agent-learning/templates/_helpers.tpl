{{/*
Expand the name of the chart.
*/}}
{{- define "data-agent-learning.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this
(by the DNS naming spec). If release name contains chart name it will be used
as a full name.
*/}}
{{- define "data-agent-learning.fullname" -}}
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
{{- define "data-agent-learning.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels applied to every object in the chart.
*/}}
{{- define "data-agent-learning.labels" -}}
helm.sh/chart: {{ include "data-agent-learning.chart" . }}
{{ include "data-agent-learning.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels (chart-wide). Stable across upgrades — MUST NOT include the
chart version or any per-release-mutating value.

The `app.kubernetes.io/name` here is `data-agent-learning`, NOT `data-agent`:
that is what keeps this chart's Services/PDBs from selecting the sibling
data-agent chart's pods when both are installed under one umbrella release.
*/}}
{{- define "data-agent-learning.selectorLabels" -}}
app.kubernetes.io/name: {{ include "data-agent-learning.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Join the chart fullname with a suffix, truncating the FULLNAME rather than the
RESULT so the suffix always survives.

This ordering is the whole point, and this chart is where it bites first: its
name is the longest of the two and its components carry the longest suffixes.
`printf "%s-%s" fullname suffix | trunc 63` cuts from the RIGHT, eating the one
part of the name that distinguishes one object from another — at a
long-but-legal release name (33 chars is enough) `learning-sweeper`,
`learning-consumer` and `learning-scheduler` all collapse onto
`…-data-agent-learning-learning`. Three Deployments, one name: one daemon runs,
the other two are silently absent, and nothing reports it. Budgeting
`63 - len(suffix) - 1` for the base keeps every generated name distinct AND
inside the 63-char DNS limit.

Usage: {{ include "data-agent-learning.suffixedName" (dict "root" $ "suffix" "config") }}
*/}}
{{- define "data-agent-learning.suffixedName" -}}
{{- $suffix := .suffix -}}
{{- $budget := int (sub 62 (len $suffix)) -}}
{{- printf "%s-%s" (include "data-agent-learning.fullname" .root | trunc $budget | trimSuffix "-") $suffix -}}
{{- end }}

{{/*
Per-component fully qualified name.
Usage: {{ include "data-agent-learning.componentFullname" (dict "root" $ "component" "inbox") }}
*/}}
{{- define "data-agent-learning.componentFullname" -}}
{{- include "data-agent-learning.suffixedName" (dict "root" .root "suffix" .component) }}
{{- end }}

{{/*
Per-component labels — common labels plus app.kubernetes.io/component.
Usage: {{ include "data-agent-learning.componentLabels" (dict "root" $ "component" "inbox") }}
*/}}
{{- define "data-agent-learning.componentLabels" -}}
{{ include "data-agent-learning.labels" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/*
Per-component selector labels — stable selector labels plus component.
Usage: {{ include "data-agent-learning.componentSelectorLabels" (dict "root" $ "component" "inbox") }}
*/}}
{{- define "data-agent-learning.componentSelectorLabels" -}}
{{ include "data-agent-learning.selectorLabels" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/*
Create the name of the service account to use.
*/}}
{{- define "data-agent-learning.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "data-agent-learning.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Name of the shared ConfigMap holding non-secret env.
*/}}
{{- define "data-agent-learning.configMapName" -}}
{{- include "data-agent-learning.suffixedName" (dict "root" . "suffix" "config") }}
{{- end }}

{{/*
Name of the Secret that pods should reference in envFrom.
When `secrets.existingSecret` is set we defer to that Secret (chart-managed
Secret is NOT rendered); otherwise the chart-managed Secret name is used.

Pointing this at the SAME existingSecret as the data-agent release is the
recommended posture: the Couchbase/Neo4j credentials and the tenant identity
they authenticate are one identity across both planes.
*/}}
{{- define "data-agent-learning.secretName" -}}
{{- if .Values.secrets.existingSecret }}
{{- .Values.secrets.existingSecret }}
{{- else }}
{{- include "data-agent-learning.suffixedName" (dict "root" . "suffix" "secret") }}
{{- end }}
{{- end }}

{{/*
Resolve the effective image reference for a component.
Per-component image fields fall back to the global `image` block, and the tag
falls back to the chart appVersion.
Usage: {{ include "data-agent-learning.image" (dict "root" $ "component" .Values.components.inbox) }}
*/}}
{{- define "data-agent-learning.image" -}}
{{- $global := .root.Values.image -}}
{{- $comp := default (dict) .component.image -}}
{{- $repository := default $global.repository $comp.repository -}}
{{- $tag := default (default .root.Chart.AppVersion $global.tag) $comp.tag -}}
{{- printf "%s:%s" $repository $tag -}}
{{- end }}

{{/*
Resolve the effective imagePullPolicy for a component.
*/}}
{{- define "data-agent-learning.imagePullPolicy" -}}
{{- $global := .root.Values.image -}}
{{- $comp := default (dict) .component.image -}}
{{- default $global.pullPolicy $comp.pullPolicy -}}
{{- end }}

{{/*
Default INBOX_SERVICE_URL derived from THIS release's inbox Service (the reviewer
UI BFF proxies its /api/inbox/* calls there), overridable via
`config.INBOX_SERVICE_URL`.

The derivation is correct here and was NOT in the data-agent chart: the inbox
Service is rendered by this same release, so its name is knowable. Override the
value when the inbox runs in another namespace/release (use the FQDN form,
http://<svc>.<ns>.svc.cluster.local:8100).
*/}}
{{- define "data-agent-learning.inboxServiceUrl" -}}
{{- $default := printf "http://%s:%d" (include "data-agent-learning.componentFullname" (dict "root" . "component" "inbox")) (int .Values.components.inbox.service.port) -}}
{{- default $default .Values.config.INBOX_SERVICE_URL -}}
{{- end }}

{{/*
Shared envFrom wiring: the non-secret ConfigMap + the (chart-managed or
existing) Secret. Render with the root context.
Usage: {{- include "data-agent-learning.envFrom" . | nindent 12 }}
*/}}
{{- define "data-agent-learning.envFrom" -}}
- configMapRef:
    name: {{ include "data-agent-learning.configMapName" . }}
- secretRef:
    name: {{ include "data-agent-learning.secretName" . }}
{{- end }}

{{/*
Pod-template checksum annotations so a change to the shared ConfigMap or Secret
triggers a rolling restart. Render with the root context.
Usage: {{- include "data-agent-learning.checksumAnnotations" . | nindent 8 }}
*/}}
{{- define "data-agent-learning.checksumAnnotations" -}}
checksum/config: {{ include (print .Template.BasePath "/configmap.yaml") . | sha256sum }}
checksum/secret: {{ include (print .Template.BasePath "/secret.yaml") . | sha256sum }}
{{- end }}
