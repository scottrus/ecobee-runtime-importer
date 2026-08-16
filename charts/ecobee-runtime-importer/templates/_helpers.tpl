{{- define "ecobee.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "ecobee.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "ecobee.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "ecobee.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{ include "ecobee.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "ecobee.selectorLabels" -}}
app.kubernetes.io/name: {{ include "ecobee.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "ecobee.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "ecobee.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
The Secret is never created by this chart — the importer rotates it in place and
a Helm-managed copy would be reset on every upgrade. This is a lookup only, and
the same value feeds the Role's resourceNames so the RBAC grant cannot drift
from the Secret it is meant to cover.
*/}}
{{- define "ecobee.secretName" -}}
{{- required "credentials.existingSecret is required — the Secret is created out of band, see the chart README" .Values.credentials.existingSecret -}}
{{- end -}}
