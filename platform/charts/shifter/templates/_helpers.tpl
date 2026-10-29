{{- define "shifter.labels" -}}
app.kubernetes.io/name: shifter
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end }}

{{- define "shifter.partOfLabels" -}}
{{ include "shifter.labels" . }}
app.kubernetes.io/part-of: shifter
{{- end }}

{{- define "shifter.podSecurityContext" -}}
securityContext:
  seccompProfile:
    type: {{ .Values.security.pod.seccompProfile }}
{{- end }}

{{- define "shifter.accessNodePlacement" -}}
{{- if .Values.capabilities.gcpAccessNodePool }}
nodeSelector:
  node-restriction.kubernetes.io/shifter-pool: access
tolerations:
  - key: dedicated
    operator: Equal
    value: access
    effect: NoSchedule
{{- end }}
{{- end }}

{{- define "shifter.podAntiAffinity" -}}
affinity:
  podAntiAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 100
        podAffinityTerm:
          labelSelector:
            matchLabels:
              app.kubernetes.io/part-of: shifter
              app.kubernetes.io/component: {{ .component }}
          topologyKey: kubernetes.io/hostname
{{- end }}

{{- define "shifter.containerSecurityContextApp" -}}
securityContext:
  allowPrivilegeEscalation: false
  capabilities:
    drop:
      - ALL
  readOnlyRootFilesystem: true
  runAsNonRoot: true
  runAsUser: {{ .Values.security.app.runAsUser }}
  runAsGroup: {{ .Values.security.app.runAsGroup }}
{{- end }}

{{- define "shifter.containerSecurityContextGuacd" -}}
securityContext:
  allowPrivilegeEscalation: false
  capabilities:
    drop:
      - ALL
  readOnlyRootFilesystem: true
  runAsNonRoot: true
  runAsUser: {{ .Values.security.guacd.runAsUser }}
  runAsGroup: {{ .Values.security.guacd.runAsGroup }}
{{- end }}

{{- define "shifter.containerSecurityContextGuacamole" -}}
securityContext:
  allowPrivilegeEscalation: false
  capabilities:
    drop:
      - ALL
  readOnlyRootFilesystem: true
  runAsNonRoot: true
  runAsUser: {{ .Values.security.guacamole.runAsUser }}
  runAsGroup: {{ .Values.security.guacamole.runAsGroup }}
{{- end }}

{{- define "shifter.tmpVolumeMount" -}}
volumeMounts:
  - name: tmp
    mountPath: /tmp
  {{- if and .Values.openfga.enabled (ne .Values.openfga.storeId "") (ne .Values.openfga.modelId "") }}
  - name: openfga-token
    mountPath: /run/secrets/openfga
    readOnly: true
  - name: openfga-ca
    mountPath: /run/secrets/openfga-ca
    readOnly: true
  {{- end }}
{{- end }}

{{- define "shifter.tmpVolume" -}}
volumes:
  - name: tmp
    emptyDir:
      sizeLimit: {{ .Values.tmpVolume.sizeLimit | quote }}
  {{- if and .Values.openfga.enabled (ne .Values.openfga.storeId "") (ne .Values.openfga.modelId "") }}
  - name: openfga-token
    secret:
      secretName: {{ .Values.openfga.runtimeSecretName }}
      items:
        - key: runtime-preshared-key
          path: token
  - name: openfga-ca
    configMap:
      name: {{ .Values.openfga.caConfigMapName }}
      items:
        - key: ca.crt
          path: ca.crt
  {{- end }}
{{- end }}

{{- define "shifter.runtimeConfigChecksum" -}}
{{ toJson (dict "runtimeEnv" .Values.runtimeEnv "openfga" (dict "enabled" .Values.openfga.enabled "hostname" .Values.openfga.hostname "storeId" .Values.openfga.storeId "modelId" .Values.openfga.modelId) "jobsNamespace" .Values.namespaces.jobs "provisionerServiceAccount" .Values.serviceAccounts.provisioner.name) | sha256sum }}
{{- end }}

{{- define "shifter.portalImage" -}}
{{ .Values.images.platform }}
{{- end }}

{{- define "shifter.guacdImage" -}}
{{ .Values.images.guacd }}
{{- end }}

{{- define "shifter.guacamoleClientImage" -}}
{{ .Values.images.guacamoleClient }}
{{- end }}

{{- define "shifter.cloudSqlProxyContainer" -}}
{{- if ne .root.Values.provider.name "gcp" }}
{{- fail "The native Cloud SQL proxy requires the GCP provider" }}
{{- end }}
- name: cloud-sql-proxy
  image: "gcr.io/cloud-sql-connectors/cloud-sql-proxy@sha256:4f9071e7fb8bc0acc0c66dbbaa292d7c0e6337003ccd29f75e1431f8f8c3fce6"
  imagePullPolicy: IfNotPresent
  restartPolicy: Always
  args:
    {{- if .autoIam }}
    - --auto-iam-authn
    {{- end }}
    - --private-ip
    - --address=127.0.0.1
    - --port=5432
    - --structured-logs
    - --health-check
    - --http-address=0.0.0.0
    - --http-port=9801
    - --run-connection-test
    - {{ .root.Values.cloudSqlProxy.instanceConnectionName | quote }}
  securityContext:
    runAsUser: 65532
    runAsGroup: 65532
    runAsNonRoot: true
    allowPrivilegeEscalation: false
    readOnlyRootFilesystem: true
    capabilities:
      drop: ["ALL"]
    seccompProfile:
      type: RuntimeDefault
  resources:
    requests: {cpu: 100m, memory: 128Mi, ephemeral-storage: 16Mi}
    limits: {cpu: "1", memory: 256Mi, ephemeral-storage: 32Mi}
  startupProbe:
    httpGet: {path: /startup, port: 9801}
    periodSeconds: 1
    timeoutSeconds: 1
    failureThreshold: 60
{{- end }}

{{- define "shifter.cloudSqlProxyPod" -}}
{{- if .root.Values.cloudSqlProxy.enabled }}
initContainers:
  {{- include "shifter.cloudSqlProxyContainer" . | nindent 2 }}
{{- end }}
{{- end }}

{{- define "shifter.databaseUserEnv" -}}
{{- if .root.Values.cloudSqlProxy.enabled }}
- name: DB_AUTH_MODE
  value: cloud-sql-proxy
- name: DB_HOST
  value: "127.0.0.1"
- name: DB_PORT
  value: "5432"
- name: DB_USER
  value: {{ required "Native database login is missing for this workload" (get .root.Values.cloudSqlProxy.databaseUsers .role) | quote }}
- name: DB_SECRET_ID
  value: ""
{{- end }}
{{- if and .root.Values.openfga.enabled (ne .root.Values.openfga.storeId "") (ne .root.Values.openfga.modelId "") }}
- name: OPENFGA_API_URL
  value: {{ printf "https://%s" .root.Values.openfga.hostname | quote }}
- name: OPENFGA_STORE_ID
  value: {{ .root.Values.openfga.storeId | quote }}
- name: OPENFGA_MODEL_ID
  value: {{ .root.Values.openfga.modelId | quote }}
- name: OPENFGA_API_TOKEN_FILE
  value: /run/secrets/openfga/token
- name: OPENFGA_CA_CERT_PATH
  value: /run/secrets/openfga-ca/ca.crt
{{- end }}
{{- end }}

{{/*
The platform-runtime environment as a JSON object. configmap-runtime.yaml renders
it, and restrict-provisioner-jobs inlines the same map as its params (#2633), so
the admission policy and the ConfigMap can not drift.
*/}}
{{- define "shifter.platformRuntimeEnv" -}}
{{- $runtimeEnv := deepCopy .Values.runtimeEnv -}}
{{- if .Values.capabilities.kubernetesJobLauncher -}}
{{- $_ := set $runtimeEnv "ENGINE_TASK_NAMESPACE" .Values.namespaces.jobs -}}
{{- $_ = set $runtimeEnv "ENGINE_TASK_SERVICE_ACCOUNT_NAME" .Values.serviceAccounts.provisioner.name -}}
{{- end -}}
{{- /* CTF image copies and webhooks reach public hosts only through content-egress (#2090). */ -}}
{{- $_ := set $runtimeEnv "CTF_EGRESS_PROXY" "content-egress:3128" -}}
{{- with .Values.runtime.secretReferences.app -}}
{{- $_ := set $runtimeEnv "APP_SECRET_ID" . -}}
{{- end -}}
{{- with .Values.runtime.secretReferences.database -}}
{{- $_ := set $runtimeEnv "DB_SECRET_ID" . -}}
{{- end -}}
{{- if and .Values.modelBroker.enabled (eq (get (default dict .Values.modelBroker.control_env) "MODEL_ACCESS_ENABLED") "true") -}}
{{- range $key, $value := .Values.modelBroker.enrollment_env -}}
{{- $_ := set $runtimeEnv $key $value -}}
{{- end -}}
{{- end -}}
{{- toJson $runtimeEnv -}}
{{- end -}}
