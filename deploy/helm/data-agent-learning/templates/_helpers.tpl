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
Name of the Secret that pods should reference in envFrom, in precedence order:

  1. `secrets.existingSecret` — this chart's own override.
  2. `global.secrets.existingSecret` — set once by the `data-agent-platform`
     umbrella so ONE out-of-band Secret serves both planes.
  3. the conventional `<fullname>-secret`.

THE CHART NO LONGER RENDERS A SECRET. Whichever name wins, you create it
out-of-band with the keys listed in values.yaml, and it must EXIST before the
pods start — envFrom against a missing Secret leaves them stuck in
CreateContainerConfigError.

LOCAL BEATS GLOBAL HERE, the opposite of the config merge in configmap.yaml, and
the asymmetry is deliberate. `secrets.existingSecret` ships EMPTY, so a local
override can only exist because someone typed it — deferring to it costs the
global nothing and leaves an escape hatch for a plane whose credentials genuinely
differ. The 18 shared `config` keys ship NON-EMPTY defaults in both charts, so
letting local win there would mean `global.config` could never take effect at all.

Pointing this at the SAME Secret as the data-agent release (which is what
`global.secrets.existingSecret` does for you) is the recommended posture: the
Couchbase/Neo4j credentials and the tenant identity they authenticate are one
identity across both planes.
*/}}
{{- define "data-agent-learning.secretName" -}}
{{- $existing := default ((.Values.global).secrets).existingSecret .Values.secrets.existingSecret }}
{{- if $existing }}
{{- $existing }}
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
Effective LEARNING_REDIS_URL for the learning job stream, in precedence order:

  1. `config.LEARNING_REDIS_URL` when non-empty — an EXTERNAL Redis you manage.
     Always wins, even with the in-chart Redis enabled, so you can point the
     workloads elsewhere without first tearing the in-chart one down.
  2. the in-chart Redis Service when `redis.enabled` — rendered by this same
     release, so its name is knowable (same reasoning as inboxServiceUrl).
  3. the legacy literal `redis://redis:6379/0` — the previous default, kept so a
     release that had `redis.enabled=false` and no explicit URL keeps talking to
     whatever `redis` Service it was already talking to.

Case 3 is a GUESS, not a configuration: if you disable the in-chart Redis you
should set `config.LEARNING_REDIS_URL` explicitly.
*/}}
{{- define "data-agent-learning.redisUrl" -}}
{{- if .Values.config.LEARNING_REDIS_URL -}}
{{- .Values.config.LEARNING_REDIS_URL -}}
{{- else if .Values.redis.enabled -}}
{{- printf "redis://%s:%d/0" (include "data-agent-learning.componentFullname" (dict "root" . "component" "redis")) (int .Values.redis.service.port) -}}
{{- else -}}
redis://redis:6379/0
{{- end -}}
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
Pod-template checksum annotation so a change to the shared ConfigMap triggers a
rolling restart. Render with the root context.

There is NO secret checksum: the Secret is created out-of-band (see the
secretName helper), so the chart cannot see its contents and cannot hash them.
Rotating a key in that Secret does NOT restart the pods — roll them yourself
(`kubectl rollout restart deploy -l app.kubernetes.io/instance=<release>`).

Usage: {{- include "data-agent-learning.checksumAnnotations" . | nindent 8 }}
*/}}
{{- define "data-agent-learning.checksumAnnotations" -}}
checksum/config: {{ include (print .Template.BasePath "/configmap.yaml") . | sha256sum }}
{{- end }}
