# The bootstrap jobs (vault-unseal-and-seed.bash, litellm-bootstrap.sh). Seed
# this project's secrets and rotate the sentinel-* AppRoles' secret-ids;
# nothing that authors a policy or reconfigures a role. No sys/seal, sys/mounts, sys/policies/acl writes, sys/generate-root,
# other auth methods, or auth/approle/role/<name> (the bare path -- role
# config) writes. This is why it is not root-equivalent: it can rotate a
# credential for a role someone else already defined, never define one itself.
#
# Each role is named explicitly rather than "sentinel-*": Vault's ACL glob only
# matches a trailing "*" against the REST of the path, not "*" followed by more
# literal path (auth/approle/role/sentinel-*/role-id does not match
# auth/approle/role/sentinel-app/role-id) -- confirmed the hard way, against a
# real Vault, before this was caught as a silent 403 rather than a policy typo.
path "secret/data/*"                                 { capabilities = ["create", "update", "read"] }
path "secret/metadata/*"                             { capabilities = ["read", "list"] }
path "auth/approle/role/vault-bootstrap/secret-id"                  { capabilities = ["create", "update"] }
path "auth/approle/role/vault-bootstrap/secret-id-accessor/destroy" { capabilities = ["create", "update"] }
path "auth/approle/role/vault-bootstrap/role-id"                    { capabilities = ["read"] }
path "auth/approle/role/sentinel-app/secret-id"                     { capabilities = ["create", "update"] }
path "auth/approle/role/sentinel-app/secret-id-accessor/destroy"    { capabilities = ["create", "update"] }
path "auth/approle/role/sentinel-app/role-id"                       { capabilities = ["read"] }
path "auth/approle/role/sentinel-migrate/secret-id"                 { capabilities = ["create", "update"] }
path "auth/approle/role/sentinel-migrate/secret-id-accessor/destroy" { capabilities = ["create", "update"] }
path "auth/approle/role/sentinel-migrate/role-id"                   { capabilities = ["read"] }
