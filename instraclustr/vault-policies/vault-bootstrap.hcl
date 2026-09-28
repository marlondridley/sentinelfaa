# The bootstrap jobs (vault-unseal-and-seed.bash, litellm-bootstrap.sh): seed
# this project's secrets and rotate their own secret-id, nothing else. No
# sys/seal, sys/mounts, sys/policies/acl writes, sys/generate-root, auth
# method or role configuration -- so this identity cannot author a policy and
# bind itself or anything else to it: it is not root-equivalent.
#
# Paths are named exactly: Vault's ACL glob only matches a trailing "*"
# against the REST of the path, not "*" followed by more literal path.
path "secret/data/*"                                                { capabilities = ["create", "update", "read"] }
path "secret/metadata/*"                                            { capabilities = ["read", "list"] }
path "auth/approle/role/vault-bootstrap/secret-id"                  { capabilities = ["create", "update"] }
path "auth/approle/role/vault-bootstrap/secret-id-accessor/destroy" { capabilities = ["create", "update"] }
path "auth/approle/role/vault-bootstrap/role-id"                    { capabilities = ["read"] }
