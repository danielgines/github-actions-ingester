{{/*
Common helpers — name, fullname, labels.
*/}}

{{- define "github-actions-ingester.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "github-actions-ingester.fullname" -}}
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

{{- define "github-actions-ingester.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "github-actions-ingester.labels" -}}
helm.sh/chart: {{ include "github-actions-ingester.chart" . }}
{{ include "github-actions-ingester.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "github-actions-ingester.selectorLabels" -}}
app.kubernetes.io/name: {{ include "github-actions-ingester.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "github-actions-ingester.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "github-actions-ingester.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
Secret holding the GitHub credential: the user's own, or the chart-managed
one rendered from inline values. Empty when nothing is configured.
*/}}
{{- define "github-actions-ingester.authSecretName" -}}
{{- if .Values.auth.existingSecret -}}
{{- .Values.auth.existingSecret -}}
{{- else if or .Values.auth.token .Values.auth.app.id -}}
{{- printf "%s-github" (include "github-actions-ingester.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/*
Secret holding GHA_DATABASE_URL, same resolution.
*/}}
{{- define "github-actions-ingester.databaseSecretName" -}}
{{- if .Values.database.existingSecret -}}
{{- .Values.database.existingSecret -}}
{{- else if .Values.database.url -}}
{{- printf "%s-database" (include "github-actions-ingester.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "github-actions-ingester.databaseSecretKey" -}}
{{- if .Values.database.existingSecret -}}
{{- default "GHA_DATABASE_URL" .Values.database.existingSecretKey -}}
{{- else -}}
GHA_DATABASE_URL
{{- end -}}
{{- end -}}
