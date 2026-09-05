# Production overlay

This directory is intentionally inactive. Production will use a separate
Kubernetes cluster and a separate Argo CD instance. Create overlays here only
after the cluster, Vault paths, endpoints, capacity, ingress and approval policy
have been reviewed. Promote backend digests from dev without rebuilding them;
the frontend is built separately because its public configuration is embedded
at build time.

Do not add a production `Application` until the production cluster is ready.
