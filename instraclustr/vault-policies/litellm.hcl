# The model gateway (litellm, instraclustr/docker-compose.yml): its provider
# key, its master key and its database password, read-only, nothing else.
# Bound to the litellm SPIFFE ID by instraclustr/terraform/.
path "secret/data/litellm" { capabilities = ["read"] }
