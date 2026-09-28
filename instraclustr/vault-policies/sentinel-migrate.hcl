# Schema jobs (migrate, proof): the owner identity, plus what they grant to
# the application role. Read-only.
path "secret/data/postgres/credentials" { capabilities = ["read"] }
path "secret/data/postgres/app"         { capabilities = ["read"] }
path "secret/data/sentinel/config"      { capabilities = ["read"] }
