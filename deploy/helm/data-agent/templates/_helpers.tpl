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
Join the chart fullname with a suffix, truncating the FULLNAME rather than the
RESULT so the suffix always survives.

This ordering is the whole point. `printf "%s-%s" fullname suffix | trunc 63`
cuts from the RIGHT, which eats the one part of the name that distinguishes one
object from another: at a long-but-legal release name every component collapses
onto the same truncated string, and two Deployments sharing a name is not a
render curiosity — one silently overwrites the other and never runs. Budgeting
`63 - len(suffix) - 1` for the base keeps every generated name distinct AND
inside the 63-char DNS limit.

Usage: {{ include "data-agent.suffixedName" (dict "root" $ "suffix" "config") }}
*/}}
{{- define "data-agent.suffixedName" -}}
{{- $suffix := .suffix -}}
{{- $budget := int (sub 62 (len $suffix)) -}}
{{- printf "%s-%s" (include "data-agent.fullname" .root | trunc $budget | trimSuffix "-") $suffix -}}
{{- end }}

{{/*
Per-component fully qualified name.
Usage: {{ include "data-agent.componentFullname" (dict "root" $ "component" "runtime") }}
*/}}
{{- define "data-agent.componentFullname" -}}
{{- include "data-agent.suffixedName" (dict "root" .root "suffix" .component) }}
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
{{- include "data-agent.suffixedName" (dict "root" . "suffix" "config") }}
{{- end }}

{{/*
Name of the Secret that pods should reference in envFrom.

THE CHART NO LONGER RENDERS A SECRET. `secrets.existingSecret` names one you
created out-of-band; when it is empty this falls back to the conventional
`<fullname>-secret`, which you must create yourself with the keys listed in
values.yaml. Either way the name must EXIST before the pods start — envFrom
against a missing Secret leaves them stuck in CreateContainerConfigError.

Pointing this at the SAME Secret as the data-agent-learning release is the
recommended posture: the Couchbase/Neo4j credentials and the tenant identity they
authenticate are one identity across both planes.
*/}}
{{- define "data-agent.secretName" -}}
{{- if .Values.secrets.existingSecret }}
{{- .Values.secrets.existingSecret }}
{{- else }}
{{- include "data-agent.suffixedName" (dict "root" . "suffix" "secret") }}
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
There is deliberately NO inboxServiceUrl helper here. The review-inbox service
is deployed by the SEPARATE `data-agent-learning` chart, whose release name this
chart cannot know — so an auto-derivation from THIS release name would point at
a Service that does not exist. The reviewer UI (which needs it) is deployed by
that chart, beside the inbox it derives from.
*/}}

{{/*
Shared envFrom wiring: the chart's ConfigMap + the OUT-OF-BAND Secret named by
the secretName helper (this chart renders no Secret of its own). Render with the
root context.
Usage: {{- include "data-agent.envFrom" . | nindent 12 }}
*/}}
{{- define "data-agent.envFrom" -}}
- configMapRef:
    name: {{ include "data-agent.configMapName" . }}
- secretRef:
    name: {{ include "data-agent.secretName" . }}
{{- end }}

{{/*
Pod-template checksum annotation so a change to the shared ConfigMap triggers a
rolling restart. Render with the root context.

There is NO secret checksum: the Secret is created out-of-band (see the
secretName helper), so the chart cannot see its contents and cannot hash them.
Rotating a key in that Secret does NOT restart the pods — roll them yourself
(`kubectl rollout restart deploy -l app.kubernetes.io/instance=<release>`).

Usage: {{- include "data-agent.checksumAnnotations" . | nindent 8 }}
*/}}
{{- define "data-agent.checksumAnnotations" -}}
checksum/config: {{ include (print .Template.BasePath "/configmap.yaml") . | sha256sum }}
{{- end }}
