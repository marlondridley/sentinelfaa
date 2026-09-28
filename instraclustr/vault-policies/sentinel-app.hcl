# Application services (api, dashboard, sentinel, temporal-worker): their
# database identity and their scoped LiteLLM key, read-only, nothing else.
# Bound to each service's own SPIFFE ID by instraclustr/terraform/.
path "secret/data/postgres/app"    { capabilities = ["read"] }
path "secret/data/sentinel/config" { capabilities = ["read"] }
